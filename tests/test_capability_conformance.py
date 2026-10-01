"""Executable conformance checks for every advertised capability and skill."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import core.capabilities  # noqa: F401 - register every built-in capability
from core.capabilities.registry import CapabilityRegistry, lookup
from runtime.persistence import JSONFileBackend


ROOT = Path(__file__).resolve().parents[1]
MARKETPLACE_INDEX = ROOT / "registry" / "capabilities" / "index.json"
NETWORK_CAPABILITIES = {"github", "weather", "web", "browser"}


def _marketplace_entries() -> list[dict]:
    return json.loads(MARKETPLACE_INDEX.read_text())["capabilities"]


def _grant_all_required_scopes(
    registry: CapabilityRegistry, identity_id: str
) -> None:
    grants = []
    for capability in registry.list(identity_id):
        for skill in capability.skills():
            if skill.permission not in ("", "public", "local"):
                grants.append(
                    {
                        "capability": capability.id,
                        "permission": skill.permission,
                    }
                )
    registry._storage.save(
        identity_id,
        "capability.permissions",
        {"grants": grants},
    )


def _install_marketplace(
    registry: CapabilityRegistry, identity_id: str, workspace: Path
) -> None:
    for entry in _marketplace_entries():
        config = None
        if entry["id"] in {"filesystem", "file_tools"}:
            config = {"allowed_roots": [str(workspace)]}
        registry.install(identity_id, entry["id"], config=config)


def test_marketplace_only_advertises_registered_conformant_capabilities():
    entries = _marketplace_entries()
    assert len(entries) == 22
    assert len({entry["id"] for entry in entries}) == len(entries)

    for entry in entries:
        capability = lookup(entry["id"])()
        manifest_path = ROOT / "registry" / "capabilities" / entry["id"] / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest_skills = {
            skill["name"]: skill.get("permission", "public")
            for skill in manifest["skills"]
        }
        runtime_skills = {
            skill.name: skill.permission for skill in capability.skills()
        }

        assert entry["url"] == f"{entry['id']}/manifest.json"
        assert entry["version"] == manifest["version"] == capability.version
        assert entry["skills"] == len(runtime_skills)
        assert manifest["id"] == entry["id"]
        assert manifest_skills == runtime_skills
        for skill in capability.skills():
            assert skill.input_schema["type"] == "object"
            assert skill.input_schema["additionalProperties"] is False


def test_fresh_identity_installs_all_capabilities_and_reloads_them(tmp_path):
    store_path = tmp_path / "store"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    identity_id = "blank-capability-conformance"
    first = CapabilityRegistry(JSONFileBackend(root_dir=str(store_path)))

    assert first.list(identity_id) == []
    assert first.all_skills(identity_id) == []
    _install_marketplace(first, identity_id, workspace)
    expected = [entry["id"] for entry in _marketplace_entries()]
    assert [capability.id for capability in first.list(identity_id)] == expected

    restarted = CapabilityRegistry(JSONFileBackend(root_dir=str(store_path)))
    assert [capability.id for capability in restarted.list(identity_id)] == expected
    result = restarted.call(
        identity_id, "datetime.now", tz_name="America/Chicago"
    )
    assert result.success is True
    assert result.data["timezone"] == "America/Chicago"


def test_analysis_capabilities_report_absent_benchmarks_as_observed_no_data(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    registry = CapabilityRegistry(JSONFileBackend(root_dir=str(tmp_path / "store")))
    registry.install("tester", "code_review")
    registry.install("tester", "repo_health")

    regressions = registry.call("tester", "code_review.check_regressions")
    trend = registry.call("tester", "repo_health.analyze_benchmark_trend")

    assert regressions.success is True
    assert regressions.data["status"] == "no_data"
    assert regressions.data["regressions"] == []
    assert trend.success is True
    assert trend.data == {"trend_files_found": 0, "status": "no_data"}


def test_registry_publish_refuses_to_replace_existing_manifest(tmp_path):
    registry_root = tmp_path / "registry"
    capability_dir = registry_root / "capabilities" / "existing"
    capability_dir.mkdir(parents=True)
    manifest = capability_dir / "manifest.json"
    original_manifest = '{"id": "existing", "version": "1.0.0"}'
    manifest.write_text(original_manifest)
    (registry_root / "index.json").write_text(json.dumps({
        "capabilities": [{"id": "existing", "version": "1.0.0"}],
    }))

    manager = lookup("registry_manager")()
    manager._registry_path = lambda: str(registry_root)  # type: ignore[method-assign]
    result = manager.call(
        "registry_manager.publish_capability",
        cap_id="existing",
        name="Replacement",
        version="2.0.0",
    )

    assert result.success is False
    assert result.data["conflict"] is True
    assert manifest.read_text() == original_manifest
    index = json.loads((registry_root / "index.json").read_text())
    assert index["capabilities"][0]["version"] == "1.0.0"


def test_registry_manager_lists_and_resolves_authoritative_marketplace(tmp_path):
    registry = CapabilityRegistry(
        JSONFileBackend(root_dir=str(tmp_path / "store"))
    )
    identity_id = "marketplace-registry-manager"
    registry.install(identity_id, "registry_manager")

    listed = registry.call(identity_id, "registry_manager.list_capabilities")
    assert listed.success is True
    expected_ids = [entry["id"] for entry in _marketplace_entries()]
    assert [entry["id"] for entry in listed.data["capabilities"]] == expected_ids
    assert listed.data["count"] == len(expected_ids)

    registry.grant(identity_id, "registry_manager", "capability:manage")
    resolved = registry.call(
        identity_id,
        "registry_manager.install_capability",
        cap_id="architecture_analysis",
    )
    assert resolved.success is True
    assert resolved.data["cap_id"] == "architecture_analysis"
    assert resolved.data["status"] == "ready_to_install"


def test_registry_manager_falls_back_to_legacy_root_index(tmp_path):
    registry_root = tmp_path / "legacy-registry"
    registry_root.mkdir()
    (registry_root / "index.json").write_text(
        json.dumps(
            {
                "capabilities": [
                    {
                        "id": "legacy_demo",
                        "name": "Legacy Demo",
                        "version": "1.0.0",
                        "description": "Root-index compatibility fixture",
                        "skills": [],
                    }
                ]
            }
        )
    )

    manager = lookup("registry_manager")()
    manager._registry_path = lambda: str(registry_root)  # type: ignore[method-assign]

    listed = manager.call("registry_manager.list_capabilities")

    assert listed.success is True
    assert listed.data["count"] == 1
    assert listed.data["capabilities"][0]["id"] == "legacy_demo"


def test_registry_manager_loads_version_1_state_with_compatible_skills(tmp_path):
    storage = JSONFileBackend(root_dir=str(tmp_path / "store"))
    identity_id = "registry-manager-v1-upgrade"
    storage.save(
        identity_id,
        CapabilityRegistry.CAP_NAMESPACE,
        {
            "installed": [
                {"id": "registry_manager", "version": "1.0.0", "config": {}}
            ]
        },
    )

    registry = CapabilityRegistry(storage)
    manager = registry.get(identity_id, "registry_manager")

    assert manager is not None
    assert manager.version == "1.1.0"
    assert {skill.name for skill in manager.skills()} == {
        "registry_manager.list_capabilities",
        "registry_manager.publish_capability",
        "registry_manager.install_capability",
    }
    result = registry.call(identity_id, "registry_manager.list_capabilities")
    assert result.success is True


def test_every_local_marketplace_skill_executes_through_gateway(tmp_path):
    store_path = tmp_path / "store"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sample = workspace / "sample.txt"
    sample.write_text("IdentityOS evidence")
    identity_id = "local-capability-conformance"
    registry = CapabilityRegistry(JSONFileBackend(root_dir=str(store_path)))
    _install_marketplace(registry, identity_id, workspace)
    _grant_all_required_scopes(registry, identity_id)

    registry_root = tmp_path / "registry"
    (registry_root / "capabilities").mkdir(parents=True)
    (registry_root / "index.json").write_text(
        json.dumps({"capabilities": []})
    )
    registry_manager = registry.get(identity_id, "registry_manager")
    assert registry_manager is not None
    registry_manager._registry_path = lambda: str(registry_root)  # type: ignore[method-assign]

    from core.embodiment import CapabilityDeviceAdapter, EmbodimentHub
    from core.executive.engine import ExecutiveRuntime, register_executive
    from core.executive.models import Evidence, Task, TaskStatus, TaskStep, TaskStepStatus
    from core.procedures import HeldOutExample, ProcedureLearner

    executive = ExecutiveRuntime(storage=registry._storage, capability_registry=registry)
    register_executive(executive)
    embodiment_hub = EmbodimentHub(registry._storage, executive)
    executive.embodiment_hub = embodiment_hub
    embodiment_hub.attach(CapabilityDeviceAdapter(
        registry,
        "command_exec",
        device_id="conformance_desktop",
        kind="desktop",
        actions=["run"],
    ))
    embodiment_hub.authorize(identity_id, "conformance_desktop", ["run"])
    procedure_task = Task(
        task_id="conformance-procedure-task",
        goal="write and validate",
        identity_id=identity_id,
        status=TaskStatus.COMPLETED,
        steps=[
            TaskStep(
                action="write_file",
                description="Write",
                params={"path": str(workspace / "training.py"), "content": "answer = 1"},
                status=TaskStepStatus.COMPLETED,
                evidence=[Evidence("write_file", "written", "observed", True)],
            ),
            TaskStep(
                action="validate_syntax",
                description="Validate",
                params={"path": str(workspace / "training.py")},
                status=TaskStepStatus.COMPLETED,
                evidence=[Evidence("validate_syntax", "valid", "observed", True)],
            ),
        ],
    )
    executive.store.save(procedure_task)
    ProcedureLearner(registry._storage).register_held_out_suite(
        identity_id,
        "conformance-suite",
        [
            HeldOutExample(
                "case-one",
                {"path": str(workspace / "one.py"), "content": "answer = 2"},
                (
                    {"action": "write_file", "params": {"path": str(workspace / "one.py"), "content": "answer = 2"}},
                    {"action": "validate_syntax", "params": {"path": str(workspace / "one.py")}},
                ),
            ),
            HeldOutExample(
                "case-two",
                {"path": str(workspace / "two.py"), "content": "answer = 3"},
                (
                    {"action": "write_file", "params": {"path": str(workspace / "two.py"), "content": "answer = 3"}},
                    {"action": "validate_syntax", "params": {"path": str(workspace / "two.py")}},
                ),
            ),
        ],
    )

    generated_interface = """
class DemoCapability(Capability):
    id = "demo"
    _SKILLS = []
    def install(self, identity_id, storage): pass
    def uninstall(self, identity_id, storage): pass
    def prompts(self, identity_id): return []
    def skills(self): return []
    def call(self, skill_name, **params): return {}
"""
    write_target = workspace / "written.txt"
    directory_target = workspace / "created"
    invocations = {
        "calc.evaluate": {"expression": "6 * 7"},
        "calc.convert": {"value": 10, "from_unit": "km", "to_unit": "miles"},
        "calc.conversions": {},
        "datetime.now": {"tz_name": "UTC"},
        "datetime.convert": {"dt_str": "2026-08-31 12:00:00", "from_tz": "UTC", "to_tz": "CST"},
        "datetime.diff": {"date1": "2026-08-01", "date2": "2026-08-31"},
        "datetime.zones": {},
        "filesystem.list_dir": {"path": str(workspace)},
        "filesystem.read_file": {"path": str(sample)},
        "filesystem.file_info": {"path": str(sample)},
        "text.stats": {"text": "one two three"},
        "text.keywords": {"text": "identity runtime identity evidence", "top_n": 3},
        "text.extract_pattern": {"text": "support@example.com", "pattern": "emails"},
        "text.split": {"text": "one two three four", "method": "tokens", "chunk_size": 2},
        "system_info.os": {},
        "system_info.disk": {"path": str(workspace)},
        "system_info.cpu": {},
        "architecture_analysis.analyze_module": {"module_name": "core/capabilities/base.py"},
        "architecture_analysis.detect_coupling": {},
        "architecture_analysis.evaluate_separation": {},
        "architecture_analysis.identify_weaknesses": {},
        "architecture_analysis.produce_report": {},
        "code_review.review_pr": {"title": "Conformance review"},
        "code_review.check_regressions": {},
        "code_review.verify_separation": {},
        "code_review.detect_technical_debt": {},
        "code_review.assess_readiness": {},
        "changelog_gen.generate": {"from_ref": "HEAD", "to_ref": "HEAD"},
        "changelog_gen.detect_breaking": {"from_ref": "HEAD", "to_ref": "HEAD"},
        "changelog_gen.categorize": {"from_ref": "HEAD", "to_ref": "HEAD"},
        "changelog_gen.format_release": {"version": "0.0.0", "from_ref": "HEAD", "to_ref": "HEAD"},
        "repo_health.assess_code_quality": {},
        "repo_health.check_test_health": {},
        "repo_health.evaluate_documentation": {},
        "repo_health.analyze_benchmark_trend": {},
        "repo_health.produce_report": {},
        "dependency_graph.analyze_module_deps": {"module_path": "core/capabilities/base.py"},
        "dependency_graph.detect_cycles": {},
        "dependency_graph.map_capability_deps": {},
        "dependency_graph.visualize": {"format": "text"},
        "file_tools.write_file": {"path": str(write_target), "content": "first"},
        "file_tools.append_file": {"path": str(write_target), "content": " second"},
        "file_tools.create_directory": {"path": str(directory_target)},
        "skill_validator.validate_syntax": {"code": "answer = 42"},
        "skill_validator.check_capability_interface": {"code": generated_interface},
        "registry_manager.list_capabilities": {},
        "registry_manager.publish_capability": {"cap_id": "verified_demo", "name": "Verified Demo", "skills": [{"name": "verified_demo.run", "description": "Verified", "permission": "public"}]},
        "registry_manager.install_capability": {"cap_id": "verified_demo"},
        "task_planner.plan_and_execute": {"goal": "list capabilities", "steps": [{"action": "list_capabilities", "params": {}, "description": "List capabilities"}]},
        "command_exec.run": {"command": "true", "timeout": 5},
        "procedure_learning.list": {},
        "procedure_learning.learn": {
            "task_id": procedure_task.task_id,
            "procedure_id": "conformance_writer",
            "bindings": {
                "path": str(workspace / "training.py"),
                "content": "answer = 1",
            },
            "held_out_suite_id": "conformance-suite",
        },
        "reflex.list": {},
        "reflex.register": {
            "reflex_id": "conformance_reflex",
            "procedure_id": "conformance_writer",
            "trigger_template": "write {content} to {path}",
        },
        "reflex.execute": {
            "utterance": f"write answer = 4 to {workspace / 'reflex.py'}",
        },
        "reflex.reconcile": {"run_id": "filled-after-dispatch"},
        "embodiment.list_devices": {},
        "embodiment.start_task": {
            "goal": "execute a conformance command on the authorized desktop",
            "steps": [{
                "device_id": "conformance_desktop",
                "action": "run",
                "params": {"command": "/bin/true", "timeout": 5},
            }],
        },
        "embodiment.task_status": {"task_id": "filled-after-start"},
    }
    expected_skills = {
        skill.name
        for capability in registry.list(identity_id)
        if capability.id not in NETWORK_CAPABILITIES
        for skill in capability.skills()
    }
    assert set(invocations) == expected_skills

    failures = {}
    reflex_run_id = None
    embodiment_task_id = None
    for skill_name, params in invocations.items():
        if skill_name == "reflex.reconcile":
            assert reflex_run_id is not None
            params = {"run_id": reflex_run_id}
        if skill_name == "embodiment.task_status":
            assert embodiment_task_id is not None
            for _ in range(20):
                executive.process_ready(identity_id, max_steps=10)
                if executive.get_task(identity_id, embodiment_task_id).status.value == "completed":
                    break
            params = {"task_id": embodiment_task_id}
        result = registry.call(identity_id, skill_name, **params)
        if skill_name == "reflex.execute" and result.success:
            reflex_run_id = result.data["run_id"]
        if skill_name == "embodiment.start_task" and result.success:
            embodiment_task_id = result.data["task_id"]
        if not result.success:
            failures[skill_name] = result.error
        assert result.data is not None
    assert failures == {}
    assert write_target.read_text() == "first second"
    assert directory_target.is_dir()
    assert executive.get_task(identity_id, embodiment_task_id).status.value == "completed"
    executive.shutdown()


@pytest.mark.network
@pytest.mark.parametrize(
    ("skill_name", "params"),
    [
        ("github.search_repositories", {"query": "IdentityOS"}),
        ("github.get_repository", {"owner": "lacebx", "repo": "IdentityOS"}),
        ("github.review_pull_request", {"owner": "lacebx", "repo": "IdentityOS", "number": 75}),
        ("github.find_beginner_issue", {"owner": "lacebx", "repo": "IdentityOS"}),
        ("github.summarize_release", {"owner": "lacebx", "repo": "IdentityOS"}),
        ("github.list_commits", {"owner": "lacebx", "repo": "IdentityOS"}),
        ("github.list_branches", {"owner": "lacebx", "repo": "IdentityOS"}),
        ("weather.current", {"location": "Chicago"}),
        ("weather.forecast", {"location": "Chicago", "days": 2}),
        ("web.fetch", {"url": "https://example.com"}),
        ("web.extract", {"url": "https://example.com"}),
    ],
)
def test_every_network_marketplace_skill_executes_live(tmp_path, skill_name, params):
    registry = CapabilityRegistry(
        JSONFileBackend(root_dir=str(tmp_path / "store"))
    )
    capability_id = skill_name.split(".", 1)[0]
    registry.install("network-conformance", capability_id)

    result = registry.call("network-conformance", skill_name, **params)
    assert result.success is True, result.error
    assert result.data is not None
