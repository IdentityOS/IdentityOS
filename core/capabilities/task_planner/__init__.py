from __future__ import annotations

import re
from typing import Any, Optional

from core.capabilities.base import Capability, Skill, object_schema
from core.capabilities.registry import lookup, register
from core.capabilities.result import CapabilityResult

# Keywords that indicate the goal asks for command execution capability
_COMMAND_INTENT = re.compile(
    r'\b(run|execute|exec|shell|terminal|neofetch|screenfetch)\b|\bcommand'
    r'[s]?\b', re.IGNORECASE
)


@register
class TaskPlannerCapability(Capability):
    id = "task_planner"
    name = "Task Planner"
    version = "1.1.0"
    author = "IdentityOS"
    license = "MIT"
    homepage = "https://github.com/lacebx/IdentityOS"
    description = "Plan multi-step work and queue it in the durable Executive"
    permissions = ["task:execute"]

    def __init__(self, config: Optional[dict] = None) -> None:
        super().__init__(config)
        self._identity_id: Optional[str] = None
        self._storage: Any = None

    def install(self, identity_id: str, storage: Any) -> None:
        self._identity_id = identity_id
        self._storage = storage
        storage.save(identity_id, "capability.task_planner", {"installed_at": None})

    def uninstall(self, identity_id: str, storage: Any) -> None:
        storage.delete(identity_id, "capability.task_planner")

    def prompts(self, identity_id: str) -> list[str]:
        return [
            "## Task Planner Skills (MANDATORY — use for multi-step autonomous tasks)",
            "When asked to create, build, publish, or install anything that requires multiple steps:",
            "  1. Call task_planner.plan_and_execute with your goal as the 'goal' parameter",
            "  2. The planner will commit the steps to the durable Executive and return a task ID",
            "  3. Use Executive task status for observed progress and completion",
            (
                "Example: task_planner.plan_and_execute(goal='create a greeting skill, "
                "validate it, publish it, and install it')"
            ),
        ]

    _SKILLS = [
        Skill(
            name="task_planner.plan_and_execute",
            description=(
                "Plan a multi-step goal and queue it in the durable Executive. "
                "Returns a persistent task ID for progress tracking."
            ),
            permission="task:execute",
            effect="execute",
            input_schema=object_schema(
                {
                    "goal": {"type": "string", "minLength": 1},
                    "steps": {"type": "array"},
                },
                required=("goal",),
            ),
        ),
    ]

    def skills(self) -> list[Skill]:
        return list(self._SKILLS)

    def call(self, skill_name: str, **params: Any) -> CapabilityResult:
        import time as _time
        _t0 = _time.monotonic()
        try:
            dispatch = {
                "task_planner.plan_and_execute": self._plan_and_execute,
            }
            handler = dispatch.get(skill_name)
            if handler is None:
                return CapabilityResult.fail(
                    "task_planner", skill_name, "unknown_skill", f"Unknown skill: {skill_name}"
                )
            data = handler(**params)
            return CapabilityResult.from_data(
                "task_planner",
                skill_name,
                data,
                source="task planner",
                duration_ms=(_time.monotonic() - _t0) * 1000,
            )
        except Exception as e:
            return CapabilityResult.fail(
                "task_planner",
                skill_name,
                type(e).__name__,
                str(e),
                duration_ms=(_time.monotonic() - _t0) * 1000,
            )

    def _plan_and_execute(self, goal: str = "", steps: Optional[list] = None, **kwargs: Any) -> dict[str, Any]:
        """Plan a goal and commit it to the authoritative durable Executive."""
        if not goal and not steps:
            return {"error": "Provide a 'goal' string describing what you want to accomplish."}
        plan = steps or self._generate_plan(goal)
        if not isinstance(plan, list) or not plan:
            return {"error": "Planner produced no executable steps."}
        if self._storage is None or self._identity_id is None:
            return {
                "error": "task planner is not installed into a durable identity runtime",
            }
        from core.executive.engine import get_executive_for

        executive = get_executive_for(self._storage)
        if executive is None:
            return {"error": "no durable Executive is registered"}
        acquisition_steps = [
            step for step in plan if step.get("action") == "request_acquisition"
        ]
        if acquisition_steps:
            if len(plan) != 1:
                return {
                    "error": "capability acquisition must be queued before dependent steps",
                }
            result = self._request_acquisition(acquisition_steps[0].get("params", {}))
            if not result.success:
                return {"error": (result.error or {}).get("message", "acquisition failed")}
            return {**result.data, "plan": plan, "durable": True}

        task = executive.start_task(
            goal=goal,
            identity_id=self._identity_id,
            original_request=goal,
            steps=plan,
            autostart=True,
        )
        return {
            "plan": plan,
            "task_id": task.task_id,
            "status": task.status.value,
            "total_steps": len(task.steps),
            "durable": True,
        }

    def _request_acquisition(self, params: dict) -> CapabilityResult:
        """Delegate generation to the durable Executive/Skill Forge path."""
        cap_id = str(params.get("cap_id", ""))
        goal = str(params.get("goal", ""))
        if not cap_id or not goal:
            return CapabilityResult.fail(
                self.id, "request_acquisition", "invalid_request", "cap_id and goal are required",
            )
        if self._storage is None or self._identity_id is None:
            return CapabilityResult.fail(
                self.id,
                "request_acquisition",
                "executive_unavailable",
                "task planner is not installed into a durable identity runtime",
            )
        from core.acquisition import get_acquisition_provider

        provider = get_acquisition_provider(self._storage)
        if provider is None:
            return CapabilityResult.fail(
                self.id,
                "request_acquisition",
                "executive_unavailable",
                "no durable acquisition provider is registered",
            )
        task, created = provider.request_acquisition(
            identity_id=self._identity_id,
            capability_id=cap_id,
            goal=goal,
            original_request=goal,
        )
        return CapabilityResult.ok(self.id, "request_acquisition", {
            "task_id": task.task_id,
            "status": task.status.value,
            "created": created,
            "capability_id": cap_id,
        }, source="durable executive")

    @staticmethod
    def _generate_plan(goal: str) -> list[dict]:
        """Parse a natural language goal into an ordered list of steps."""
        import re
        gl = goal.lower()

        # Detect command-execution intent (create command_exec + run a real command)
        command = None
        if _COMMAND_INTENT.search(gl):
            m = re.search(r'\b(?:run|execute)\s+(?:the\s+)?["\']?([\w./~][\w./~\-]*)["\']?', gl)
            if m:
                command = m.group(1)
            else:
                for kw in (
                    "neofetch", "screenfetch", "pwd", "ls", "uname", "whoami",
                    "date", "uptime", "hostname", "echo",
                ):
                    if re.search(rf'\b{kw}\b', gl):
                        command = kw
                        break

        # Extract capability name: look for words that appear right after "create", "called", "named", "a", "an"
        cap_name = None
        patterns = [
            r'(?:create|build|make|write)\s+(?:a\s+|an\s+)?(?:capability\s+|skill\s+)?["\']?([a-z_]\w*)["\']?',
            r'(?:called|named)\s+["\']?([a-z_]\w*)["\']?',
            r'(?:capability|skill)\s+["\']?([a-z_]\w*)["\']?',
            r'["\']?([a-z_]\w*_cap)["\']?',
            r'["\']?([a-z_]\w*_skill)["\']?',
        ]
        for pat in patterns:
            m = re.search(pat, gl)
            if m:
                candidate = m.group(1)
                # Skip action verbs that aren't capability names
                if candidate not in (
                    "create", "build", "make", "write", "publish", "install",
                    "validate", "check", "test", "list", "show", "add", "load",
                    "register", "update", "delete", "remove",
                ):
                    cap_name = candidate
                    break

        # Command-execution goals always target the command_exec capability
        if command and "capability" in gl:
            cap_name = "command_exec"
            if "install" not in gl:
                gl = f"{gl} install"
            if "publish" not in gl:
                gl = f"{gl} publish"

        steps = []

        if cap_name:
            try:
                lookup(cap_name)
                capability_exists = True
            except ValueError:
                capability_exists = False

            # Registered capabilities are authoritative. Reuse them instead of
            # replacing their source and marketplace metadata with a generated
            # scaffold merely because the goal also says "create" or "publish".
            if not capability_exists:
                steps.append({
                    "action": "request_acquisition",
                    "params": {"cap_id": cap_name, "goal": goal},
                    "description": f"Queuing truthful acquisition of {cap_name}",
                })

            if capability_exists and ("install" in gl or "add" in gl or "load" in gl):
                steps.append({
                    "action": "install_capability",
                    "params": {"cap_id": cap_name},
                    "description": f"Installing {cap_name} onto identity",
                })

            if command:
                steps.append({
                    "action": "run_command",
                    "params": {"cap_id": cap_name, "command": command},
                    "description": f"Running command: {command}",
                })
        else:
            # No capability name found, do generic actions
            if "publish" in gl or "register" in gl:
                steps.append({"action": "list_capabilities", "params": {}, "description": "Checking registry"})
            if "list" in gl or "show" in gl or "what" in gl:
                steps.append({"action": "list_capabilities", "params": {}, "description": "Listing capabilities"})

        if not steps:
            steps.append({"action": "list_capabilities", "params": {}, "description": "Assessing current state"})

        return steps

    @staticmethod
    def _capability_template(name: str) -> str:
        """Generate a minimal but valid capability Python file."""
        raise RuntimeError(
            "direct capability scaffolding is disabled; use the durable Skill Forge acquisition path"
        )

    @staticmethod
    def _command_exec_template() -> str:
        """Generate a real command-execution capability backed by subprocess."""
        raise RuntimeError(
            "direct capability scaffolding is disabled; install the verified command_exec package"
        )
