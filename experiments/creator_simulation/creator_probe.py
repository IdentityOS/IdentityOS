from __future__ import annotations

from pathlib import Path
from typing import Any

import tempfile

from identityos import Identity

from core.capabilities.base import Capability, Skill, object_schema
from core.capabilities.registry import register
from core.capabilities.result import CapabilityResult


@register
class CreatorProbeCapability(Capability):
    id = "creator_probe"
    name = "Creator Probe"
    version = "1.0.0"
    author = "Daedalus"
    description = "Runs Identity SDK lifecycle probe"
    permissions = ["filesystem"]
    dependencies = ["identityos", "tempfile"]

    def install(self, identity_id: str, storage: Any) -> None:
        # Record that the capability is installed; no persistent state required.
        storage.save(identity_id, f"capability.{self.id}", {"installed": True})

    def uninstall(self, identity_id: str, storage: Any) -> None:
        storage.delete(identity_id, f"capability.{self.id}")

    def prompts(self, identity_id: str) -> list[str]:
        return ["Use creator_probe.run to execute the SDK lifecycle probe."]

    def skills(self) -> list[Skill]:
        return [
            Skill(
                name="creator_probe.run",
                description="SDK lifecycle probe – creates an identity, stores a memory and a goal, exports, reloads and verifies persistence.",
                input_schema=object_schema({"text": {"type": "string"}}, required=("text",)),
                verification_params={"text": "probe"},
                permission="filesystem",
            )
        ]

    def call(self, skill_name: str, **params: Any) -> CapabilityResult:
        if skill_name != "creator_probe.run":
            return CapabilityResult.fail(self.id, skill_name, "unknown_skill", "Skill not recognized")

        text = str(params.get("text", ""))
        if not text:
            return CapabilityResult.fail(self.id, skill_name, "invalid_input", "'text' parameter is required")

        # Begin SDK lifecycle probe
        try:
            # Create a temporary directory that will survive until the object is garbage‑collected.
            tmp_dir = tempfile.TemporaryDirectory()
            storage_path = tmp_dir.name

            # Create the identity using the public SDK constructor.
            identity = Identity.create(name="CreatorProbe", identity_id="probe", storage_path=storage_path)

            # Remember a piece of text and set a goal.
            identity.remember(text)
            identity.goal(text)

            # Export the identity to a JSON file inside the temporary directory.
            export_path = Path(storage_path) / "identity.json"
            identity.export(str(export_path))

            # Load the same identity from storage.
            loaded = Identity.load("probe", storage_path=storage_path)

            # Verify that the memory persisted.
            memories = loaded.memories()
            memory_persisted = any(text in str(m) for m in memories)

            # Verify that the goal persisted.
            goals = loaded.goals("all")
            goal_persisted = any(g.get("title") == text for g in goals)

            # Verify that the export file exists.
            export_exists = export_path.is_file()

            # Clean up the temporary directory reference – the directory will be removed when the object is GC'd.
            # (We deliberately keep the reference alive until the end of this call.)

            data = {
                "text": text,
                "memory_persisted": memory_persisted,
                "goal_persisted": goal_persisted,
                "export_exists": export_exists,
            }

            if all([memory_persisted, goal_persisted, export_exists]):
                return CapabilityResult.ok(self.id, skill_name, data, source="creator_probe")
            else:
                return CapabilityResult.fail(
                    self.id,
                    skill_name,
                    "verification_failed",
                    f"Verification failed: memory={memory_persisted}, goal={goal_persisted}, export={export_exists}",
                )
        except Exception as exc:
            return CapabilityResult.fail(self.id, skill_name, "exception", str(exc))
