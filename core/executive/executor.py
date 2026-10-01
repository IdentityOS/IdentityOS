"""
executor.py — Generic step executor.

Executes one ``TaskStep`` at a time, producing evidence for every step.
The executor knows generic lifecycle actions, never individual capability
names. It also passes through planner-style file actions so plans produced by
the planner execute with the same persistence and evidence guarantees.

Every handler returns ``(success, result_dict, evidence_list)``.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from core.executive.models import Evidence, ReplayPolicy, Task, TaskStep
from core.executive.verification import (
    capability_module_path,
    verification_probe,
    verify_capability,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


@dataclass
class ExecutionContext:
    identity_id: str
    capability_registry: Any
    storage: Any
    runtime: Any = None
    skill_forge: Any = None
    embodiment_hub: Any = None


class StepError(Exception):
    """Raised when a step fails permanently (after retries exhausted)."""

    def __init__(self, message: str, evidence: Optional[list] = None):
        super().__init__(message)
        self.evidence = evidence or []


def execute_step(task: Task, step: TaskStep, ctx: ExecutionContext) -> tuple[bool, dict, list]:
    """Execute a single step.

    Returns ``(success, result, evidence)``. Raises ``StepError`` only when
    the step should be treated as a hard failure (not retried).
    """
    handler = _HANDLERS.get(step.action)
    if handler is None:
        return (False, {}, [Evidence(
            step=step.action, label="unknown_action",
            detail=f"No handler for action: {step.action}", success=False,
        )])
    return handler(task, step, ctx)


def replay_policy_for_action(action: str) -> ReplayPolicy:
    """Return the declared crash-replay contract for an executor action.

    Unknown and externally mutating actions deliberately require manual
    reconciliation. Adding a new handler without declaring a policy therefore
    fails safe instead of silently duplicating a possible side effect.
    """

    return _REPLAY_POLICIES.get(action, ReplayPolicy.BLOCK)


def rollback_acquisition(task: Task, ctx: ExecutionContext) -> list[Evidence]:
    """Compensate a newly created installation after terminal verification failure.

    Never uninstall a capability that pre-dated this task. The evidence is
    returned to the engine so rollback failure is visible rather than hidden.
    """
    install_step = task.step_by_id("install")
    if (
        install_step is None
        or install_step.result.get("already_installed")
        or not install_step.result.get("installed")
        or ctx.capability_registry is None
    ):
        return []
    cap_id = str(install_step.result["installed"])
    try:
        ctx.capability_registry.uninstall(ctx.identity_id, cap_id)
        install_step.result["rolled_back"] = True
        return [Evidence(
            step="rollback",
            label="acquisition_rolled_back",
            detail=f"removed newly installed {cap_id} after lifecycle failure",
            success=True,
            data={"capability": cap_id},
        )]
    except Exception as exc:
        install_step.result["rollback_failed"] = True
        return [Evidence(
            step="rollback",
            label="acquisition_rollback_failed",
            detail=f"failed to remove {cap_id}: {exc}",
            success=False,
            data={"capability": cap_id, "error": str(exc)},
        )]


# ── Generic acquisition handlers ────────────────────────────────────────

def _registry_search(task: Task, step: TaskStep, ctx: ExecutionContext) -> tuple[bool, dict, list]:
    cap = step.params.get("capability", "")
    found, candidate = _search_marketplace(cap)
    manifest = _load_capability_manifest(candidate) if candidate else {}
    evidence = [Evidence(
        step=step.action, label="registry_search",
        detail=f"found={found} candidate={candidate or 'none'}", success=True,
        data={
            "capability": cap,
            "found": found,
            "candidate": candidate,
            "version": manifest.get("version"),
            "author": manifest.get("author"),
        },
    )]
    return (True, {
        "found": found,
        "candidate": candidate,
        "manifest": manifest,
    }, evidence)


def _trust(task: Task, step: TaskStep, ctx: ExecutionContext) -> tuple[bool, dict, list]:
    """Apply Prometheus' marketplace trust policy before installation."""
    cap = _resolve_capability(task, step)
    manifest = _load_capability_manifest(cap)
    if not manifest:
        return (False, {"trusted": False}, [Evidence(
            step=step.action,
            label="trust_verified",
            detail=f"manifest missing for registry capability {cap}",
            success=False,
            data={"capability": cap},
        )])
    try:
        from core.prometheus.models import AcquisitionMode, RegistryCandidate
        from core.prometheus.stages.trust_verifier import is_trusted, verify_trust

        candidate = RegistryCandidate(
            cap_id=cap,
            name=manifest.get("name", cap),
            version=manifest.get("version", "0.0.0"),
            author=manifest.get("author", "unknown"),
            description=manifest.get("description", ""),
            skills=manifest.get("skills", []),
            permissions=manifest.get("permissions", {}),
            dependencies=manifest.get("dependencies", []),
            manifest_url=f"registry/capabilities/{cap}/manifest.json",
            behaviorally_verified=bool(manifest.get("behaviorally_verified")),
            artifact_sha256=manifest.get("artifact_sha256"),
        )
        score = verify_trust(candidate, mode=AcquisitionMode.AUTOMATIC)
        trusted = is_trusted(candidate, mode=AcquisitionMode.AUTOMATIC)
        return (trusted, {
            "trusted": trusted,
            "score": score,
            "author": candidate.author,
        }, [Evidence(
            step=step.action,
            label="trust_verified",
            detail=f"{cap} trust score={score:.3f} trusted={trusted}",
            success=trusted,
            data={"capability": cap, "score": score, "author": candidate.author},
        )])
    except Exception as exc:
        return (False, {"trusted": False}, [Evidence(
            step=step.action,
            label="trust_verified",
            detail=f"trust verification failed: {exc}",
            success=False,
            data={"capability": cap, "error": str(exc)},
        )])


def _dependencies(task: Task, step: TaskStep, ctx: ExecutionContext) -> tuple[bool, dict, list]:
    cap = _resolve_capability(task, step)
    manifest = _load_capability_manifest(cap)
    required = [str(dep) for dep in manifest.get("dependencies", [])]
    installed = {
        getattr(item, "id", "")
        for item in (ctx.capability_registry.list(ctx.identity_id) if ctx.capability_registry else [])
    }
    missing = [dep for dep in required if dep not in installed]
    ok = not missing
    return (ok, {
        "dependencies": required,
        "missing": missing,
    }, [Evidence(
        step=step.action,
        label="dependencies_resolved",
        detail=(f"all dependencies available for {cap}" if ok else f"missing dependencies: {', '.join(missing)}"),
        success=ok,
        data={"capability": cap, "required": required, "missing": missing},
    )])


def _generate(task: Task, step: TaskStep, ctx: ExecutionContext) -> tuple[bool, dict, list]:
    cap = step.params.get("capability", "")
    path = capability_module_path(cap)
    if path.exists():
        return (False, {"path": str(path), "conflict": True}, [Evidence(
            step=step.action,
            label="generation_blocked_existing_source",
            detail=f"refusing to overwrite existing capability source: {path}",
            success=False,
            data={"path": str(path), "conflict": True},
        )])
    forge = ctx.skill_forge or getattr(ctx.runtime, "skill_forge", None)
    if forge is None:
        return (False, {"forge_unavailable": True}, [Evidence(
            step=step.action,
            label="skill_forge_unavailable",
            detail="no Skill Forge author and independent test designer are configured",
            success=False,
            data={"capability": cap},
        )])
    try:
        from core.skill_forge import ForgeRequest

        identity = ctx.runtime.load(ctx.identity_id) if ctx.runtime is not None else None
        result = forge.forge(ForgeRequest(
            capability_id=cap,
            goal=task.original_request or task.goal,
            identity_id=ctx.identity_id,
            allowed_permissions=tuple(step.params.get("allowed_permissions", [])),
            allowed_dependencies=tuple(step.params.get("allowed_dependencies", [])),
        ), identity=identity)
        report = result.isolation_report
        evidence = [
            Evidence(
                step=step.action,
                label="behavioral_tests_isolated",
                detail=f"{report.get('case_count', 0)} independent cases passed in a clean subprocess",
                success=bool(report.get("passed")),
                data={
                    "case_count": report.get("case_count", 0),
                    "duration_ms": report.get("duration_ms"),
                    "exit_code": report.get("exit_code"),
                },
            ),
            Evidence(
                step=step.action,
                label="artifact_packaged",
                detail=f"packaged tested bytes as {result.artifact_path}",
                success=True,
                data={
                    "artifact_path": result.artifact_path,
                    "artifact_sha256": result.artifact_sha256,
                },
            ),
            Evidence(
                step=step.action,
                label="file_generated",
                detail=f"wrote tested source {result.source_path} ({path.stat().st_size} bytes)",
                success=True,
                data={"path": result.source_path, "bytes": path.stat().st_size},
            ),
        ]
        return (True, result.to_dict(), evidence)
    except Exception as exc:
        return (False, {}, [Evidence(
            step=step.action,
            label="skill_forge_failed",
            detail=f"forge failed: {type(exc).__name__}: {exc}",
            success=False,
            data={"capability": cap, "error": str(exc)},
        )])


def _validate(task: Task, step: TaskStep, ctx: ExecutionContext) -> tuple[bool, dict, list]:
    cap = _resolve_capability(task, step)
    path = capability_module_path(cap)
    evidence: list = []
    ok = True
    try:
        import ast
        ast.parse(path.read_text(encoding="utf-8"))
        evidence.append(Evidence(step=step.action, label="syntax_valid", detail=str(path), success=True))
    except Exception as e:
        ok = False
        evidence.append(Evidence(step=step.action, label="syntax_valid", detail=f"invalid syntax: {e}", success=False, data={"error": str(e)}))
    try:
        from core.capabilities.registry import lookup
        import importlib
        importlib.import_module(f"core.capabilities.{cap}")
        lookup(cap)
        evidence.append(Evidence(step=step.action, label="interface_valid", detail=f"{cap} imports and registers", success=True))
    except Exception as e:
        ok = False
        evidence.append(Evidence(step=step.action, label="interface_valid", detail=f"import/register failed: {e}", success=False, data={"error": str(e)}))
    return (ok, {"valid": ok}, evidence)


def _publish(task: Task, step: TaskStep, ctx: ExecutionContext) -> tuple[bool, dict, list]:
    cap = step.params.get("capability", "")
    try:
        from core.capabilities.registry import lookup
        generated = task.step_by_id("generate")
        generated_result = generated.result if generated is not None else {}
        generated_manifest = generated_result.get("manifest", {})
        rmgmt = lookup("registry_manager")()
        res = rmgmt.call(
            "registry_manager.publish_capability",
            cap_id=cap,
            name=generated_manifest.get("name", cap.replace("_", " ").title()),
            version=generated_manifest.get("version", "1.0.0"),
            description=generated_manifest.get("description", f"Forged capability: {cap}"),
            skills=generated_manifest.get("skills", []),
        )
        success = bool(getattr(res, "success", False)) or "published" in str(getattr(res, "data", {}))
        artifact_destination = None
        artifact_source = generated_result.get("artifact_path")
        if success and artifact_source:
            artifact_destination = _REPO_ROOT / "registry" / "capabilities" / cap / "capability.idcap"
            shutil.copyfile(artifact_source, artifact_destination)
            manifest_path = artifact_destination.parent / "manifest.json"
            published_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            published_manifest["dependencies"] = generated_manifest.get("dependencies", [])
            published_manifest["forge_permissions"] = generated_manifest.get("permissions", [])
            published_manifest["artifact"] = "capability.idcap"
            published_manifest["artifact_sha256"] = generated_result.get("artifact_sha256")
            published_manifest["behaviorally_verified"] = True
            manifest_path.write_text(json.dumps(published_manifest, indent=2) + "\n", encoding="utf-8")
        evidence = [Evidence(
            step=step.action, label="registry_published",
            detail=f"{cap} published ({getattr(res, 'data', {})})", success=success,
            data={
                **(
                    getattr(res, "data", {})
                    if isinstance(getattr(res, "data", {}), dict)
                    else {"result": str(getattr(res, "data", ""))}
                ),
                "artifact": str(artifact_destination) if artifact_destination else None,
                "artifact_sha256": generated_result.get("artifact_sha256"),
            },
        )]
        return (success, {"published": success}, evidence)
    except Exception as e:
        return (False, {}, [Evidence(step=step.action, label="registry_published", detail=f"publish failed: {e}", success=False, data={"error": str(e)})])


def _resolve_capability(task: Task, step: TaskStep) -> str:
    """Resolve the canonical capability id for a step.

    Prefers the marketplace-resolved candidate from the registry_search step
    so steps after search install/verify the *canonical* id even when the
    goal named the capability loosely (e.g. 'command' -> 'command_exec').
    """
    cap = step.params.get("capability", "")
    ref = task.step_by_id("registry_search")
    if ref is not None and ref.result.get("candidate"):
        return ref.result["candidate"]
    return cap


def _install(task: Task, step: TaskStep, ctx: ExecutionContext) -> tuple[bool, dict, list]:
    cap = _resolve_capability(task, step)
    try:
        import importlib
        from core.capabilities.registry import lookup
        importlib.import_module(f"core.capabilities.{cap}")  # fires @register
        lookup(cap)  # must be registered before install
        if ctx.capability_registry is None:
            return (False, {}, [Evidence(step=step.action, label="installed", detail="no capability registry available", success=False)])
        cap_obj = ctx.capability_registry.get(ctx.identity_id, cap)
        already_installed = cap_obj is not None
        if cap_obj is None:
            cap_obj = ctx.capability_registry.install(ctx.identity_id, cap)
        evidence = [Evidence(
            step=step.action, label="installed",
            detail=(
                f"{cap} already installed for {ctx.identity_id}"
                if already_installed
                else f"{cap} installed for {ctx.identity_id}"
            ),
            success=True,
            data={
                "capability": cap,
                "identity_id": ctx.identity_id,
                "skills": [s.name for s in cap_obj.skills()],
                "already_installed": already_installed,
            },
        )]
        return (True, {
            "installed": cap,
            "skills": [s.name for s in cap_obj.skills()],
            "already_installed": already_installed,
        }, evidence)
    except Exception as e:
        return (False, {}, [Evidence(step=step.action, label="installed", detail=f"install failed: {e}", success=False, data={"error": str(e)})])


def _activate(task: Task, step: TaskStep, ctx: ExecutionContext) -> tuple[bool, dict, list]:
    """Prove the installed capability is exposed and authorized for a safe probe."""
    cap_id = _resolve_capability(task, step)
    registry = ctx.capability_registry
    if registry is None:
        return (False, {}, [Evidence(
            step=step.action,
            label="activation_failed",
            detail="no capability registry available",
            success=False,
        )])
    cap = registry.get(ctx.identity_id, cap_id)
    if cap is None:
        return (False, {"activated": False}, [Evidence(
            step=step.action,
            label="activation_failed",
            detail=f"{cap_id} is not installed",
            success=False,
        )])
    probe = verification_probe(cap)
    if probe is None:
        return (False, {
            "activated": False,
            "reason": "no skill declares safe verification parameters",
        }, [Evidence(
            step=step.action,
            label="safe_probe_missing",
            detail=f"{cap_id} has no declared harmless verification probe",
            success=False,
            data={"capability": cap_id},
        )])
    skill, params = probe
    allowed, reason = registry.can(ctx.identity_id, skill.name)
    if not allowed:
        return (False, {
            "blocked": True,
            "resumable": True,
            "block_type": "authorization_required",
            "activated": False,
            "capability": cap_id,
            "skill": skill.name,
            "permission": skill.permission,
            "reason": reason,
        }, [Evidence(
            step=step.action,
            label="authorization_required",
            detail=reason,
            success=False,
            data={
                "capability": cap_id,
                "skill": skill.name,
                "permission": skill.permission,
            },
        )])
    return (True, {
        "activated": True,
        "capability": cap_id,
        "skill": skill.name,
        "params": params,
    }, [Evidence(
        step=step.action,
        label="capability_activated",
        detail=f"{skill.name} is exposed and authorized",
        success=True,
        data={"capability": cap_id, "skill": skill.name, "permission": skill.permission},
    )])


def _invoke(task: Task, step: TaskStep, ctx: ExecutionContext) -> tuple[bool, dict, list]:
    cap_id = _resolve_capability(task, step)
    registry = ctx.capability_registry
    cap = registry.get(ctx.identity_id, cap_id) if registry is not None else None
    probe = verification_probe(cap) if cap is not None else None
    if registry is None or probe is None:
        return (False, {"invoked": False}, [Evidence(
            step=step.action,
            label="safe_probe_invoked",
            detail="installed capability or safe probe unavailable",
            success=False,
            data={"capability": cap_id},
        )])
    skill, params = probe
    try:
        result = registry.call(ctx.identity_id, skill.name, **params)
        ok = bool(getattr(result, "success", False))
        result_evidence = (
            result.to_evidence_dict()
            if hasattr(result, "to_evidence_dict")
            else {"success": ok}
        )
        return (ok, {
            "invoked": ok,
            "skill": skill.name,
            "params": params,
            "result": result_evidence,
        }, [Evidence(
            step=step.action,
            label="safe_probe_invoked",
            detail=f"{skill.name} {'succeeded' if ok else 'failed'} through capability gateway",
            success=ok,
            data={"capability": cap_id, "skill": skill.name, "result": result_evidence},
        )])
    except Exception as exc:
        return (False, {"invoked": False}, [Evidence(
            step=step.action,
            label="safe_probe_invoked",
            detail=f"gateway invocation failed: {exc}",
            success=False,
            data={"capability": cap_id, "skill": skill.name, "error": str(exc)},
        )])


def _persist(task: Task, step: TaskStep, ctx: ExecutionContext) -> tuple[bool, dict, list]:
    cap_id = _resolve_capability(task, step)
    raw = ctx.storage.load(ctx.identity_id, "capabilities") if ctx.storage is not None else None
    entries = raw.get("installed", []) if isinstance(raw, dict) else []
    entry = next((item for item in entries if item.get("id") == cap_id), None)
    ok = entry is not None
    return (ok, {
        "persisted": ok,
        "entry": entry or {},
    }, [Evidence(
        step=step.action,
        label="installation_persisted",
        detail=(f"{cap_id} found in durable capability state" if ok else f"{cap_id} missing from durable capability state"),
        success=ok,
        data={"capability": cap_id, "entry": entry or {}},
    )])


def _reload(task: Task, step: TaskStep, ctx: ExecutionContext) -> tuple[bool, dict, list]:
    cap_id = _resolve_capability(task, step)
    if ctx.storage is None:
        return (False, {"reloaded": False}, [Evidence(
            step=step.action,
            label="capability_reloaded",
            detail="no persistent storage available",
            success=False,
        )])
    try:
        from core.capabilities.registry import CapabilityRegistry

        fresh_registry = CapabilityRegistry(ctx.storage)
        cap = fresh_registry.get(ctx.identity_id, cap_id)
        probe = verification_probe(cap) if cap is not None else None
        allowed, reason = (
            fresh_registry.can(ctx.identity_id, probe[0].name)
            if probe is not None
            else (False, "safe probe unavailable after reload")
        )
        ok = cap is not None and probe is not None and allowed
        return (ok, {
            "reloaded": ok,
            "capability": cap_id,
            "skill": probe[0].name if probe else None,
            "reason": reason,
        }, [Evidence(
            step=step.action,
            label="capability_reloaded",
            detail=(f"{cap_id} reloaded with an authorized probe" if ok else f"reload verification failed: {reason}"),
            success=ok,
            data={"capability": cap_id, "skill": probe[0].name if probe else None},
        )])
    except Exception as exc:
        return (False, {"reloaded": False}, [Evidence(
            step=step.action,
            label="capability_reloaded",
            detail=f"reload failed: {exc}",
            success=False,
            data={"capability": cap_id, "error": str(exc)},
        )])


def _reuse(task: Task, step: TaskStep, ctx: ExecutionContext) -> tuple[bool, dict, list]:
    cap_id = _resolve_capability(task, step)
    if ctx.storage is None:
        return (False, {"reused": False}, [Evidence(
            step=step.action,
            label="capability_reused",
            detail="no persistent storage available",
            success=False,
        )])
    try:
        from core.capabilities.registry import CapabilityRegistry

        fresh_registry = CapabilityRegistry(ctx.storage)
        cap = fresh_registry.get(ctx.identity_id, cap_id)
        probe = verification_probe(cap) if cap is not None else None
        if probe is None:
            raise RuntimeError("safe probe unavailable after reload")
        skill, params = probe
        result = fresh_registry.call(ctx.identity_id, skill.name, **params)
        ok = bool(getattr(result, "success", False))
        result_evidence = (
            result.to_evidence_dict()
            if hasattr(result, "to_evidence_dict")
            else {"success": ok}
        )
        return (ok, {
            "reused": ok,
            "skill": skill.name,
            "params": params,
            "result": result_evidence,
        }, [Evidence(
            step=step.action,
            label="capability_reused",
            detail=f"{skill.name} {'succeeded' if ok else 'failed'} after fresh registry reload",
            success=ok,
            data={"capability": cap_id, "skill": skill.name, "result": result_evidence},
        )])
    except Exception as exc:
        return (False, {"reused": False}, [Evidence(
            step=step.action,
            label="capability_reused",
            detail=f"post-reload invocation failed: {exc}",
            success=False,
            data={"capability": cap_id, "error": str(exc)},
        )])


def _verify(task: Task, step: TaskStep, ctx: ExecutionContext) -> tuple[bool, dict, list]:
    cap = _resolve_capability(task, step)
    evidence = verify_capability(cap, ctx.identity_id, ctx.capability_registry)
    ok = all(e.success for e in evidence)
    return (ok, {"verified": ok, "checks": len(evidence)}, evidence)


def _verify_goal(task: Task, step: TaskStep, ctx: ExecutionContext) -> tuple[bool, dict, list]:
    request = step.params.get("request", "")
    cap = step.params.get("capability", "")
    if not request or ctx.runtime is None:
        return (True, {"retried": False}, [Evidence(step=step.action, label="goal_retry", detail="no original request to retry", success=True)])
    try:
        from runtime.orchestrator import InteractionRequest
        resp = ctx.runtime.process(InteractionRequest(
            identity_id=ctx.identity_id,
            user_input=request,
            session_id=None,
        ))
        output = getattr(resp, "output", "") or ""
        return (True, {"retried": True, "output": output[:500]}, [Evidence(step=step.action, label="goal_retry", detail="original request re-run", success=True, data={"output": output[:200]})])
    except Exception as e:
        return (True, {"retried": False, "error": str(e)}, [Evidence(step=step.action, label="goal_retry", detail=f"retry unavailable: {e}", success=False, data={"error": str(e)})])


def _device_action(task: Task, step: TaskStep, ctx: ExecutionContext) -> tuple[bool, dict, list]:
    """Route an authorized device action through the runtime's embodiment hub."""
    hub = ctx.embodiment_hub or getattr(ctx.runtime, "embodiment_hub", None)
    if hub is None:
        return (False, {
            "blocked": True,
            "resumable": True,
            "block_type": "device_unavailable",
            "reason": "embodiment hub is unavailable",
        }, [Evidence(
            step=step.action,
            label="device_unavailable",
            detail="embodiment hub is unavailable",
            success=False,
        )])
    device_id = str(step.params.get("device_id", ""))
    action = str(step.params.get("device_action", ""))
    params = step.params.get("device_params", {})
    try:
        observation = hub.invoke(
            ctx.identity_id, task.task_id, device_id, action, params,
        )
        ok = bool(observation.get("success"))
        return (ok, observation, [Evidence(
            step=step.action,
            label="device_observation",
            detail=(
                f"observed {device_id}.{action} via {observation.get('source') or 'device adapter'}"
                if ok else f"{device_id}.{action} reported failure: {observation.get('error') or 'unknown'}"
            ),
            success=ok,
            data=observation,
        )])
    except Exception as exc:
        from core.embodiment import DeviceAuthorizationError, DeviceUnavailableError

        if isinstance(exc, (DeviceAuthorizationError, DeviceUnavailableError)):
            block_type = (
                "authorization_required"
                if isinstance(exc, DeviceAuthorizationError)
                else "device_unavailable"
            )
            return (False, {
                "blocked": True,
                "resumable": True,
                "block_type": block_type,
                "reason": str(exc),
                "device_id": device_id,
                "device_action": action,
            }, [Evidence(
                step=step.action,
                label=block_type,
                detail=str(exc),
                success=False,
                data={"device_id": device_id, "device_action": action},
            )])
        return (False, {
            "device_id": device_id,
            "device_action": action,
            "error": str(exc),
        }, [Evidence(
            step=step.action,
            label="device_execution_failed",
            detail=f"{device_id}.{action} failed: {exc}",
            success=False,
            data={"device_id": device_id, "device_action": action},
        )])


# ── Planner capability-action handlers ────────────────────────────

_ACTION_SKILLS = {
    "create_directory": "file_tools.create_directory",
    "write_file": "file_tools.write_file",
    "append_file": "file_tools.append_file",
    "validate_syntax": "skill_validator.validate_syntax",
    "check_interface": "skill_validator.check_capability_interface",
    "list_capabilities": "registry_manager.list_capabilities",
    "publish_capability": "registry_manager.publish_capability",
    "install_capability": "registry_manager.install_capability",
}


def capability_skill_for_action(action: str, params: Optional[dict] = None) -> Optional[str]:
    """Resolve a planner action to its gateway-enforced skill contract."""
    if action == "run_command":
        cap_id = str((params or {}).get("cap_id", "command_exec"))
        return f"{cap_id}.run"
    return _ACTION_SKILLS.get(action)


def _gateway_action(action: str):
    def handler(task: Task, step: TaskStep, ctx: ExecutionContext) -> tuple[bool, dict, list]:
        skill = capability_skill_for_action(action, step.params)
        if skill is None or ctx.capability_registry is None:
            reason = (
                f"no capability mapping for {action}"
                if skill is None else "capability registry unavailable"
            )
            return (False, {}, [Evidence(
                step=step.action, label=action, detail=reason, success=False,
            )])

        params = dict(step.params)
        params.pop("cap_id", None)
        try:
            res = ctx.capability_registry.call(
                ctx.identity_id,
                skill,
                execution_scope=f"task:{task.task_id}",
                **params,
            )
            ok = bool(getattr(res, "success", False))
            raw_data = getattr(res, "data", {})
            data = raw_data if isinstance(raw_data, dict) else {"result": str(raw_data)}
            error = getattr(res, "error", None) or {}
            error_type = error.get("type", "") if isinstance(error, dict) else ""
            error_message = error.get("message", str(error)) if error else ""
            evidence = [Evidence(
                step=step.action,
                label=action,
                detail=(
                    f"{skill} succeeded"
                    if ok else f"{skill} failed: {error_message or 'unknown error'}"
                ),
                success=ok,
                data={
                    **data,
                    "skill": skill,
                    **({"error_type": error_type} if error_type else {}),
                },
            )]
            if error_type == "permission_denied":
                return (False, {
                    "blocked": True,
                    "resumable": True,
                    "block_type": "authorization_required",
                    "reason": error_message,
                    "skill": skill,
                }, evidence)
            return (ok, data, evidence)
        except Exception as exc:
            return (False, {"skill": skill, "error": str(exc)}, [Evidence(
                step=step.action,
                label=action,
                detail=f"{skill} failed: {exc}",
                success=False,
                data={"skill": skill, "error": str(exc)},
            )])
    return handler


_HANDLERS: dict[str, Any] = {
    "registry_search": _registry_search,
    "trust": _trust,
    "dependencies": _dependencies,
    "generate": _generate,
    "validate": _validate,
    "publish": _publish,
    "install": _install,
    "activate": _activate,
    "invoke": _invoke,
    "persist": _persist,
    "reload": _reload,
    "reuse": _reuse,
    "verify": _verify,
    "verify_goal": _verify_goal,
    "device_action": _device_action,
    # Planner actions execute through the same install/permission/schema
    # gateway as model-originated capability calls.
    "create_directory": _gateway_action("create_directory"),
    "write_file": _gateway_action("write_file"),
    "append_file": _gateway_action("append_file"),
    "validate_syntax": _gateway_action("validate_syntax"),
    "check_interface": _gateway_action("check_interface"),
    "list_capabilities": _gateway_action("list_capabilities"),
    "publish_capability": _gateway_action("publish_capability"),
    "install_capability": _gateway_action("install_capability"),
    "run_command": _gateway_action("run_command"),
}


# These handlers are either observational or converge on the same persisted
# state when invoked repeatedly with the same parameters. Everything omitted
# from this set is treated as outcome-unknown after an interrupted attempt.
_REPLAY_POLICIES: dict[str, ReplayPolicy] = {
    "registry_search": ReplayPolicy.RETRY,
    "trust": ReplayPolicy.RETRY,
    "dependencies": ReplayPolicy.RETRY,
    "generate": ReplayPolicy.RETRY,
    "validate": ReplayPolicy.RETRY,
    "install": ReplayPolicy.RETRY,
    "activate": ReplayPolicy.RETRY,
    "invoke": ReplayPolicy.RETRY,
    "persist": ReplayPolicy.RETRY,
    "reload": ReplayPolicy.RETRY,
    "reuse": ReplayPolicy.RETRY,
    "verify": ReplayPolicy.RETRY,
    "create_directory": ReplayPolicy.RETRY,
    "write_file": ReplayPolicy.RETRY,
    "validate_syntax": ReplayPolicy.RETRY,
    "check_interface": ReplayPolicy.RETRY,
    "list_capabilities": ReplayPolicy.RETRY,
    "install_capability": ReplayPolicy.RETRY,
}


def _load_manifest_skills(cap_id: str) -> list:
    return _load_capability_manifest(cap_id).get("skills", [])


def _load_capability_manifest(cap_id: Optional[str]) -> dict:
    if not cap_id:
        return {}
    manifest_path = _REPO_ROOT / "registry" / "capabilities" / cap_id / "manifest.json"
    try:
        with open(manifest_path) as f:
            manifest = json.load(f)
        return manifest if isinstance(manifest, dict) else {}
    except Exception:
        return {}


def _search_marketplace(capability: str) -> tuple[bool, Optional[str]]:
    """Search the marketplace index for a capability by id/name/skills."""
    idx_path = _REPO_ROOT / "registry" / "capabilities" / "index.json"
    try:
        with open(idx_path) as f:
            data = json.load(f)
    except Exception:
        return (False, None)
    entries = data if isinstance(data, list) else data.get("capabilities", [])
    needle = (capability or "").lower()
    for e in entries:
        eid = str(e.get("id", "")).lower()
        name = str(e.get("name", "")).lower()
        skills = e.get("skills", [])
        if isinstance(skills, int):
            skills = []
        if not skills:
            skills = _load_manifest_skills(e.get("id", ""))
        skills = [str(s.get("name", "")).lower() for s in skills if isinstance(s, dict)]
        if needle in eid or needle == name or needle in skills or needle in [s.split(".")[0] for s in skills]:
            return (True, e.get("id", capability))
        if needle == eid.split(".")[-1]:
            return (True, e.get("id", capability))
    return (False, None)
