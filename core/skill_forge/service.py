"""Skill Forge authoring, audit, isolation, packaging, and installation."""

from __future__ import annotations

import json
import os
import re
import subprocess  # nosec B404
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Protocol

from .artifacts import package_record, payload_digest, write_artifact
from .audit import audit_source
from .loader import PACKAGE_NAMESPACE, capability_class_from_source
from .models import AcceptanceCase, ForgeProposal, ForgeRequest, ForgeResult

# The subprocess import is intentional: only the fixed isolated-runner command
# in ``run_isolated`` is launched.


class SkillForgeError(RuntimeError):
    """Raised when a candidate fails a forge invariant."""


class CapabilityAuthor(Protocol):
    def author(self, request: ForgeRequest, identity: Any = None) -> ForgeProposal:
        """Produce implementation source and manifest without seeing hidden cases."""


class CapabilityTestDesigner(Protocol):
    def design(self, request: ForgeRequest, identity: Any = None) -> list[AcceptanceCase]:
        """Derive black-box behavioral cases independently of candidate source."""


def _extract_json(raw: str) -> dict[str, Any]:
    text = (raw or "").strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL | re.IGNORECASE)
    if fenced:
        text = fenced.group(1)
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise SkillForgeError("authoring model did not return a JSON object")
        try:
            value = json.loads(text[start:end + 1])
        except json.JSONDecodeError as exc:
            raise SkillForgeError(f"authoring model returned invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise SkillForgeError("authoring model response must be a JSON object")
    return value


class ModelCapabilityTestDesigner:
    """Use the runtime model to write black-box cases before source is authored."""

    def __init__(self, adapter: Any) -> None:
        self.adapter = adapter

    def design(self, request: ForgeRequest, identity: Any = None) -> list[AcceptanceCase]:
        raw = self.adapter.generate(
            context=(
                "You are the independent acceptance-test designer for IdentityOS Skill Forge. "
                "Return JSON only: {\"cases\":[{\"name\":str,\"skill\":str,"
                "\"params\":object,\"expected\":object,\"expect_success\":bool,"
                "\"grants\":[str]}]}. Design at least two black-box cases, including "
                "different inputs when the skill accepts input. Do not write implementation code. "
                "Expected values must be exact, observable output subsets. Use only permissions "
                f"explicitly allowed here: {list(request.allowed_permissions)}. The capability id "
                f"is {request.capability_id}. Allowed dependencies: "
                f"{list(request.allowed_dependencies)}."
            ),
            user_input=request.goal,
            identity=identity,
            temperature=0.1,
            max_tokens=1800,
        )
        parsed = _extract_json(str(raw))
        cases = parsed.get("cases")
        if not isinstance(cases, list):
            raise SkillForgeError("test designer did not return a cases list")
        return [AcceptanceCase.from_dict(item) for item in cases]


class ModelCapabilityAuthor:
    """Use the runtime model to implement a capability against the public goal."""

    def __init__(self, adapter: Any) -> None:
        self.adapter = adapter

    def author(self, request: ForgeRequest, identity: Any = None) -> ForgeProposal:
        raw = self.adapter.generate(
            context=(
                "You implement one IdentityOS capability. Return JSON only with keys source and "
                "manifest. source must define exactly one concrete Capability subclass decorated "
                "with @register, with id matching the requested id. Every call returns a "
                "CapabilityResult. Skills require explicit object input schemas and at least one "
                "harmless verification_params case. Do real work; never return a synthetic "
                "completed/success claim. Do not include tests. The manifest must contain id, "
                "name, version, author, description, permissions (list), dependencies (list), "
                "and skills (list). "
                f"Permitted risk scopes are only: {list(request.allowed_permissions)}. "
                f"Permitted third-party dependencies are only: {list(request.allowed_dependencies)}."
            ),
            user_input=f"Capability id: {request.capability_id}\nBehavior required: {request.goal}",
            identity=identity,
            temperature=0.1,
            max_tokens=5000,
        )
        parsed = _extract_json(str(raw))
        source = parsed.get("source")
        manifest = parsed.get("manifest")
        if not isinstance(source, str) or not isinstance(manifest, dict):
            raise SkillForgeError("author did not return string source and object manifest")
        return ForgeProposal(source=source, manifest=manifest)


class SkillForge:
    """Forge only behavior that survives independent isolated verification."""

    def __init__(
        self,
        workspace_root: str | Path,
        *,
        author: CapabilityAuthor,
        test_designer: CapabilityTestDesigner,
        timeout_seconds: float = 20.0,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.author = author
        self.test_designer = test_designer
        self.timeout_seconds = max(1.0, float(timeout_seconds))

    @property
    def artifact_root(self) -> Path:
        return self.workspace_root / ".identity_forge" / "artifacts"

    def forge(self, request: ForgeRequest, *, identity: Any = None) -> ForgeResult:
        cases = list(request.acceptance_cases) or self.test_designer.design(request, identity)
        self._validate_cases(request, cases)
        proposal = self.author.author(request, identity)
        manifest = self._validate_proposal(request, proposal, cases)
        source = proposal.source
        files = {
            "capability.py": source.encode(),
            "manifest.json": _canonical_manifest(manifest),
            "acceptance.json": _canonical_acceptance(cases),
        }
        digest = payload_digest(files)
        report = self.run_isolated(
            capability_id=request.capability_id,
            source=source,
            cases=cases,
            artifact_sha256=digest,
            manifest=manifest,
        )
        if not report.get("passed"):
            raise SkillForgeError(
                "independent behavioral verification failed: "
                + str(report.get("error") or report.get("cases"))
            )

        artifact_path = self.artifact_root / request.capability_id / f"{digest}.idcap"
        observed_digest = write_artifact(
            artifact_path,
            source=source,
            manifest=manifest,
            acceptance=[case.to_dict() for case in cases],
        )
        if observed_digest != digest:
            raise SkillForgeError("packaged bytes differed from tested bytes")

        source_path = self.workspace_root / "core" / "capabilities" / request.capability_id / "__init__.py"
        if source_path.exists():
            raise SkillForgeError(f"refusing to overwrite existing capability source: {source_path}")
        source_path.parent.mkdir(parents=True, exist_ok=False)
        try:
            source_path.write_text(source, encoding="utf-8")
        except Exception:
            source_path.parent.rmdir()
            raise
        return ForgeResult(
            capability_id=request.capability_id,
            artifact_path=str(artifact_path),
            artifact_sha256=digest,
            source_path=str(source_path),
            manifest=manifest,
            acceptance_cases=tuple(cases),
            isolation_report=report,
        )

    def run_isolated(
        self,
        *,
        capability_id: str,
        source: str,
        cases: list[AcceptanceCase],
        artifact_sha256: str,
        manifest: dict[str, Any],
    ) -> dict[str, Any]:
        payload = {
            "capability_id": capability_id,
            "source": source,
            "acceptance": [case.to_dict() for case in cases],
            "artifact_sha256": artifact_sha256,
            "manifest": manifest,
        }
        started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="identityos-forge-") as isolated:
            payload_path = Path(isolated) / "payload.json"
            payload_path.write_text(json.dumps(payload), encoding="utf-8")
            env = {
                "PATH": os.environ.get("PATH", ""),
                "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
                "PYTHONHASHSEED": "0",
                "TMPDIR": isolated,
            }
            try:
                # argv is fixed; there is no shell or model-supplied executable.
                completed = subprocess.run(  # nosec B603
                    [sys.executable, "-m", "core.skill_forge.runner", str(payload_path)],
                    cwd=isolated,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout_seconds,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                return {
                    "passed": False,
                    "error": f"isolated tests exceeded {self.timeout_seconds:g}s",
                    "duration_ms": round((time.monotonic() - started) * 1000, 3),
                    "stdout": (exc.stdout or "")[-2000:] if isinstance(exc.stdout, str) else "",
                }
        try:
            report = json.loads(completed.stdout.strip().splitlines()[-1])
        except (json.JSONDecodeError, IndexError):
            report = {
                "passed": False,
                "error": "isolated runner did not return a JSON report",
                "stdout": completed.stdout[-2000:],
                "stderr": completed.stderr[-2000:],
            }
        report["exit_code"] = completed.returncode
        report["wall_duration_ms"] = round((time.monotonic() - started) * 1000, 3)
        if completed.returncode != 0:
            report["passed"] = False
        return report

    def install_artifact(
        self,
        artifact_path: str | Path,
        *,
        identity_id: str,
        capability_registry: Any,
        storage: Any,
    ) -> Any:
        """Verify, persist, and install a tested artifact for another identity."""
        record = package_record(artifact_path)
        manifest = record["manifest"]
        cap_id = str(manifest.get("id", ""))
        cases = [AcceptanceCase.from_dict(item) for item in record["acceptance"]]
        issues = audit_source(
            str(record["source"]),
            set(manifest.get("permissions", [])),
            set(manifest.get("dependencies", [])),
        )
        if issues:
            raise SkillForgeError("artifact source audit failed: " + "; ".join(issues))
        report = self.run_isolated(
            capability_id=cap_id,
            source=str(record["source"]),
            cases=cases,
            artifact_sha256=str(record["artifact_sha256"]),
            manifest=manifest,
        )
        if not report.get("passed"):
            raise SkillForgeError("artifact failed installation-time behavioral verification")
        capability_class_from_source(
            str(record["source"]),
            cap_id,
            str(record["artifact_sha256"]),
            allowed_permissions=set(manifest.get("permissions", [])),
            allowed_dependencies=set(manifest.get("dependencies", [])),
        )
        raw = storage.load(identity_id, PACKAGE_NAMESPACE) or {}
        packages = dict(raw.get("packages") or {})
        packages[cap_id] = record
        storage.save(identity_id, PACKAGE_NAMESPACE, {"packages": packages})
        try:
            return capability_registry.install(identity_id, cap_id)
        except Exception:
            packages.pop(cap_id, None)
            storage.save(identity_id, PACKAGE_NAMESPACE, {"packages": packages})
            raise

    def _validate_cases(self, request: ForgeRequest, cases: list[AcceptanceCase]) -> None:
        if len(cases) < 2:
            raise SkillForgeError("at least two independent behavioral cases are required")
        for case in cases:
            if not case.skill.startswith(request.capability_id + "."):
                raise SkillForgeError(
                    f"case '{case.name}' targets a different capability: {case.skill}"
                )
            undeclared = set(case.grants) - set(request.allowed_permissions)
            if undeclared:
                raise SkillForgeError(
                    f"case '{case.name}' requests permissions outside the forge grant: "
                    + ", ".join(sorted(undeclared))
                )

    def _validate_proposal(
        self,
        request: ForgeRequest,
        proposal: ForgeProposal,
        cases: list[AcceptanceCase],
    ) -> dict[str, Any]:
        manifest = dict(proposal.manifest)
        required = {"id", "name", "version", "author", "description", "permissions", "skills"}
        missing = sorted(required - set(manifest))
        if missing:
            raise SkillForgeError("manifest missing fields: " + ", ".join(missing))
        if manifest.get("id") != request.capability_id:
            raise SkillForgeError("manifest id does not match forge request")
        permissions = manifest.get("permissions")
        if not isinstance(permissions, list) or not all(isinstance(item, str) for item in permissions):
            raise SkillForgeError("manifest permissions must be a list of strings")
        excess = set(permissions) - {"public", "local", *request.allowed_permissions}
        if excess:
            raise SkillForgeError(
                "manifest requests permissions outside the forge grant: "
                + ", ".join(sorted(excess))
            )
        dependencies = manifest.get("dependencies", [])
        if not isinstance(dependencies, list) or not all(isinstance(item, str) for item in dependencies):
            raise SkillForgeError("manifest dependencies must be a list of strings")
        excess_dependencies = set(dependencies) - set(request.allowed_dependencies)
        if excess_dependencies:
            raise SkillForgeError(
                "manifest requests dependencies outside the forge allowlist: "
                + ", ".join(sorted(excess_dependencies))
            )
        skills = manifest.get("skills")
        if not isinstance(skills, list) or not skills:
            raise SkillForgeError("manifest must declare at least one skill")
        declared_skills = {
            item.get("name") for item in skills if isinstance(item, dict)
        }
        case_skills = {case.skill for case in cases}
        if case_skills and not case_skills.issubset(declared_skills):
            raise SkillForgeError("manifest does not declare every acceptance-tested skill")
        issues = audit_source(
            proposal.source,
            set(request.allowed_permissions),
            set(request.allowed_dependencies),
        )
        if issues:
            raise SkillForgeError("source audit failed: " + "; ".join(issues))
        return manifest


def _canonical_manifest(manifest: dict[str, Any]) -> bytes:
    return (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _canonical_acceptance(cases: list[AcceptanceCase]) -> bytes:
    return (
        json.dumps([case.to_dict() for case in cases], sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode()
