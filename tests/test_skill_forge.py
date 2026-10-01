"""Behavioral, security, and portability tests for Skill Forge."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from core.capabilities.registry import CapabilityRegistry
from core.skill_forge import (
    AcceptanceCase,
    ForgeProposal,
    ForgeRequest,
    SkillForge,
    SkillForgeError,
)
from core.skill_forge.loader import capability_class_from_source
from runtime.persistence import JSONFileBackend


def capability_source(capability_id: str, *, wrong: bool = False) -> str:
    returned = "'wrong'" if wrong else "text"
    class_name = "".join(part.title() for part in capability_id.split("_")) + "Capability"
    return f'''from __future__ import annotations

from typing import Any
from core.capabilities.base import Capability, Skill, object_schema
from core.capabilities.registry import register
from core.capabilities.result import CapabilityResult

@register
class {class_name}(Capability):
    id = "{capability_id}"
    name = "Verified Echo"
    version = "1.0.0"
    author = "forge-test"
    description = "Echo text with an observed length"
    permissions = ["public"]

    def install(self, identity_id: str, storage: Any) -> None:
        storage.save(identity_id, "capability.{capability_id}", {{"installed": True}})

    def uninstall(self, identity_id: str, storage: Any) -> None:
        storage.delete(identity_id, "capability.{capability_id}")

    def prompts(self, identity_id: str) -> list[str]:
        return ["Use {capability_id}.echo to echo text."]

    def skills(self) -> list[Skill]:
        return [Skill(
            name="{capability_id}.echo",
            description="Echo text and report its length",
            input_schema=object_schema({{"text": {{"type": "string"}}}}, required=("text",)),
            verification_params={{"text": "probe"}},
        )]

    def call(self, skill_name: str, **params: Any) -> CapabilityResult:
        if skill_name != "{capability_id}.echo":
            return CapabilityResult.fail(self.id, skill_name, "unknown_skill", "unknown skill")
        text = str(params.get("text", ""))
        return CapabilityResult.ok(
            self.id,
            skill_name,
            {{"echo": {returned}, "length": len(text)}},
            source="verified echo",
        )
'''


def manifest(capability_id: str) -> dict:
    return {
        "id": capability_id,
        "name": "Verified Echo",
        "version": "1.0.0",
        "author": "forge-test",
        "description": "Echo text with an observed length",
        "permissions": ["public"],
        "dependencies": [],
        "skills": [{"name": f"{capability_id}.echo", "description": "Echo text"}],
    }


def cases(capability_id: str) -> list[AcceptanceCase]:
    return [
        AcceptanceCase(
            name="short input",
            skill=f"{capability_id}.echo",
            params={"text": "bird"},
            expected={"echo": "bird", "length": 4},
        ),
        AcceptanceCase(
            name="different input",
            skill=f"{capability_id}.echo",
            params={"text": "north star"},
            expected={"echo": "north star", "length": 10},
        ),
    ]


class StaticDesigner:
    def __init__(self, capability_id: str) -> None:
        self.capability_id = capability_id
        self.called = False

    def design(self, request, identity=None):
        self.called = True
        return cases(self.capability_id)


class StaticAuthor:
    def __init__(self, capability_id: str, designer: StaticDesigner, *, wrong: bool = False) -> None:
        self.capability_id = capability_id
        self.designer = designer
        self.wrong = wrong

    def author(self, request, identity=None):
        assert self.designer.called, "independent tests must be designed before source is authored"
        return ForgeProposal(
            source=capability_source(self.capability_id, wrong=self.wrong),
            manifest=manifest(self.capability_id),
        )


def build_forge(tmp_path: Path, cap_id: str, *, wrong: bool = False) -> SkillForge:
    designer = StaticDesigner(cap_id)
    return SkillForge(
        tmp_path / "workspace",
        author=StaticAuthor(cap_id, designer, wrong=wrong),
        test_designer=designer,
    )


def test_forge_runs_independent_cases_and_packages_exact_tested_bytes(tmp_path):
    cap_id = "portable_echo"
    forge = build_forge(tmp_path, cap_id)

    result = forge.forge(ForgeRequest(cap_id, "echo text and report its length", "author"))

    assert result.isolation_report["passed"] is True
    assert result.isolation_report["case_count"] == 2
    assert Path(result.source_path).read_text() == capability_source(cap_id)
    assert Path(result.artifact_path).is_file()
    with zipfile.ZipFile(result.artifact_path) as archive:
        assert set(archive.namelist()) == {
            "capability.py", "manifest.json", "acceptance.json", "attestation.json",
        }


def test_forge_rejects_candidate_that_only_claims_success(tmp_path):
    cap_id = "lying_echo"
    forge = build_forge(tmp_path, cap_id, wrong=True)

    with pytest.raises(SkillForgeError, match="behavioral verification failed"):
        forge.forge(ForgeRequest(cap_id, "echo text", "author"))

    assert not (tmp_path / "workspace" / "core" / "capabilities" / cap_id).exists()
    assert not list((tmp_path / "workspace" / ".identity_forge").glob("**/*.idcap"))


def test_forge_rejects_undeclared_process_execution_before_running(tmp_path):
    cap_id = "unsafe_process"
    designer = StaticDesigner(cap_id)
    source = capability_source(cap_id).replace(
        "from typing import Any", "from typing import Any\nimport subprocess"
    )

    class UnsafeAuthor:
        def author(self, request, identity=None):
            return ForgeProposal(source=source, manifest=manifest(cap_id))

    forge = SkillForge(tmp_path / "workspace", author=UnsafeAuthor(), test_designer=designer)
    with pytest.raises(SkillForgeError, match="process:execute"):
        forge.forge(ForgeRequest(cap_id, "run a process", "author"))


def test_loader_reaudits_source_and_rejects_internal_runtime_import():
    cap_id = "unsafe_internal"
    source = capability_source(cap_id).replace(
        "from typing import Any", "from typing import Any\nfrom core.executive import ExecutiveRuntime"
    )

    with pytest.raises(ValueError, match="outside the capability API"):
        capability_class_from_source(
            source,
            cap_id,
            "0" * 64,
            allowed_permissions={"public"},
            allowed_dependencies=set(),
        )


def test_artifact_installs_on_another_identity_and_survives_fresh_registry(tmp_path):
    cap_id = "transfer_echo"
    forge = build_forge(tmp_path, cap_id)
    forged = forge.forge(ForgeRequest(cap_id, "echo text", "forger"))
    storage = JSONFileBackend(root_dir=str(tmp_path / "identity-store"))
    first_registry = CapabilityRegistry(storage)

    installed = forge.install_artifact(
        forged.artifact_path,
        identity_id="receiver",
        capability_registry=first_registry,
        storage=storage,
    )
    observed = first_registry.call("receiver", f"{cap_id}.echo", text="transferred")

    assert installed.id == cap_id
    assert observed.success is True
    assert observed.data == {"echo": "transferred", "length": 11}

    fresh_registry = CapabilityRegistry(storage)
    reused = fresh_registry.call("receiver", f"{cap_id}.echo", text="after restart")
    assert reused.success is True
    assert reused.data == {"echo": "after restart", "length": 13}


def test_tampered_artifact_is_not_installed(tmp_path):
    cap_id = "tamper_echo"
    forge = build_forge(tmp_path, cap_id)
    forged = forge.forge(ForgeRequest(cap_id, "echo text", "forger"))
    with zipfile.ZipFile(forged.artifact_path, "r") as archive:
        entries = {name: archive.read(name) for name in archive.namelist()}
    entries["capability.py"] = b"tampered"
    with zipfile.ZipFile(forged.artifact_path, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    storage = JSONFileBackend(root_dir=str(tmp_path / "store"))

    with pytest.raises(ValueError, match="attestation"):
        forge.install_artifact(
            forged.artifact_path,
            identity_id="receiver",
            capability_registry=CapabilityRegistry(storage),
            storage=storage,
        )
    assert storage.load("receiver", "capabilities") is None


def test_explicit_missing_ability_is_inferred_without_domain_hardcoding():
    from core.prometheus.stages.need_detector import detect_need_from_input

    need = detect_need_from_input("I would like to be able to talk to you verbally")

    assert need is not None
    assert need.suggested_capability_ids == ["talk_verbally"]
    assert need.original_request.endswith("verbally")
