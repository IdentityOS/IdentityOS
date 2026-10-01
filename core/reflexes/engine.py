"""Exact-trigger execution of promoted procedures without model replanning."""

from __future__ import annotations

import hashlib
import json
import re
import statistics
import string
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from core.capabilities.contracts import validate_parameters
from core.executive.executor import capability_skill_for_action
from core.executive.models import TaskStatus, TaskStepStatus
from core.procedures import ProcedureLearner, ProcedureStore

from .models import ReflexBinding, ReflexRun
from .store import ReflexStore

_ID = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_FIELD = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SENSITIVE = ("api_key", "credential", "password", "secret", "token")


class ReflexError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _canonical_steps(steps: list[Any]) -> list[dict[str, Any]]:
    result = []
    for step in steps:
        if isinstance(step, dict):
            result.append({
                "action": str(step.get("action", "")),
                "params": dict(step.get("params", {})),
            })
        else:
            result.append({
                "action": str(getattr(step, "action", "")),
                "params": dict(getattr(step, "params", {})),
            })
    return result


class ReflexEngine:
    def __init__(self, storage: Any, executive: Any, capability_registry: Any) -> None:
        self.storage = storage
        self.executive = executive
        self.capability_registry = capability_registry
        self.store = ReflexStore(storage)
        self.procedures = ProcedureStore(storage)

    def register(
        self,
        identity_id: str,
        reflex_id: str,
        procedure_id: str,
        trigger_template: str,
    ) -> ReflexBinding:
        if not _ID.fullmatch(reflex_id):
            raise ReflexError("invalid reflex id")
        procedure = self.procedures.get(identity_id, procedure_id)
        if procedure is None or procedure.champion is None:
            raise ReflexError("reflexes require a promoted procedure champion")
        champion = procedure.champion
        if champion.status != "promoted":
            raise ReflexError("procedure champion is not promoted")
        self._compile_trigger(trigger_template, champion.parameter_schema)
        self._validate_actions(champion.template)

        normalized = trigger_template.strip()
        trigger_sha256 = _digest({
            "trigger_template": normalized,
            "parameter_schema": champion.parameter_schema,
        })
        existing = self.store.binding(identity_id, reflex_id)
        if existing is not None:
            if (
                existing.procedure_id == procedure_id
                and existing.procedure_version == champion.version
                and existing.trigger_sha256 == trigger_sha256
                and existing.procedure_sha256 == champion.template_sha256
            ):
                return existing
            raise ReflexError("reflex ids are immutable; register a new reflex id")
        for item in self.store.bindings(identity_id):
            if item.trigger_sha256 == trigger_sha256:
                raise ReflexError("an identical trigger is already registered")

        binding = ReflexBinding(
            reflex_id=reflex_id,
            identity_id=identity_id,
            procedure_id=procedure_id,
            procedure_version=champion.version,
            trigger_template=normalized,
            trigger_sha256=trigger_sha256,
            procedure_sha256=champion.template_sha256,
            created_at=_now(),
        )
        self.store.save_binding(binding)
        return binding

    def list(self, identity_id: str) -> list[dict[str, Any]]:
        items = []
        for binding in self.store.bindings(identity_id):
            valid, reason = self._current_champion(binding)
            items.append({
                **binding.to_dict(),
                "ready": bool(valid),
                "reason": reason,
            })
        return items

    def prepare(self, identity_id: str, utterance: str) -> Optional[dict[str, Any]]:
        match_started = time.monotonic()
        matches: list[tuple[ReflexBinding, dict[str, Any], Any]] = []
        for binding in self.store.bindings(identity_id):
            champion, _ = self._current_champion(binding)
            if champion is None:
                continue
            pattern = self._compile_trigger(
                binding.trigger_template, champion.parameter_schema,
            )
            match = pattern.fullmatch(utterance.strip())
            if match is None:
                continue
            bindings = self._coerce_bindings(
                match.groupdict(), champion.parameter_schema,
            )
            matches.append((binding, bindings, champion))
        match_ms = (time.monotonic() - match_started) * 1000
        if not matches:
            return None
        if len(matches) != 1:
            raise ReflexError("multiple reflexes matched; refusing ambiguous execution")

        binding, bindings, champion = matches[0]
        compile_started = time.monotonic()
        steps = ProcedureLearner(self.storage).compile(
            identity_id, binding.procedure_id, bindings,
        )
        self._validate_actions(steps)
        self._authorize_steps(identity_id, steps)
        compile_ms = (time.monotonic() - compile_started) * 1000
        canonical = _canonical_steps(steps)
        return {
            "binding": binding,
            "bindings": bindings,
            "steps": steps,
            "plan_sha256": _digest(canonical),
            "timings_ms": {
                "match": round(match_ms, 3),
                "compile_and_preflight": round(compile_ms, 3),
            },
        }

    def dispatch(
        self,
        identity_id: str,
        utterance: str,
        *,
        autostart: bool = True,
    ) -> Optional[dict[str, Any]]:
        started = time.monotonic()
        prepared = self.prepare(identity_id, utterance)
        if prepared is None:
            return None
        binding: ReflexBinding = prepared["binding"]
        dispatch_started = time.monotonic()
        task = self.executive.start_task(
            goal=f"Reflex {binding.reflex_id}: {binding.procedure_id}",
            identity_id=identity_id,
            original_request=utterance,
            steps=prepared["steps"],
            autostart=autostart,
        )
        dispatch_ms = (time.monotonic() - dispatch_started) * 1000
        timings = dict(prepared["timings_ms"])
        timings["dispatch"] = round(dispatch_ms, 3)
        timings["total"] = round((time.monotonic() - started) * 1000, 3)
        run = ReflexRun(
            run_id=str(uuid.uuid4()),
            reflex_id=binding.reflex_id,
            identity_id=identity_id,
            task_id=task.task_id,
            procedure_id=binding.procedure_id,
            procedure_version=binding.procedure_version,
            expected_plan_sha256=prepared["plan_sha256"],
            trigger_sha256=binding.trigger_sha256,
            status="dispatched",
            dispatched_at=_now(),
            timings_ms=timings,
            model_planning_calls=0,
        )
        self.store.save_run(run)
        return {
            "run_id": run.run_id,
            "reflex_id": run.reflex_id,
            "task_id": task.task_id,
            "task_status": task.status.value,
            "procedure_id": run.procedure_id,
            "procedure_version": run.procedure_version,
            "plan_sha256": run.expected_plan_sha256,
            "model_planning_calls": 0,
            "timings_ms": timings,
        }

    def reconcile(self, identity_id: str, run_id: str) -> dict[str, Any]:
        run = self.store.run(identity_id, run_id)
        if run is None:
            raise ReflexError(f"unknown reflex run: {run_id}")
        task = self.executive.get_task(identity_id, run.task_id)
        if task is None:
            correct = False
            terminal = True
            reason = "durable task is missing"
            observed_digest = _digest([])
        else:
            terminal = task.status in {
                TaskStatus.COMPLETED,
                TaskStatus.FAILED,
                TaskStatus.CANCELLED,
            }
            observed_digest = _digest(_canonical_steps(task.steps))
            evidence_ok = bool(task.steps) and all(
                step.status == TaskStepStatus.COMPLETED
                and bool(step.evidence)
                and all(item.success for item in step.evidence)
                for step in task.steps
            )
            correct = (
                task.status == TaskStatus.COMPLETED
                and observed_digest == run.expected_plan_sha256
                and evidence_ok
            )
            if not terminal:
                reason = f"task is {task.status.value}"
            elif observed_digest != run.expected_plan_sha256:
                reason = "executed plan digest differs from dispatched plan"
            elif not evidence_ok:
                reason = "task lacks wholly successful step evidence"
            else:
                reason = "execution and evidence match the promoted procedure"

        run.status = "verified" if correct else ("pending" if not terminal else "failed")
        run.verified_at = _now() if terminal else None
        run.verification = {
            "terminal": terminal,
            "correct": correct,
            "reason": reason,
            "expected_plan_sha256": run.expected_plan_sha256,
            "observed_plan_sha256": observed_digest,
            "task_status": task.status.value if task is not None else "missing",
        }
        self.store.save_run(run)
        return {"run_id": run_id, "status": run.status, **run.verification}

    def benchmark_planning(
        self,
        identity_id: str,
        reflex_id: str,
        utterance: str,
        planner: Callable[[str], list[Any]],
        *,
        iterations: int = 3,
    ) -> dict[str, Any]:
        """Measure both paths locally and compare their emitted plans.

        Caller-supplied latency or correctness claims are never accepted.
        This host API measures elapsed monotonic time and hashes canonical plans.
        """
        if iterations < 2 or iterations > 20:
            raise ReflexError("benchmark iterations must be between 2 and 20")
        binding = self.store.binding(identity_id, reflex_id)
        if binding is None:
            raise ReflexError(f"unknown reflex: {reflex_id}")
        reflex_ms: list[float] = []
        planner_ms: list[float] = []
        reflex_digests: list[str] = []
        planner_digests: list[str] = []
        for _ in range(iterations):
            started = time.monotonic()
            prepared = self.prepare(identity_id, utterance)
            reflex_ms.append((time.monotonic() - started) * 1000)
            if prepared is None or prepared["binding"].reflex_id != reflex_id:
                raise ReflexError("benchmark utterance did not resolve to the requested reflex")
            reflex_digests.append(prepared["plan_sha256"])

            started = time.monotonic()
            planned = planner(utterance)
            planner_ms.append((time.monotonic() - started) * 1000)
            planner_digests.append(_digest(_canonical_steps(planned)))

        reflex_median = statistics.median(reflex_ms)
        planner_median = statistics.median(planner_ms)
        correctness = (
            len(set(reflex_digests)) == 1
            and len(set(planner_digests)) == 1
            and reflex_digests[0] == planner_digests[0]
        )
        report = {
            "reflex_id": reflex_id,
            "iterations": iterations,
            "reflex_median_ms": round(reflex_median, 3),
            "planner_median_ms": round(planner_median, 3),
            "latency_improved": reflex_median < planner_median,
            "speedup": round(planner_median / max(reflex_median, 0.001), 3),
            "correctness_preserved": correctness,
            "reflex_plan_sha256": reflex_digests[0],
            "planner_plan_sha256": planner_digests[0],
            "measured_at": _now(),
        }
        self.store.save_benchmark(identity_id, report)
        return report

    def _current_champion(self, binding: ReflexBinding) -> tuple[Any, str]:
        procedure = self.procedures.get(binding.identity_id, binding.procedure_id)
        champion = procedure.champion if procedure is not None else None
        if champion is None:
            return None, "procedure champion is missing"
        if champion.status != "promoted":
            return None, "procedure champion is not promoted"
        if champion.version != binding.procedure_version:
            return None, "procedure champion version changed; register a new reflex"
        if champion.template_sha256 != binding.procedure_sha256:
            return None, "procedure digest changed"
        expected_trigger = _digest({
            "trigger_template": binding.trigger_template,
            "parameter_schema": champion.parameter_schema,
        })
        if expected_trigger != binding.trigger_sha256:
            return None, "trigger or parameter schema digest changed"
        return champion, "ready"

    def _validate_actions(self, steps: list[dict[str, Any]]) -> None:
        unsupported = [
            str(step.get("action", ""))
            for step in steps
            if capability_skill_for_action(
                str(step.get("action", "")), step.get("params", {}),
            ) is None
        ]
        if unsupported:
            raise ReflexError(
                "reflex procedure contains unsupported action(s): "
                + ", ".join(sorted(set(unsupported)))
            )

    def _authorize_steps(self, identity_id: str, steps: list[dict[str, Any]]) -> None:
        for step in steps:
            skill = capability_skill_for_action(step["action"], step.get("params", {}))
            allowed, reason = self.capability_registry.can(identity_id, skill)
            if not allowed:
                raise ReflexError(f"reflex preflight denied {skill}: {reason}")

    @staticmethod
    def _compile_trigger(template: str, schema: dict[str, Any]) -> re.Pattern[str]:
        normalized = template.strip()
        if not normalized or len(normalized) > 500:
            raise ReflexError("trigger template must contain 1 to 500 characters")
        properties = schema.get("properties", {})
        required = set(schema.get("required", []))
        if set(properties) != required:
            raise ReflexError("reflex triggers require all procedure parameters to be required")
        if any(any(secret in name.lower() for secret in _SENSITIVE) for name in properties):
            raise ReflexError("sensitive parameters cannot be bound by a reflex trigger")
        for name, definition in properties.items():
            if not _FIELD.fullmatch(name):
                raise ReflexError(f"invalid trigger parameter name: {name}")
            if definition.get("type") not in {"string", "integer", "number", "boolean"}:
                raise ReflexError(f"trigger parameter '{name}' must be a scalar")

        pieces = ["^"]
        fields: list[str] = []
        previous_was_field = False
        try:
            parsed = list(string.Formatter().parse(normalized))
        except ValueError as exc:
            raise ReflexError(f"invalid trigger template: {exc}") from exc
        for literal, field_name, format_spec, conversion in parsed:
            if previous_was_field and not literal:
                raise ReflexError("adjacent trigger parameters are ambiguous")
            pieces.append(re.escape(literal))
            previous_was_field = field_name is not None
            if field_name is None:
                continue
            if format_spec or conversion:
                raise ReflexError("trigger formatting and conversions are not supported")
            if not _FIELD.fullmatch(field_name):
                raise ReflexError(f"invalid trigger parameter name: {field_name}")
            if field_name in fields:
                raise ReflexError("each trigger parameter must appear exactly once")
            fields.append(field_name)
            pieces.append(f"(?P<{field_name}>.+?)")
        if set(fields) != set(properties):
            raise ReflexError("trigger parameters must exactly match procedure parameters")
        pieces.append("$")
        return re.compile("".join(pieces), re.IGNORECASE | re.DOTALL)

    @staticmethod
    def _coerce_bindings(
        raw: dict[str, str], schema: dict[str, Any],
    ) -> dict[str, Any]:
        bindings: dict[str, Any] = {}
        for name, value in raw.items():
            expected = schema["properties"][name].get("type")
            try:
                if expected == "integer":
                    bindings[name] = int(value)
                elif expected == "number":
                    bindings[name] = float(value)
                elif expected == "boolean":
                    lowered = value.lower()
                    if lowered not in {"true", "false"}:
                        raise ValueError("expected true or false")
                    bindings[name] = lowered == "true"
                else:
                    bindings[name] = value
            except ValueError as exc:
                raise ReflexError(f"invalid value for trigger parameter '{name}': {exc}") from exc
        validate_parameters(schema, bindings)
        return bindings
