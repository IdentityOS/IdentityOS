"""Generalize verified Executive tasks and score them on held-out examples."""

from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any

from core.capabilities.contracts import validate_parameters
from core.executive.executor import replay_policy_for_action
from core.executive.models import ReplayPolicy, Task, TaskStatus, TaskStepStatus

from .models import HeldOutExample, Procedure, ProcedureVersion
from .store import ProcedureStore

_PROCEDURE_ID = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_SENSITIVE_KEYS = {"api_key", "credential", "password", "secret", "token"}
_MARKER = "$procedure_parameter"


class ProcedureLearningError(RuntimeError):
    pass


def _same_value(left: Any, right: Any) -> bool:
    return type(left) is type(right) and left == right


def _parameterize(value: Any, bindings: dict[str, Any], used: set[str]) -> Any:
    for name, training_value in bindings.items():
        if _same_value(value, training_value):
            used.add(name)
            return {_MARKER: name}
    if isinstance(value, dict):
        return {key: _parameterize(item, bindings, used) for key, item in value.items()}
    if isinstance(value, list):
        return [_parameterize(item, bindings, used) for item in value]
    return value


def _render(value: Any, bindings: dict[str, Any]) -> Any:
    if isinstance(value, dict) and set(value) == {_MARKER}:
        name = value[_MARKER]
        if name not in bindings:
            raise ProcedureLearningError(f"missing procedure parameter: {name}")
        return bindings[name]
    if isinstance(value, dict):
        return {key: _render(item, bindings) for key, item in value.items()}
    if isinstance(value, list):
        return [_render(item, bindings) for item in value]
    return value


def _schema_for(value: Any) -> dict[str, Any]:
    if isinstance(value, bool):
        return {"type": "boolean"}
    if isinstance(value, int):
        return {"type": "integer"}
    if isinstance(value, float):
        return {"type": "number"}
    if isinstance(value, str):
        return {"type": "string"}
    if isinstance(value, list):
        return {"type": "array"}
    if isinstance(value, dict):
        return {"type": "object"}
    raise ProcedureLearningError(f"unsupported parameter type: {type(value).__name__}")


def _contains_unbound_secret(value: Any, path: tuple[str, ...] = ()) -> bool:
    if isinstance(value, dict):
        if set(value) == {_MARKER}:
            return False
        for key, item in value.items():
            lowered = str(key).lower()
            if any(part in lowered for part in _SENSITIVE_KEYS):
                return True
            if _contains_unbound_secret(item, (*path, str(key))):
                return True
    elif isinstance(value, list):
        return any(_contains_unbound_secret(item, path) for item in value)
    return False


def _canonical_steps(steps: list[dict[str, Any]] | tuple[dict[str, Any], ...]) -> list[dict[str, Any]]:
    return [
        {"action": str(step.get("action", "")), "params": step.get("params", {})}
        for step in steps
    ]


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class ProcedureLearner:
    def __init__(self, storage: Any) -> None:
        self.store = ProcedureStore(storage)

    def register_held_out_suite(
        self,
        identity_id: str,
        suite_id: str,
        examples: list[HeldOutExample],
    ) -> str:
        """Trusted host API; deliberately not exposed as a model-callable skill."""
        return self.store.register_suite(identity_id, suite_id, examples)

    def learn(
        self,
        *,
        identity_id: str,
        procedure_id: str,
        source_task: Task,
        parameter_bindings: dict[str, Any],
        held_out_suite_id: str,
        name: str = "",
        minimum_score: float = 0.8,
    ) -> ProcedureVersion:
        if not _PROCEDURE_ID.fullmatch(procedure_id):
            raise ProcedureLearningError("invalid procedure id")
        self._verify_source_task(identity_id, source_task)
        candidate, schema = self._generalize(source_task, parameter_bindings)
        template_digest = _digest({"template": candidate, "parameter_schema": schema})

        # Held-out data is loaded only after the candidate is frozen and
        # digested. It can score the candidate but cannot shape it.
        suite_digest, examples = self.store.load_suite(identity_id, held_out_suite_id)
        evaluations = self._evaluate(candidate, schema, examples)
        score = sum(item["passed"] for item in evaluations) / len(evaluations)

        procedure = self.store.get(identity_id, procedure_id) or Procedure(
            procedure_id=procedure_id,
            identity_id=identity_id,
            name=name or procedure_id.replace("_", " ").title(),
        )
        champion = procedure.champion
        promotes = score >= minimum_score and (
            champion is None or score > champion.held_out_score
        )
        reason = None
        if score < minimum_score:
            reason = f"held-out score {score:.3f} is below required {minimum_score:.3f}"
        elif champion is not None:
            reason = (
                f"candidate score {score:.3f} did not beat champion "
                f"{champion.held_out_score:.3f}"
            )
        version = ProcedureVersion(
            version=len(procedure.versions) + 1,
            source_task_id=source_task.task_id,
            template=candidate,
            parameter_schema=schema,
            template_sha256=template_digest,
            suite_id=held_out_suite_id,
            suite_sha256=suite_digest,
            held_out_score=round(score, 6),
            evaluations=evaluations,
            status="promoted" if promotes else "rejected",
            created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            rejection_reason=reason,
        )
        procedure.versions.append(version)
        if promotes:
            if champion is not None:
                champion.status = "superseded"
            procedure.champion_version = version.version
        self.store.save(procedure)
        return version

    def compile(
        self,
        identity_id: str,
        procedure_id: str,
        bindings: dict[str, Any],
    ) -> list[dict[str, Any]]:
        procedure = self.store.get(identity_id, procedure_id)
        if procedure is None or procedure.champion is None:
            raise ProcedureLearningError(f"no promoted procedure: {procedure_id}")
        validate_parameters(procedure.champion.parameter_schema, bindings)
        return [
            {
                "action": step["action"],
                "description": step.get("description", step["action"]),
                "params": _render(step.get("params", {}), bindings),
            }
            for step in procedure.champion.template
        ]

    def _verify_source_task(self, identity_id: str, task: Task) -> None:
        if task.identity_id != identity_id or task.status != TaskStatus.COMPLETED:
            raise ProcedureLearningError("source task must be completed by the same identity")
        steps = [step for step in task.steps if step.status == TaskStepStatus.COMPLETED]
        if len(steps) < 2:
            raise ProcedureLearningError("procedure learning requires a multi-step task")
        for step in steps:
            if replay_policy_for_action(step.action) != ReplayPolicy.RETRY:
                raise ProcedureLearningError(
                    f"step '{step.action}' is not deterministic/replay-safe"
                )
            if not step.evidence or not all(item.success for item in step.evidence):
                raise ProcedureLearningError(
                    f"step '{step.action}' lacks wholly successful execution evidence"
                )

    def _generalize(
        self,
        task: Task,
        parameter_bindings: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        if not parameter_bindings:
            raise ProcedureLearningError("at least one parameter binding is required")
        properties = {name: _schema_for(value) for name, value in parameter_bindings.items()}
        schema = {
            "type": "object",
            "properties": properties,
            "required": sorted(properties),
            "additionalProperties": False,
        }
        used: set[str] = set()
        template = []
        for step in task.steps:
            if step.status != TaskStepStatus.COMPLETED:
                continue
            params = _parameterize(step.params, parameter_bindings, used)
            if _contains_unbound_secret(params):
                raise ProcedureLearningError(
                    f"step '{step.action}' contains an unparameterized sensitive field"
                )
            template.append({
                "action": step.action,
                "description": step.description,
                "params": params,
            })
        unused = set(parameter_bindings) - used
        if unused:
            raise ProcedureLearningError(
                "bindings not observed in the successful task: " + ", ".join(sorted(unused))
            )
        return template, schema

    def _evaluate(
        self,
        template: list[dict[str, Any]],
        schema: dict[str, Any],
        examples: list[HeldOutExample],
    ) -> list[dict[str, Any]]:
        results = []
        for example in examples:
            try:
                accepted_names = set(schema.get("properties", {}))
                accepted_bindings = {
                    name: value
                    for name, value in example.bindings.items()
                    if name in accepted_names
                }
                validate_parameters(schema, accepted_bindings)
                observed = [
                    {
                        "action": step["action"],
                        "params": _render(step.get("params", {}), accepted_bindings),
                    }
                    for step in template
                ]
                expected = _canonical_steps(example.expected_steps)
                passed = observed == expected
                error = None
            except Exception as exc:
                observed = []
                expected = _canonical_steps(example.expected_steps)
                passed = False
                error = f"{type(exc).__name__}: {exc}"
            results.append({
                "example_id": example.example_id,
                "passed": passed,
                "expected_sha256": _digest(expected),
                "observed_sha256": _digest(observed),
                "error": error,
            })
        return results
