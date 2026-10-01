"""
engine.py — ExecutiveRuntime.

The persistent execution engine for IdentityOS identities.

The planner decides WHAT should happen; the executive ensures it actually
happens: it commits goals to durable tasks, executes steps with evidence,
tracks progress, survives interruption, and never silently abandons work.
"""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any, Optional

from core.executive import workflow
from core.executive.executor import (
    ExecutionContext,
    execute_step,
    replay_policy_for_action,
    rollback_acquisition,
)
from core.executive.models import (
    Evidence,
    ReplayPolicy,
    Task,
    TaskStatus,
    TaskStep,
    TaskStepStatus,
)
from core.executive.progress import compute_progress, render_progress_block
from core.executive.recovery import recover_tasks
from core.executive.scheduler import TaskScheduler
from core.executive.state import IllegalTransition, transition
from core.executive.store import TaskStore


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def register_executive(executive: "ExecutiveRuntime") -> None:
    from core.acquisition import register_acquisition_provider

    register_acquisition_provider(executive.storage, executive)


def get_executive_for(storage: Any) -> Optional["ExecutiveRuntime"]:
    from core.acquisition import get_acquisition_provider

    provider = get_acquisition_provider(storage)
    return provider if isinstance(provider, ExecutiveRuntime) else None


def unregister_executive(executive: "ExecutiveRuntime") -> None:
    """Remove only the exact engine registered for its storage object."""
    from core.acquisition import unregister_acquisition_provider

    unregister_acquisition_provider(executive.storage, executive)


class ExecutiveRuntime:
    def __init__(
        self,
        storage: Any,
        capability_registry: Any = None,
        *,
        autostart: bool = False,
        skill_forge: Any = None,
        embodiment_hub: Any = None,
    ) -> None:
        self.storage = storage
        self.capability_registry = capability_registry
        self.skill_forge = skill_forge
        self.embodiment_hub = embodiment_hub
        self.store = TaskStore(storage)
        self._ctx_cache: dict[str, ExecutionContext] = {}
        self.scheduler = TaskScheduler(self)
        self._autostart = autostart
        # Serializes step execution between the scheduler thread and direct
        # process_ready() calls from the chat thread so the storage backend
        # never sees concurrent writes to the same task document.
        self._exec_lock = threading.RLock()
        self.scheduler._lock = self._exec_lock

    # ── Context ──────────────────────────────────────────────────────

    def _ctx(self, identity_id: str, runtime: Any = None) -> ExecutionContext:
        if identity_id not in self._ctx_cache:
            self._ctx_cache[identity_id] = ExecutionContext(
                identity_id=identity_id,
                capability_registry=self.capability_registry,
                storage=self.storage,
                runtime=runtime,
                skill_forge=self.skill_forge,
                embodiment_hub=self.embodiment_hub,
            )
        else:
            if runtime is not None:
                self._ctx_cache[identity_id].runtime = runtime
            self._ctx_cache[identity_id].embodiment_hub = self.embodiment_hub
        return self._ctx_cache[identity_id]

    # ── Task lifecycle API ────────────────────────────────────────────

    def start_task(
        self,
        goal: str,
        identity_id: str,
        *,
        capability_id: Optional[str] = None,
        original_request: Optional[str] = None,
        priority: int = 0,
        steps: Optional[list] = None,
        autostart: Optional[bool] = None,
        runtime: Any = None,
    ) -> Task:
        """Commit to a goal: create a persistent task and queue it."""
        task = Task(
            task_id=str(uuid.uuid4()),
            goal=goal,
            identity_id=identity_id,
            priority=priority,
            capability_id=capability_id,
            original_request=original_request,
        )
        task.steps = [self._coerce_step(s) for s in (steps or self._plan_for(goal, capability_id))]
        if runtime is not None:
            self._ctx(identity_id, runtime=runtime)
        self.store.save(task)
        if autostart is None:
            autostart = self._autostart
        if autostart:
            self.scheduler.start()
        return task

    def request_acquisition(
        self,
        identity_id: str,
        capability_id: str,
        goal: str,
        *,
        original_request: Optional[str] = None,
        priority: int = 0,
        runtime: Any = None,
    ) -> tuple[Task, bool]:
        """Idempotently request a durable capability acquisition.

        A historical ``COMPLETED`` task is only reused when runtime state still
        proves that the capability is installed. This prevents a stale task
        claim from replacing current installation evidence.
        """
        for task in self.active_tasks(identity_id):
            if task.capability_id == capability_id:
                if runtime is not None:
                    self._ctx(identity_id, runtime=runtime)
                return task, False

        installed = False
        if self.capability_registry is not None:
            try:
                installed = self.capability_registry.get(identity_id, capability_id) is not None
            except Exception:
                installed = False
        if installed:
            completed = [
                task
                for task in self.store.load_terminal(identity_id)
                if task.capability_id == capability_id
                and task.status == TaskStatus.COMPLETED
            ]
            if completed:
                completed.sort(key=lambda task: task.last_updated, reverse=True)
                return completed[0], False

        task = self.create_acquisition_task(
            identity_id=identity_id,
            capability_id=capability_id,
            goal=goal,
            original_request=original_request,
            priority=priority,
            runtime=runtime,
        )
        self.scheduler.start()
        return task, True

    def create_acquisition_task(
        self,
        identity_id: str,
        capability_id: str,
        goal: str,
        *,
        original_request: Optional[str] = None,
        priority: int = 0,
        runtime: Any = None,
    ) -> Task:
        """Create a task that acquires *capability_id* via the generic workflow."""
        return self.start_task(
            goal=goal,
            identity_id=identity_id,
            capability_id=capability_id,
            original_request=original_request,
            priority=priority,
            steps=workflow.build_acquisition_plan(capability_id, original_request),
            runtime=runtime,
        )

    def _plan_for(self, goal: str, capability_id: Optional[str] = None) -> list[dict]:
        cap = capability_id or workflow.extract_capability_name(goal)
        if cap and workflow.is_acquisition_goal(goal):
            return workflow.build_acquisition_plan(cap, goal)
        # Not an acquisition goal: ask the planner for a PLAN ONLY — the
        # executive owns execution (planner never executes on its own here).
        try:
            from core.capabilities.task_planner import TaskPlannerCapability
            plan = TaskPlannerCapability._generate_plan(goal)
            if plan:
                return [
                    {
                        "action": s.get("action", ""),
                        "description": s.get("description", s.get("action", "")),
                        "params": s.get("params", {}),
                    }
                    for s in plan
                    if s.get("action")
                ]
        except Exception:
            pass
        return [{
            "action": "verify_goal",
            "description": "Completing goal",
            "params": {"request": goal},
        }]

    def _coerce_step(self, s) -> TaskStep:
        if isinstance(s, TaskStep):
            step = s
        elif isinstance(s, dict):
            if "status" in s and not isinstance(s.get("status"), TaskStepStatus):
                step = TaskStep.from_dict(s)
            else:
                step = TaskStep(**s)
        else:
            raise TypeError(f"Invalid step definition: {type(s).__name__}")
        if step.replay_policy is None:
            step.replay_policy = replay_policy_for_action(step.action)
        if step.replay_policy == ReplayPolicy.BLOCK:
            # A non-replay-safe effect gets one automatic attempt. If its
            # outcome is unknown, only explicit reconciliation may continue it.
            step.max_retries = 1
        return step

    def get_task(self, identity_id: str, task_id: str) -> Optional[Task]:
        return self.store.load(identity_id, task_id)

    def active_tasks(self, identity_id: str) -> list[Task]:
        tasks = self.store.load_active(identity_id)
        tasks.sort(key=lambda t: (-t.priority, t.created_at))
        return tasks

    def current_task(self, identity_id: str) -> Optional[Task]:
        active = self.active_tasks(identity_id)
        return active[0] if active else None

    def task_status(self, identity_id: str, task_id: str) -> dict:
        task = self.get_task(identity_id, task_id)
        if task is None:
            return {"task_id": task_id, "found": False}
        return compute_progress(task)

    def task_progress(self, identity_id: str, task_id: str) -> dict:
        return self.task_status(identity_id, task_id)

    def history(self, identity_id: str, limit: int = 10) -> list[dict]:
        tasks = self.store.load_terminal(identity_id)
        tasks.sort(key=lambda t: t.last_updated, reverse=True)
        return [t.to_dict() for t in tasks[:limit]]

    # ── Status transitions ────────────────────────────────────────────

    def resume_task(self, identity_id: str, task_id: str, *, force: bool = False) -> Task:
        task = self.require(identity_id, task_id)
        if task.status == TaskStatus.COMPLETED:
            return task
        if task.status in (TaskStatus.RUNNING, TaskStatus.QUEUED):
            pass
        elif task.status == TaskStatus.BLOCKED:
            blocked_steps = [
                step for step in task.steps
                if step.status == TaskStepStatus.BLOCKED
            ]
            if any(
                not (
                    step.result.get("resumable")
                    or step.result.get("block_type") == "authorization_required"
                )
                for step in blocked_steps
            ):
                raise IllegalTransition(
                    "Task has a non-resumable or outcome-unknown step; use "
                    "resolve_interrupted_step before resuming"
                )
            for step in blocked_steps:
                step.status = TaskStepStatus.PENDING
                step.error = None
            transition(task, TaskStatus.RUNNING)
            task.error = None
        elif task.status == TaskStatus.FAILED:
            if not force:
                raise IllegalTransition(
                    f"Cannot resume failed task {task_id} without force=True"
                )
            task.retry_count = 0
            install_step = task.step_by_id("install")
            rolled_back = bool(
                install_step is not None
                and install_step.result.get("rolled_back")
            )
            reset_after_install = False
            for s in task.steps:
                if s is install_step and rolled_back:
                    reset_after_install = True
                if (
                    s.status in (TaskStepStatus.FAILED, TaskStepStatus.PENDING)
                    or reset_after_install
                ):
                    s.status = TaskStepStatus.PENDING
                    s.retry_count = 0
                    s.error = None
                    if s is install_step:
                        s.result = {}
            transition(task, TaskStatus.RUNNING)
        else:
            raise IllegalTransition(f"Cannot resume task {task_id} in state {task.status.value}")
        self.store.save(task)
        return task

    def pause_task(self, identity_id: str, task_id: str) -> Task:
        task = self.require(identity_id, task_id)
        if task.status == TaskStatus.RUNNING:
            transition(task, TaskStatus.BLOCKED)
            self.store.save(task)
        return task

    def block_task(self, identity_id: str, task_id: str) -> Task:
        return self.pause_task(identity_id, task_id)

    def unblock_task(self, identity_id: str, task_id: str) -> Task:
        return self.resume_task(identity_id, task_id)

    def resolve_interrupted_step(
        self,
        identity_id: str,
        task_id: str,
        *,
        resolution: str,
        result: Optional[dict] = None,
        detail: str = "",
    ) -> Task:
        """Reconcile an outcome-unknown step without silently replaying it.

        ``completed`` records independently established success. ``retry``
        records an explicit decision that repeating the effect is acceptable.
        Both paths preserve an evidence event before returning the task to the
        scheduler.
        """
        task = self.require(identity_id, task_id)
        step = next((s for s in task.steps if s.status == TaskStepStatus.BLOCKED), None)
        if task.status != TaskStatus.BLOCKED or step is None:
            raise IllegalTransition("Task has no interrupted step awaiting reconciliation")
        if resolution not in ("completed", "retry"):
            raise ValueError("resolution must be 'completed' or 'retry'")
        evidence = Evidence(
            step=step.action,
            label=f"interrupted_attempt_{resolution}",
            detail=detail or f"Interrupted step reconciled as {resolution}",
            success=True,
            data={"resolution": resolution},
        )
        step.evidence.append(evidence)
        task.evidence.append(evidence)
        step.error = None
        if resolution == "completed":
            step.status = TaskStepStatus.COMPLETED
            step.result = result or {"reconciled": True}
        else:
            step.status = TaskStepStatus.PENDING
        task.error = None
        transition(task, TaskStatus.RUNNING)
        task.current_step = step.description
        task.last_updated = _now()
        task.checkpoint = {
            "reconciled_step": step.action,
            "resolution": resolution,
            "saved_at": _now(),
        }
        self.store.save(task)
        return task

    def complete_task(self, identity_id: str, task_id: str, result: Optional[dict] = None) -> Task:
        task = self.require(identity_id, task_id)
        if task.status not in (TaskStatus.RUNNING, TaskStatus.BLOCKED, TaskStatus.QUEUED):
            raise IllegalTransition(f"Cannot complete task in state {task.status.value}")
        transition(task, TaskStatus.COMPLETED)
        task.completion_result = result or {"outcome": "completed"}
        self.store.save(task)
        return task

    def fail_task(self, identity_id: str, task_id: str, error: str, evidence: Optional[list] = None) -> Task:
        task = self.require(identity_id, task_id)
        if task.status not in (TaskStatus.RUNNING, TaskStatus.BLOCKED, TaskStatus.QUEUED):
            raise IllegalTransition(f"Cannot fail task in state {task.status.value}")
        transition(task, TaskStatus.FAILED)
        task.error = error
        if evidence:
            task.evidence.extend(evidence)
        self.store.save(task)
        return task

    def cancel_task(self, identity_id: str, task_id: str) -> Task:
        task = self.require(identity_id, task_id)
        transition(task, TaskStatus.CANCELLED)
        self.store.save(task)
        return task

    def checkpoint(self, identity_id: str, task_id: str) -> Task:
        """Persist current task state (safe to call at any point)."""
        task = self.require(identity_id, task_id)
        task.checkpoint = {
            "progress": task.progress,
            "current_step": task.current_step,
            "completed": [s.action for s in task.completed_steps],
            "saved_at": _now(),
        }
        self.store.save(task)
        return task

    def require(self, identity_id: str, task_id: str) -> Task:
        task = self.store.load(identity_id, task_id)
        if task is None:
            raise KeyError(f"Task not found: {task_id}")
        return task

    # ── Recovery ──────────────────────────────────────────────────────

    def recover(self, identity_id: str) -> list[Task]:
        """Reload active tasks and resume any interrupted work."""
        return recover_tasks(self.store, identity_id, replay_policy_for_action)

    # ── Execution loop ────────────────────────────────────────────────

    def process_ready(self, identity_id: Optional[str] = None, max_steps: int = 1) -> dict:
        """Advance every ready task by at most *max_steps* steps.

        Order: RUNNING tasks (priority desc, then oldest), then QUEUED tasks.
        Returns a summary dict for observability.
        """
        summary = {
            "advanced": 0,
            "completed": [],
            "failed": [],
            "blocked": [],
            "tasks_processed": 0,
        }
        with self._exec_lock:
            identities = [identity_id] if identity_id else self._all_identities_with_tasks()
            for ident in identities:
                tasks = self._ready_tasks(ident)
                for task in tasks:
                    steps_run = self._advance_task(task, ident, max_steps)
                    summary["tasks_processed"] += 1
                    summary["advanced"] += steps_run
                    if task.status == TaskStatus.COMPLETED:
                        summary["completed"].append(task.task_id)
                    elif task.status == TaskStatus.FAILED:
                        summary["failed"].append(task.task_id)
                    elif task.status == TaskStatus.BLOCKED:
                        summary["blocked"].append(task.task_id)
        return summary

    def _all_identities_with_tasks(self) -> list[str]:
        if self.storage is None:
            return []
        try:
            identities = []
            for ident in self.storage.list_identities():
                if self.store.list_task_ids(ident):
                    identities.append(ident)
            return identities
        except Exception:
            return []

    def _ready_tasks(self, identity_id: str) -> list[Task]:
        tasks = [
            task for task in self.store.load_active(identity_id)
            if task.status in (TaskStatus.QUEUED, TaskStatus.RUNNING)
        ]
        tasks.sort(key=lambda t: (0 if t.status == TaskStatus.RUNNING else 1, -t.priority, t.created_at))
        return tasks

    def _advance_task(self, task: Task, identity_id: str, max_steps: int) -> int:
        steps_run = 0
        ctx = self._ctx(identity_id)

        if task.status == TaskStatus.QUEUED:
            transition(task, TaskStatus.RUNNING)

        # Refuse to keep running a terminal task
        if task.status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED):
            return 0

        while steps_run < max_steps:
            next_step = self._next_step(task)
            if next_step is None:
                break
            # Guard conditions
            if not self._guards_pass(task, next_step):
                next_step.status = TaskStepStatus.SKIPPED
                task.current_step = next_step.description
                task.last_updated = _now()
                task.checkpoint = {
                    "skipped": next_step.action,
                    "saved_at": _now(),
                }
                self.store.save(task)
                steps_run += 1
                continue

            next_step.status = TaskStepStatus.RUNNING
            attempt = {
                "attempt_id": str(uuid.uuid4()),
                "status": "running",
                "started_at": _now(),
            }
            next_step.execution_attempts.append(attempt)
            task.current_step = next_step.description
            task.last_updated = _now()
            self.store.save(task)  # checkpoint before executing

            ok, result, evidence = self._execute(task, next_step, ctx)
            next_step.result = result
            next_step.evidence.extend(evidence)
            task.evidence.extend(evidence)

            if ok:
                attempt["status"] = "completed"
                next_step.status = TaskStepStatus.COMPLETED
                next_step.error = None
            else:
                if result.get("blocked"):
                    reason = str(result.get("reason") or f"Step '{next_step.action}' is blocked")
                    attempt["status"] = "blocked"
                    attempt["finished_at"] = _now()
                    attempt["evidence_count"] = len(evidence)
                    next_step.status = TaskStepStatus.BLOCKED
                    next_step.error = reason
                    task.error = reason
                    task.current_step = next_step.description
                    task.last_updated = _now()
                    task.checkpoint = {
                        "blocked_step": next_step.action,
                        "block_type": result.get("block_type", "unspecified"),
                        "saved_at": _now(),
                    }
                    transition(task, TaskStatus.BLOCKED)
                    self.store.save(task)
                    return steps_run + 1
                attempt["status"] = "failed"
                next_step.retry_count += 1
                task.retry_count += 1
                if next_step.retry_count < next_step.max_retries:
                    next_step.status = TaskStepStatus.PENDING  # retry later
                    task.error = f"Step '{next_step.action}' failed, retrying ({next_step.retry_count}/{next_step.max_retries})"
                else:
                    next_step.status = TaskStepStatus.FAILED
                    task.error = f"Step '{next_step.action}' failed after {next_step.retry_count} retries"
                    rollback_evidence = rollback_acquisition(task, ctx)
                    next_step.evidence.extend(rollback_evidence)
                    task.evidence.extend(rollback_evidence)
                    transition(task, TaskStatus.FAILED)
                    attempt["finished_at"] = _now()
                    attempt["evidence_count"] = len(evidence)
                    self.store.save(task)
                    return steps_run + 1

            attempt["finished_at"] = _now()
            attempt["evidence_count"] = len(evidence)

            task.current_step = next_step.description
            task.last_updated = _now()
            task.checkpoint = {
                "progress": task.progress,
                "last_step": next_step.action,
                "saved_at": _now(),
            }
            self.store.save(task)
            steps_run += 1

            if task.status == TaskStatus.FAILED:
                break

        if self._all_steps_terminal(task):
            if task.failed_steps:
                transition(task, TaskStatus.FAILED)
            else:
                transition(task, TaskStatus.COMPLETED)
                task.completion_result = {
                    "outcome": "completed",
                    "steps": len(task.steps),
                    "evidence": len(task.evidence),
                    "capability_id": task.capability_id,
                }
            task.last_updated = _now()
            self.store.save(task)
        else:
            # Point current_step at the next step to run so the persisted
            # field always matches what progress reporting derives from state.
            nxt = self._next_step(task)
            if nxt is not None:
                task.current_step = nxt.description
                task.last_updated = _now()
                self.store.save(task)
        return steps_run

    def _next_step(self, task: Task) -> Optional[TaskStep]:
        for s in task.steps:
            # A persisted RUNNING step has an unresolved outcome. Never skip
            # past it and execute later steps in the same task; restart
            # recovery will reconcile it according to its replay policy.
            if s.status == TaskStepStatus.RUNNING:
                return None
            if s.status == TaskStepStatus.PENDING:
                return s
        return None

    def _guards_pass(self, task: Task, step: TaskStep) -> bool:
        if step.run_unless_step:
            ref = task.step_by_id(step.run_unless_step)
            if ref is not None and ref.result.get(step.run_unless_key or "found"):
                return False
        if step.run_if_step:
            ref = task.step_by_id(step.run_if_step)
            if ref is not None and not ref.result.get(step.run_if_key or "found"):
                return False
        return True

    def _execute(self, task: Task, step: TaskStep, ctx: ExecutionContext) -> tuple[bool, dict, list]:
        try:
            return execute_step(task, step, ctx)
        except Exception as e:
            return (False, {}, [Evidence(
                step=step.action, label="exception",
                detail=f"{type(e).__name__}: {e}", success=False,
                data={"error": str(e)},
            )])

    @staticmethod
    def _all_steps_terminal(task: Task) -> bool:
        if not task.steps:
            return False
        return all(s.status in (TaskStepStatus.COMPLETED, TaskStepStatus.SKIPPED, TaskStepStatus.FAILED) for s in task.steps)

    # ── Context rendering ─────────────────────────────────────────────

    def render_state(self, identity_id: str) -> str:
        """Human-readable executive state injected into the identity context.

        Includes active tasks AND recent completed/failed tasks so the identity
        can answer "did you finish?" truthfully from persisted state.
        """
        active = self.active_tasks(identity_id)
        lines: list[str] = []
        if active:
            lines.append("## Executive State (live task engine)")
            lines.append("You are committed to the following active work. Do NOT restart it — continue it.")
            for i, task in enumerate(active[:3], 1):
                lines.append(f"\nTask {i}: {task.goal}  [{task.status.value}, {task.progress * 100:.0f}%]")
                try:
                    lines.append(render_progress_block(task).replace("\n", "\n  "))
                except Exception:
                    pass
        terminal = self.store.load_terminal(identity_id)
        terminal.sort(key=lambda t: t.last_updated, reverse=True)
        recent = terminal[:3]
        if recent:
            lines.append("## Executive History (recently completed/failed work)")
            for t in recent:
                outcome = t.status.value
                lines.append(f"- {outcome}: {t.goal[:100]}  [{t.progress * 100:.0f}%]")
        return "\n".join(lines)

    # ── Cleanup ───────────────────────────────────────────────────────

    def shutdown(self) -> None:
        self.scheduler.stop()
        unregister_executive(self)
