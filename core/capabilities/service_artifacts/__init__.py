from core.capabilities.base import Capability, Skill, object_schema
from core.capabilities.registry import register
from core.capabilities.result import CapabilityResult
from core.prometheus.service_artifacts import execute, validate


@register
class ServiceArtifacts(Capability):
    id = "service_artifacts"
    name = "Governed local service artifacts"
    version = "1.0.0"
    permissions = ["local"]
    description = "Fingerprint-verified, bounded local transformation artifacts"
    default_grants = []

    @classmethod
    def inspect_installation(cls, config):
        # Constructor only retains configuration; skills validates fingerprints.
        return {"skills": cls(config).skills(), "readiness": "ready"}

    def install(self, identity_id, storage):
        self.skills()  # Configuration is persisted by CapabilityRegistry.

    def uninstall(self, identity_id, storage):
        return None  # No separate mutable state.

    def prompts(self, identity_id):
        return []  # Artifact text never becomes a system prompt.

    def skills(self):
        skills = []
        for name, bundle in self._config.get("bundles", {}).items():
            if validate(bundle["document"]) != bundle["fingerprint"] or bundle["document"]["skill"] != name:
                raise ValueError("artifact fingerprint mismatch")
            skills.append(
                Skill(
                    name=name,
                    description="Bounded local transformation",
                    permission="local",
                    input_schema=object_schema({"value": {"type": "string"}}, required=("value",)),
                )
            )
        return skills

    def call(self, skill_name, **params):
        try:
            self.skills()  # verify on every invocation, not only installation
            document = self._config["bundles"][skill_name]["document"]
            return CapabilityResult.from_data(
                self.id, skill_name, execute(document, params["value"]), source="local-transform-v1"
            )
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            return CapabilityResult.fail(self.id, skill_name, type(exc).__name__, str(exc))
