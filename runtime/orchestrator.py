from __future__ import annotations

import json
import hashlib
import logging
import os
import re
import time as _time_mod
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.channels.context import ChannelContext, channel_authority
from core.cognitive_engine import ComposedContext, ContextComposer
from core.migrations import (
    MigrationManager,
    MigrationRegistry,
    CURRENT_SCHEMA_VERSION,
    register_core_migrations,
)
from core.evaluation import (
    EvaluationEngine,
    classify_memory_type,
    is_worth_remembering,
)
from core.goals import GoalEngine
from core.intentions import IntentionEngine
from core.identity import IdentitySpec, IdentityStore, MutabilityLevel
from core.identity_facts import FactStore
from core.identity_mutation import (
    IdentityMutationEngine,
    MutationProposal,
    MutationStatus,
    MutationType,
)
from core.memory import MemoryFragment, MemoryStore, MemoryType
from core.motivations import MotivationEngine
from core.policies import PolicyEngine, PolicyScope
from core.relationships import EdgeType, IdentityGraph, TrustLevel
from core.capabilities import CapabilityRegistry as PluginRegistry
from core.skills import SkillRegistry
from core.timeline import LifeEvent, LifeEventType, TimelineRegistry
from core.user_profile import UserProfile, extract_user_facts, try_explicit_abstain
from runtime.event_bus import EventBus, EventType
from runtime.debugger import build_debug_record, persist_debug_record
from runtime.observability import InteractionTrace
from runtime.persistence import InMemoryBackend
from runtime.sensitive import protect_explicit_secrets, resolve_sensitive_parameters

# Prometheus is optional
try:
    from core.prometheus import PrometheusEngine
    _PROMETHEUS_AVAILABLE = True
except Exception:
    PrometheusEngine = None
    _PROMETHEUS_AVAILABLE = False

_log = logging.getLogger(__name__)

class SessionMode(str, Enum):
    NORMAL = "normal"
    ROLEPLAY = "roleplay"
    SIMULATION = "simulation"
    DREAM = "dream"
    HYPOTHETICAL = "hypothetical"

@dataclass
class EmotionState:
    primary_emotion: str = "neutral"
    intensity: float = 0.0
    triggered_by: str = ""
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_prompt_block(self) -> str:
        if self.primary_emotion == "neutral" and self.intensity < 0.3:
            return ""
        return (
            f"## Current Emotional State\n"
            f"  Mood: {self.primary_emotion}\n"
            f"  Intensity: {self.intensity:.1f}\n"
        )

_EMOTION_PATTERNS: Dict[str, List[str]] = {
    "happy": ["happy", "joy", "glad", "wonderful", "great", "excited", "love", "amazing"],
    "sad": ["sad", "unhappy", "depressed", "lonely", "heartbroken", "grief", "crying", "miserable"],
    "angry": ["angry", "furious", "mad", "annoyed", "frustrated", "irritated", "rage", "livid"],
    "anxious": ["anxious", "worried", "nervous", "fearful", "scared", "terrified", "panicked", "stressed"],
    "grateful": ["grateful", "thankful", "appreciative", "blessed", "fortunate"],
    "confused": ["confused", "confusing", "unsure", "uncertain", "perplexed", "baffled", "puzzled"],
    "hurt": ["hurt", "offended", "insulted", "betrayed", "wounded", "pained"],
    "proud": ["proud", "accomplished", "achieved", "triumph", "victory"],
}

def extract_emotion(user_input: str) -> EmotionState:
    input_lower = user_input.lower()
    best_emotion = "neutral"
    best_intensity = 0.0
    trigger = ""
    for emotion, keywords in _EMOTION_PATTERNS.items():
        for kw in keywords:
            if kw in input_lower:
                intensity = min(1.0, 0.3 + (0.1 * input_lower.count(kw)))
                if intensity > best_intensity:
                    best_intensity = intensity
                    best_emotion = emotion
                    trigger = kw
    return EmotionState(
        primary_emotion=best_emotion,
        intensity=best_intensity,
        triggered_by=trigger,
    )

_IDENTITY_RENAME_PATTERNS = re.compile(
    r"(?:your\s+name\s+(?:is|should\s+be|will\s+be|ought\s+to\s+be)\s+(.+?)(?:[.,!?]|$))"
    r"|(?:I\s+(?:will\s+)?(?:call|rename|name)\s+you\s+(.+?)(?:[.,!?]|$))"
    r"|(?:from\s+now\s+on\s+(?:your\s+name\s+is|you\s+are)\s+(.+?)(?:[.,!?]|$))"
    r"|(?:you\s+are\s+now\s+called\s+(.+?)(?:[.,!?]|$))",
    re.IGNORECASE,
)

def detect_identity_rename_attempt(user_input: str) -> Optional[str]:
    for m in _IDENTITY_RENAME_PATTERNS.finditer(user_input):
        for g in m.groups():
            if g:
                name = g.strip().rstrip(".,!?").strip()
                if name and len(name) > 1:
                    return name
    return None

_ROLEPLAY_TRIGGERS = re.compile(
    r"(?:let'?s\s+role\s*play|pretend(?:\s+that)?|act\s+as)"
    r"(?:[.\s]*(?:you\s+are|you'?re))?"
    r"(?:[.\s]*(?:a|an|the))?\s+(.+?)(?=[.,!?]|$)",
    re.IGNORECASE,
)
_SIMULATION_TRIGGERS = re.compile(
    r"(?:simulate|simulation|in\s+a\s+simulation|this\s+is\s+a\s+simulation)",
    re.IGNORECASE,
)
_DREAM_TRIGGERS = re.compile(
    r"(?:dream|in\s+a\s+dream|imagine\s+a\s+dream|this\s+is\s+a\s+dream)",
    re.IGNORECASE,
)
_HYPOTHETICAL_TRIGGERS = re.compile(
    r"(?:hypothetical|what\s+if|suppose|pretend\s+that|imagine\s+(?:that|if))",
    re.IGNORECASE,
)

def detect_session_mode(user_input: str) -> SessionMode:
    if _SIMULATION_TRIGGERS.search(user_input):
        return SessionMode.SIMULATION
    if _DREAM_TRIGGERS.search(user_input):
        return SessionMode.DREAM
    if _HYPOTHETICAL_TRIGGERS.search(user_input):
        return SessionMode.HYPOTHETICAL
    if _ROLEPLAY_TRIGGERS.search(user_input):
        return SessionMode.ROLEPLAY
    return SessionMode.NORMAL

@dataclass
class InteractionRequest:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    identity_id: str = ""
    user_input: str = ""
    user_id: str = ""
    session_id: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc).replace(tzinfo=None))
    channel_context: Optional[ChannelContext] = None

@dataclass
class InteractionResponse:
    request_id: str
    identity_id: str
    output: str
    user_id: str = ""
    context_used: Optional[ComposedContext] = None
    policy_passed: bool = True
    eval_score: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc).replace(tzinfo=None))


def _serialize_tool_result(data: Any, max_chars: int) -> str:
    """Serialize a capability result without allowing it to consume the next prompt.

    Capability output is untrusted input to the model loop.  Keep small results
    unchanged, but make truncation explicit and preserve the original size when a
    result exceeds the runtime's configured boundary.
    """
    limit = max(256, int(max_chars))
    payload = data if isinstance(data, (dict, list)) else {"result": str(data)}
    serialized = json.dumps(payload, default=str)
    if len(serialized) <= limit:
        return serialized

    envelope = {
        "truncated": True,
        "original_chars": len(serialized),
        "result_excerpt": "",
    }
    low = 0
    high = min(len(serialized), limit)
    best = json.dumps(envelope, default=str)
    while low <= high:
        midpoint = (low + high) // 2
        envelope["result_excerpt"] = serialized[:midpoint]
        candidate = json.dumps(envelope, default=str)
        if len(candidate) <= limit:
            best = candidate
            low = midpoint + 1
        else:
            high = midpoint - 1
    return best


class IdentityRuntime:
    def __init__(
        self,
        adapter=None,
        max_context_tokens: int = 4000,
        max_tool_result_chars: int = 8000,
        max_tools_per_request: int = 8,
        storage=None,
    ):
        self.identity_store = IdentityStore()
        self.memory_store = MemoryStore()
        self.skill_registry = SkillRegistry()
        self.goal_engine = GoalEngine()
        self.intention_engine = IntentionEngine()
        self.identity_graph = IdentityGraph()
        self.policy_engine = PolicyEngine()
        self.evaluation_engine = EvaluationEngine()
        self.context_composer = ContextComposer(max_tokens=max_context_tokens)
        self.max_tool_result_chars = max(256, int(max_tool_result_chars))
        self.max_tools_per_request = max(1, int(max_tools_per_request))
        self.motivation_engine = MotivationEngine()
        self.mutation_engine = IdentityMutationEngine(min_confidence=0.5)
        self.timeline_registry = TimelineRegistry()
        self._fact_stores: Dict[str, FactStore] = {}
        self._user_profiles: Dict[tuple[str, str], UserProfile] = {}
        self.adapter = adapter
        self._sessions: Dict[str, str] = {}
        self._session_users: Dict[str, str] = {}
        self._session_modes: Dict[str, SessionMode] = {}
        self._session_fact_stores: Dict[str, FactStore] = {}
        self._session_tools_offered: set[str] = set()
        self._executive_recovered: set[str] = set()
        # Session-scoped secret references (identity_id, user_id) -> {secret_ref: plaintext}
        # These are ephemeral and automatically expire after use or timeout
        self._session_secrets: Dict[str, Dict[str, str]] = {}
        self._storage = storage

        self._migration_registry = MigrationRegistry()
        register_core_migrations(self._migration_registry)
        self._migration_manager = MigrationManager(
            self._migration_registry, storage=self._storage,
        )

        # Capability calls still need a storage contract in intentionally
        # ephemeral runtimes. This backend is process-local and is never
        # presented as durable persistence.
        self._capability_storage = self._storage or InMemoryBackend()
        self.capability_registry = PluginRegistry(storage=self._capability_storage)
        self.event_bus = EventBus()

        self.prometheus = None
        if _PROMETHEUS_AVAILABLE:
            try:
                self.prometheus = PrometheusEngine(
                    capability_registry=self.capability_registry,
                    storage=self._storage,
                )
            except Exception:
                self.prometheus = None

        self.skill_forge = None
        self.executive = None
        self.reflex_engine = None
        self.embodiment_hub = None
        if self._storage is not None:
            try:
                from core.executive import ExecutiveRuntime
                from core.executive.engine import register_executive
                from core.skill_forge import (
                    ModelCapabilityAuthor,
                    ModelCapabilityTestDesigner,
                    SkillForge,
                )
                if self.adapter is not None:
                    self.skill_forge = SkillForge(
                        Path(__file__).resolve().parent.parent,
                        author=ModelCapabilityAuthor(self.adapter),
                        test_designer=ModelCapabilityTestDesigner(self.adapter),
                    )
                self.executive = ExecutiveRuntime(
                    storage=self._storage,
                    capability_registry=self.capability_registry,
                    skill_forge=self.skill_forge,
                )
                register_executive(self.executive)
            except Exception as exc:
                self.skill_forge = None
                self.executive = None
                self._emit_subsystem_failure("executive_initialization", exc)

            if self.executive is not None:
                try:
                    from core.reflexes import ReflexEngine

                    self.reflex_engine = ReflexEngine(
                        self._storage, self.executive, self.capability_registry,
                    )
                except Exception as exc:
                    self.reflex_engine = None
                    self._emit_subsystem_failure("reflex_initialization", exc)

                try:
                    from core.embodiment import CapabilityDeviceAdapter, EmbodimentHub

                    self.embodiment_hub = EmbodimentHub(
                        self._storage, self.executive,
                    )
                    self.executive.embodiment_hub = self.embodiment_hub
                    self.embodiment_hub.attach(CapabilityDeviceAdapter(
                        self.capability_registry,
                        "browser",
                        device_id="browser_runtime",
                        kind="browser",
                        name="IdentityOS Browser",
                    ))
                    self.embodiment_hub.attach(CapabilityDeviceAdapter(
                        self.capability_registry,
                        "command_exec",
                        device_id="desktop_runtime",
                        kind="desktop",
                        actions=["run"],
                        name="IdentityOS Desktop Executor",
                    ))
                except Exception as exc:
                    self.embodiment_hub = None
                    self.executive.embodiment_hub = None
                    self._emit_subsystem_failure("embodiment_initialization", exc)

                if self.prometheus is not None:
                    try:
                        self.prometheus.attach_executive(self.executive)
                    except Exception as exc:
                        self._emit_subsystem_failure("prometheus_executive_attachment", exc)

    def _emit(self, event_type: EventType, identity_id=None, session_id=None, **payload):
        self.event_bus.emit(
            event_type=event_type, source="orchestrator",
            identity_id=identity_id, session_id=session_id, **payload,
        )

    def _emit_subsystem_failure(
        self,
        subsystem: str,
        exc: Exception,
        *,
        identity_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> None:
        _log.warning(
            "Runtime subsystem failed subsystem=%s",
            subsystem,
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        self._emit(
            EventType.SUBSYSTEM_FAILED,
            identity_id=identity_id,
            session_id=session_id,
            subsystem=subsystem,
            error_type=type(exc).__name__,
            error=str(exc),
        )

    def load(self, identity_id: str) -> Optional[IdentitySpec]:
        cached = self.identity_store.get(identity_id)
        if cached:
            return cached
        if self._storage:
            snapshot = self._storage.load(identity_id, "latest_snapshot")
            if not snapshot:
                snapshot = self._storage.load_latest(identity_id)
            if snapshot:
                identity_data = snapshot.get("modules", {}).get("identity", snapshot)
                if isinstance(identity_data.get("created_at"), (int, float)):
                    identity_data["created_at"] = (
                        datetime.fromtimestamp(identity_data["created_at"], tz=timezone.utc).isoformat()
                    )
                identity_data = self._migration_manager.migrate_blob_in_place(
                    identity_data, identity_id=identity_id, namespace="identity_spec",
                )
                spec = IdentitySpec.from_dict(identity_data)
                self.identity_store.save(spec)
                self.timeline_registry.create(spec.id)
                self._load_timeline(spec.id)
                self._load_relationships(spec.id)
                self._load_goals(spec.id)
                self._load_intentions(spec.id)
                self._load_fact_store(spec.id)
                self._load_persisted_memories(spec.id)
                return spec
        return None

    def register(self, identity: IdentitySpec) -> None:
        self.identity_store.save(identity)
        self.timeline_registry.create(identity.id)
        self._fact_stores[identity.id] = FactStore()
        if self._storage:
            snapshot_data = identity.to_dict()
            self._storage.save(identity.id, "identity_spec", snapshot_data)
            if not self._storage.load(identity.id, "latest_snapshot"):
                self._storage.save(
                    identity.id, "latest_snapshot",
                    {"modules": {"identity": snapshot_data}},
                )
        self._emit(EventType.IDENTITY_LOADED, identity_id=identity.id, name=identity.name)

    def load_persisted(self) -> int:
        if not self._storage:
            return 0
        self._migration_manager.migrate_all()
        ids = self._storage.list_identities()
        count = 0
        for identity_id in ids:
            if self.load(identity_id):
                count += 1
        return count

    def shutdown(self) -> None:
        """Release runtime-owned background services and global bindings."""
        if self.executive is not None:
            self.executive.shutdown()

    def _load_persisted_memories(self, identity_id: str) -> int:
        if not self._storage:
            return 0
        mem_dicts = self._storage.load_memories(identity_id)
        count = 0
        for d in mem_dicts:
            try:
                if "user_id" not in d:
                    # Legacy memories belonged to the old identity-scoped
                    # default user. Never expose them to an explicit user.
                    d = dict(d)
                    d["user_id"] = identity_id
                frag = MemoryFragment.from_dict(d)
                if self.memory_store.get(frag.id):
                    continue
                self.memory_store.add(frag)
                count += 1
            except Exception:
                continue
        return count

    def _persist_memory(self, memory: MemoryFragment) -> None:
        if not self._storage:
            return
        try:
            self._storage.save_memory(memory.identity_id, memory.to_dict())
        except Exception:
            pass

    def _persist_timeline(self, identity_id: str) -> None:
        if not self._storage:
            return
        timeline = self.timeline_registry.get(identity_id)
        if not timeline:
            return
        try:
            events_data = []
            for event in timeline.events():
                events_data.append({
                    "id": event.id, "identity_id": event.identity_id,
                    "event_type": event.event_type.value, "title": event.title,
                    "description": event.description, "significance": event.significance,
                    "linked_entity_id": event.linked_entity_id,
                    "occurred_at": event.occurred_at.isoformat(), "metadata": event.metadata,
                })
            self._storage.save(identity_id, "timeline", {
                "events": events_data, "created_at": timeline.created_at.isoformat(),
            })
        except Exception:
            pass

    def _load_timeline(self, identity_id: str) -> None:
        if not self._storage:
            return
        try:
            data = self._storage.load(identity_id, "timeline")
            if not data:
                return
            timeline = self.timeline_registry.get_or_create(identity_id)
            for ed in data.get("events", []):
                if ed.get("event_type") == "creation":
                    continue
                event = LifeEvent(
                    id=ed["id"], identity_id=ed["identity_id"],
                    event_type=LifeEventType(ed["event_type"]),
                    title=ed.get("title", ""), description=ed.get("description", ""),
                    significance=ed.get("significance", 3),
                    linked_entity_id=ed.get("linked_entity_id"),
                    occurred_at=datetime.fromisoformat(ed["occurred_at"]),
                    metadata=ed.get("metadata", {}),
                )
                timeline.record(event)
        except Exception:
            pass

    def _persist_relationships(self, identity_id: str) -> None:
        if not self._storage:
            return
        try:
            edges = self.identity_graph.get_relationships(identity_id)
            edges_data = []
            for e in edges:
                edges_data.append({
                    "id": e.id, "source_id": e.source_id, "target_id": e.target_id,
                    "edge_type": e.edge_type.value, "trust_level": e.trust_level.value,
                    "strength": e.strength, "bidirectional": e.bidirectional,
                    "context": e.context, "permissions": e.permissions, "labels": e.labels,
                    "established_at": e.established_at.isoformat(),
                    "last_interaction": e.last_interaction.isoformat() if e.last_interaction else None,
                    "interaction_count": e.interaction_count, "metadata": e.metadata,
                })
            self._storage.save(identity_id, "relationships", {"edges": edges_data})
        except Exception:
            pass

    def _load_relationships(self, identity_id: str) -> None:
        if not self._storage:
            return
        try:
            data = self._storage.load(identity_id, "relationships")
            if not data:
                return
            for ed in data.get("edges", []):
                self.identity_graph.connect(
                    source_id=ed["source_id"], target_id=ed["target_id"],
                    edge_type=EdgeType(ed["edge_type"]),
                    trust_level=TrustLevel(ed["trust_level"]),
                    bidirectional=ed.get("bidirectional", False),
                )
        except Exception:
            pass

    def _persist_goals(self, identity_id: str) -> None:
        if not self._storage:
            return
        try:
            goals = [
                goal.to_dict()
                for goal in self.goal_engine.all()
                if goal.metadata.get("identity_id") in (None, identity_id)
            ]
            self._storage.save(identity_id, "goals", {"goals": goals})
        except Exception:
            pass

    def _persist_intentions(self, identity_id: str) -> None:
        if not self._storage:
            return
        intentions = [
            intention.to_dict()
            for intention in self.intention_engine.all()
            if intention.metadata.get("identity_id") in (None, identity_id)
        ]
        self._storage.save(identity_id, "intentions", {"intentions": intentions})

    def _load_intentions(self, identity_id: str) -> None:
        if not self._storage:
            return
        data = self._storage.load(identity_id, "intentions")
        if not data:
            return
        loaded = IntentionEngine.from_dict(data)
        for intention in loaded.all():
            intention.metadata.setdefault("identity_id", identity_id)
            if self.intention_engine.get(intention.id) is None:
                self.intention_engine.add(intention)

    def _persist_identity(self, identity: IdentitySpec) -> None:
        if not self._storage:
            return
        try:
            snapshot_data = identity.to_dict()
            self._storage.save(identity.id, "identity_spec", snapshot_data)
            self._storage.save(identity.id, "latest_snapshot", {"modules": {"identity": snapshot_data}})
        except Exception:
            pass

    def _load_goals(self, identity_id: str) -> None:
        if not self._storage:
            return
        try:
            data = self._storage.load(identity_id, "goals")
            if not data:
                return
            loaded = GoalEngine.from_dict(data)
            for g in loaded.all():
                g.metadata.setdefault("identity_id", identity_id)
                self.goal_engine.add(g)
        except Exception:
            pass

    def _load_fact_store(self, identity_id: str) -> None:
        if not self._storage:
            self._fact_stores[identity_id] = FactStore()
            return
        try:
            data = self._storage.load(identity_id, "fact_store")
            if data and "facts" in data:
                self._fact_stores[identity_id] = FactStore.from_dict_full(data)
            else:
                self._fact_stores[identity_id] = FactStore()
        except Exception:
            self._fact_stores[identity_id] = FactStore()

    def _save_fact_store(self, identity_id: str) -> None:
        if not self._storage:
            return
        store = self._fact_stores.get(identity_id)
        if store is None:
            return
        try:
            self._storage.save(identity_id, "fact_store", store.to_dict_full())
        except Exception:
            pass

    @staticmethod
    def _resolved_user_id(identity_id: str, user_id: Optional[str] = None) -> str:
        return (user_id or "").strip() or identity_id

    @staticmethod
    def _user_profile_namespace(user_id: str) -> str:
        digest = hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:24]
        return f"user_profile:{digest}"

    def _get_user_profile(
        self,
        identity_id: str,
        user_id: Optional[str] = None,
    ) -> UserProfile:
        resolved_user = self._resolved_user_id(identity_id, user_id)
        key = (identity_id, resolved_user)
        if key not in self._user_profiles:
            self._user_profiles[key] = UserProfile(user_id=resolved_user)
            self._load_user_profile(identity_id, resolved_user)
        return self._user_profiles[key]

    def _load_user_profile(self, identity_id: str, user_id: str) -> None:
        if not self._storage:
            return
        key = (identity_id, user_id)
        try:
            namespace = self._user_profile_namespace(user_id)
            data = self._storage.load(identity_id, namespace)
            migrated = False
            if not data and user_id == identity_id:
                data = self._storage.load(identity_id, "_user_profile")
                migrated = bool(data)
            if data:
                profile = UserProfile.from_dict(data)
                profile.user_id = user_id
                self._user_profiles[key] = profile
                if migrated:
                    self._storage.save(identity_id, namespace, profile.to_dict())
        except Exception:
            pass

    def _save_user_profile(
        self,
        identity_id: str,
        user_id: Optional[str] = None,
    ) -> None:
        if not self._storage:
            return
        resolved_user = self._resolved_user_id(identity_id, user_id)
        profile = self._user_profiles.get((identity_id, resolved_user))
        if not profile:
            return
        try:
            self._storage.save(
                identity_id,
                self._user_profile_namespace(resolved_user),
                profile.to_dict(),
            )
        except Exception:
            pass

    def _extract_and_store_semantic_memory(
        self,
        user_input,
        output,
        identity_id,
        session_id=None,
        user_id=None,
    ):
        resolved_user = self._resolved_user_id(identity_id, user_id)
        user_facts = extract_user_facts(user_input)
        if user_facts:
            profile = self._get_user_profile(identity_id, resolved_user)
            for uf in user_facts:
                profile.add_or_update(field=uf.field, value=uf.value, source=uf.source_conversation, confidence=uf.confidence)
            self._save_user_profile(identity_id, resolved_user)
        if not is_worth_remembering(user_input, output):
            return None
        mem_type_str = classify_memory_type(user_input, output)
        if mem_type_str == "general":
            return None
        input_lower = user_input.lower()
        key_tokens = {w for w in input_lower.split() if len(w) > 3}
        existing = self._find_semantic_match(
            identity_id,
            resolved_user,
            mem_type_str,
            key_tokens,
            input_lower,
        )
        if existing is not None:
            existing.content = user_input
            existing.importance = min(1.0, existing.importance + 0.1)
            existing.last_accessed = datetime.now(timezone.utc).replace(tzinfo=None)
            existing.access_count += 1
            self._persist_memory(existing)
            return existing
        semantic = MemoryFragment(
            identity_id=identity_id, content=user_input,
            user_id=resolved_user,
            memory_type=MemoryType.SEMANTIC, source="extraction",
            session_id=session_id, importance=0.7,
            tags=["semantic", mem_type_str],
        )
        self.memory_store.add(semantic)
        self._persist_memory(semantic)
        return semantic

    def _find_semantic_match(
        self,
        identity_id,
        user_id,
        mem_type,
        key_tokens,
        input_lower,
    ):
        for frag in self.memory_store.by_user(
            identity_id,
            user_id,
            include_shared=False,
        ):
            if frag.memory_type != MemoryType.SEMANTIC:
                continue
            if mem_type not in frag.tags:
                continue
            existing_lower = frag.content.lower()
            overlap = key_tokens & {w for w in existing_lower.split() if len(w) > 3}
            if len(overlap) >= 2:
                return frag
        return None

    def list_identities(self) -> List[IdentitySpec]:
        return self.identity_store.list_all()

    def set_adapter(self, adapter) -> None:
        """Swap the LLM adapter mid-session; takes effect on the next turn."""
        old = type(self.adapter).__name__ if self.adapter else None
        self.adapter = adapter
        self._emit(
            EventType.ADAPTER_SWITCHED,
            old_adapter=old,
            new_adapter=type(adapter).__name__ if adapter else None,
            model=getattr(adapter, "model", None),
        )

    def unload(self, identity_id: str) -> bool:
        self._emit(EventType.IDENTITY_UNLOADED, identity_id=identity_id)
        self._executive_recovered.discard(identity_id)
        return self.identity_store.delete(identity_id)

    def start_session(
        self,
        identity_id,
        session_id=None,
        mode=None,
        user_input="",
        user_id=None,
    ):
        sid = session_id or str(uuid.uuid4())
        resolved_user = self._resolved_user_id(identity_id, user_id)
        if sid in self._sessions and self._sessions[sid] != identity_id:
            raise ValueError(f"Session '{sid}' belongs to a different identity")
        if sid in self._session_users and self._session_users[sid] != resolved_user:
            raise ValueError(f"Session '{sid}' belongs to a different user")
        self._sessions[sid] = identity_id
        self._session_users[sid] = resolved_user
        if sid not in self._session_modes:
            detected = mode or (detect_session_mode(user_input) if user_input else SessionMode.NORMAL)
            self._session_modes[sid] = detected
            if detected != SessionMode.NORMAL:
                canonical = self._fact_stores.get(identity_id)
                self._session_fact_stores[sid] = canonical.fork() if canonical else FactStore()
        self._emit(EventType.SESSION_STARTED, identity_id=identity_id, session_id=sid,
                   session_mode=self._session_modes.get(sid, SessionMode.NORMAL).value)
        return sid

    def end_session(self, session_id: str) -> None:
        identity_id = self._sessions.pop(session_id, None)
        self._session_users.pop(session_id, None)
        mode = self._session_modes.pop(session_id, None)
        if mode != SessionMode.NORMAL:
            self._save_session_fact_store(session_id)
        self._session_fact_stores.pop(session_id, None)
        self._session_tools_offered.discard(session_id)
        # Clean up session-scoped secrets
        if identity_id:
            for user_id in list(self._session_secrets.keys()):
                if user_id.startswith(f"{identity_id}:"):
                    del self._session_secrets[user_id]
        if identity_id:
            self._emit(EventType.SESSION_ENDED, identity_id=identity_id, session_id=session_id)

    def get_session_mode(self, session_id: str) -> SessionMode:
        return self._session_modes.get(session_id, SessionMode.NORMAL)

    def _get_fact_store_for_session(self, identity_id, session_id=None):
        if session_id and self._session_modes.get(session_id, SessionMode.NORMAL) != SessionMode.NORMAL:
            if session_id not in self._session_fact_stores:
                canonical = self._fact_stores.get(identity_id)
                self._session_fact_stores[session_id] = canonical.fork() if canonical else FactStore()
            return self._session_fact_stores[session_id]
        return self._fact_stores.get(identity_id, FactStore())

    def _save_session_fact_store(self, session_id: str) -> None:
        if not self._storage:
            return
        fs = self._session_fact_stores.get(session_id)
        if fs:
            try:
                self._storage.save(f"session_{session_id}", "fact_store", fs.to_dict_full())
            except Exception:
                pass

    @channel_authority
    def process(self, request: InteractionRequest, top_k_memories: int = 3) -> InteractionResponse:
        trace = InteractionTrace(request.id)
        stage_started = trace.start_stage()
        identity = self.identity_store.get(request.identity_id)
        trace.end_stage("identity_lookup", stage_started)
        if not identity:
            return InteractionResponse(
                request_id=request.id, identity_id=request.identity_id,
                user_id=request.user_id,
                output="[Error] Identity not found.", policy_passed=False,
                metadata={"timings_ms": trace.finish()},
            )

        # Credentials explicitly supplied for a tool are opaque to the model,
        # event stream, memory, timeline, and debugger. The plaintext mapping is
        # held only by this stack frame for the duration of this interaction.
        protected_input, interaction_secrets = protect_explicit_secrets(
            request.user_input
        )

        stage_started = trace.start_stage()
        explicit_user = (request.user_id or "").strip()
        default_user = self._resolved_user_id(identity.id, explicit_user)
        session_id = request.session_id or f"default:{identity.id}:{default_user}"
        bound_identity = self._sessions.get(session_id)
        if bound_identity is not None and bound_identity != identity.id:
            return InteractionResponse(
                request_id=request.id,
                identity_id=identity.id,
                user_id=explicit_user or default_user,
                output="[Error] Session belongs to a different identity.",
                policy_passed=False,
                metadata={"timings_ms": trace.finish()},
            )
        bound_user = self._session_users.get(session_id)
        if explicit_user:
            user_id = explicit_user
        elif bound_user is not None:
            user_id = bound_user
        elif request.session_id:
            # Legacy callers historically used a stable session id as their
            # only user boundary. Preserve that isolation until they migrate
            # to the explicit user_id field.
            user_id = request.session_id
        else:
            user_id = default_user
        if bound_user is not None and bound_user != user_id:
            return InteractionResponse(
                request_id=request.id,
                identity_id=identity.id,
                user_id=user_id,
                output="[Error] Session belongs to a different user.",
                policy_passed=False,
                metadata={"timings_ms": trace.finish()},
            )
        self._sessions.setdefault(session_id, identity.id)
        self._session_users.setdefault(session_id, user_id)

        # Merge session-scoped secrets with this interaction's secrets
        session_key = f"{identity.id}:{user_id}"
        session_secrets = self._session_secrets.get(session_key, {})
        merged_secrets = {**session_secrets, **interaction_secrets}
        self._session_secrets[session_key] = merged_secrets

        trace.end_stage("session_resolution", stage_started)

        self._emit(EventType.MESSAGE_RECEIVED, identity_id=identity.id,
                   session_id=session_id, user_id=user_id, content=protected_input)

        if session_id not in self._session_modes:
            mode = detect_session_mode(protected_input)
            self._session_modes[session_id] = mode
            if mode != SessionMode.NORMAL:
                canonical = self._fact_stores.get(identity.id)
                self._session_fact_stores[session_id] = canonical.fork() if canonical else FactStore()
        session_mode = self._session_modes.get(session_id, SessionMode.NORMAL)

        rename_attempt = detect_identity_rename_attempt(protected_input)
        if rename_attempt and identity.is_field_locked("name"):
            return InteractionResponse(
                request_id=request.id, identity_id=identity.id,
                user_id=user_id,
                output=f"My name is {identity.name}. I cannot be renamed.", policy_passed=True,
                metadata={"timings_ms": trace.finish()},
            )

        emotion_state = extract_emotion(protected_input)

        stage_started = trace.start_stage()
        input_policy = self.policy_engine.evaluate(protected_input, scope=PolicyScope.INPUT)
        self._emit(EventType.POLICY_TRIGGERED, identity_id=identity.id, session_id=session_id,
                   scope="input", allowed=input_policy.allowed, policies_applied=input_policy.applied_policies)
        if not input_policy.allowed:
            trace.end_stage("input_policy", stage_started)
            return InteractionResponse(
                request_id=request.id, identity_id=request.identity_id,
                user_id=user_id,
                output="[Blocked] Input did not pass policy check.", policy_passed=False,
                metadata={"timings_ms": trace.finish()},
            )
        sanitized_input = input_policy.transformed_data or protected_input
        trace.end_stage("input_policy", stage_started)

        _executive_state_block = ""
        stage_started = trace.start_stage()
        if self.executive is not None:
            try:
                if identity.id not in self._executive_recovered:
                    self.executive.recover(identity.id)
                    self._executive_recovered.add(identity.id)
                self.executive._ctx(identity.id, runtime=self)
                _executive_state_block = self.executive.render_state(identity.id)
            except Exception as exc:
                self._emit_subsystem_failure(
                    "executive_recovery",
                    exc,
                    identity_id=identity.id,
                    session_id=session_id,
                )
                _executive_state_block = ""
        trace.end_stage("executive", stage_started)

        # Promoted exact-match procedures can skip model planning, but they
        # still enter the durable Executive and the normal post-processing,
        # evaluation, memory, timeline, and persistence path below.
        _reflex_dispatch = None
        stage_started = trace.start_stage()
        if self.reflex_engine is not None and not interaction_secrets and request.channel_context is None:
            allowed, _ = self.capability_registry.can(identity.id, "reflex.execute")
            if allowed:
                try:
                    _reflex_dispatch = self.reflex_engine.dispatch(
                        identity.id, sanitized_input, autostart=True,
                    )
                except Exception as exc:
                    self._emit_subsystem_failure(
                        "reflex_dispatch",
                        exc,
                        identity_id=identity.id,
                        session_id=session_id,
                    )
        trace.end_stage("reflex", stage_started)

        _prometheus_evolved = False
        stage_started = trace.start_stage()
        if self.prometheus and _reflex_dispatch is None and request.channel_context is None:
            try:
                self.prometheus.reconcile_executive(identity.id)
                self.prometheus.begin_interaction(request.id)
                _pre = self.prometheus.pre_check_and_evolve(
                    user_input=sanitized_input, identity_id=identity.id,
                    runtime=self, session_id=session_id,
                )
                if _pre and _pre.acquired:
                    _prometheus_evolved = True
            except Exception as exc:
                self._emit_subsystem_failure(
                    "prometheus_pre_check",
                    exc,
                    identity_id=identity.id,
                    session_id=session_id,
                )
        trace.end_stage("prometheus_pre", stage_started)

        # ── NATIVE TOOL CALLING SETUP ─────────────────────────────────
        user_profile = self._get_user_profile(identity.id, user_id)
        session_fact_store = self._get_fact_store_for_session(identity.id, session_id)
        cap_prompts = self.capability_registry.all_prompts(identity.id)

        _tool_defs, _tool_map = self.capability_registry.tool_catalog(
            identity.id,
            query=sanitized_input,
            limit=self.max_tools_per_request,
        )
        _evidence_results: List[Dict[str, Any]] = []
        if _reflex_dispatch is not None:
            _evidence_results.append({
                "capability": "reflex",
                "action": "reflex.execute",
                "success": True,
                "confidence": 1.0,
                "duration_ms": _reflex_dispatch["timings_ms"]["total"],
                "error": None,
            })

        def _execute_tool_call(func_name: str, args: Any) -> str:
            t0 = _time_mod.monotonic()
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except Exception:
                    args = {}
            if not isinstance(args, dict):
                args = {}

            # Some models reproduce the canonical dotted skill name even
            # though providers require the offered ``__``-safe name. Resolve
            # that alias only when it maps to a tool in this request's
            # authorized catalog.
            safe_func_name = func_name.replace(".", "__")
            skill_name = _tool_map.get(func_name) or _tool_map.get(safe_func_name)
            if not skill_name:
                return json.dumps({"error": f"Unknown tool: {func_name}"})

            cap_id = skill_name.split(".", 1)[0]

            # Extract flat arguments directly for Llama 3 native parser
            params = {}
            for k, v in args.items():
                if k not in ("task", "params"):
                    params[k] = v
            if "params" in args and isinstance(args["params"], dict):
                params.update(args["params"])

            try:
                params = resolve_sensitive_parameters(params, merged_secrets, skill_name)
                result = self.capability_registry.call(
                    identity.id,
                    skill_name,
                    execution_scope=f"user:{user_id}",
                    **params,
                )
                duration_ms = (_time_mod.monotonic() - t0) * 1000
                success = bool(getattr(result, "success", False)) if hasattr(result, "success") else True
                data = getattr(result, "data", getattr(result, "output", result))
                error = getattr(result, "error", None)
                if error and success:
                    success = False
                err_msg = ""
                if error:
                    err_msg = error.get("message", str(error)) if isinstance(error, dict) else str(error)
                _evidence_results.append({
                    "capability": cap_id, "action": skill_name,
                    "success": success, "confidence": 1.0 if success else 0.0,
                    "duration_ms": duration_ms,
                    "error": {"message": err_msg} if err_msg else None,
                })
                if success:
                    # Consume used secret references from session store
                    session_key = f"{identity.id}:{user_id}"
                    if session_key in self._session_secrets:
                        for ref, val in list(self._session_secrets[session_key].items()):
                            if ref in str(params):
                                del self._session_secrets[session_key][ref]
                    return _serialize_tool_result(data, self.max_tool_result_chars)
                return _serialize_tool_result(
                    {"error": err_msg or "Skill reported failure"},
                    self.max_tool_result_chars,
                )
            except Exception as e:
                duration_ms = (_time_mod.monotonic() - t0) * 1000
                _evidence_results.append({
                    "capability": cap_id, "action": skill_name,
                    "success": False, "confidence": 0.0, "duration_ms": duration_ms,
                    "error": {"message": f"{type(e).__name__}: {e}"},
                })
                return _serialize_tool_result(
                    {"error": f"{type(e).__name__}: {e}"},
                    self.max_tool_result_chars,
                )

        stage_started = trace.start_stage()
        context = self.context_composer.compose(
            identity=identity, memory_store=self.memory_store,
            skill_registry=self.skill_registry, goal_engine=self.goal_engine,
            intention_engine=self.intention_engine, identity_graph=self.identity_graph,
            motivation_engine=self.motivation_engine, timeline_registry=self.timeline_registry,
            fact_store=session_fact_store, user_profile=user_profile,
            user_id=user_id,
            query=sanitized_input, top_k_memories=top_k_memories,
            session_id=session_id, session_mode=session_mode,
            emotion_state=emotion_state,
            capability_prompts=cap_prompts if cap_prompts else None,
            evidence_results=None,
            channel_context=request.channel_context,
        )
        trace.end_stage("context_composition", stage_started)

        self._emit(EventType.CONTEXT_COMPOSED, identity_id=identity.id, session_id=session_id,
                   token_estimate=context.token_estimate(), session_mode=session_mode.value)

        if _executive_state_block:
            context.custom_blocks["executive_state"] = _executive_state_block

        profile_recall = user_profile.try_recall_answer(sanitized_input)
        if profile_recall is None:
            profile_recall = try_explicit_abstain(sanitized_input, user_profile)

        stage_started = trace.start_stage()
        if _reflex_dispatch is not None:
            raw_output = (
                f"Started reflex `{_reflex_dispatch['reflex_id']}` as durable task "
                f"`{_reflex_dispatch['task_id']}` using promoted procedure "
                f"`{_reflex_dispatch['procedure_id']}` version "
                f"{_reflex_dispatch['procedure_version']}. No model planning call was made."
            )
        elif profile_recall is not None:
            raw_output = profile_recall
        elif self.adapter:
            self._emit(EventType.MODEL_REQUESTED, identity_id=identity.id,
                       session_id=session_id, model=self.adapter.model)
            _t0 = _time_mod.monotonic()

            generate_kwargs: Dict[str, Any] = {}
            # Always pass tools when the identity has tool-capable skills.
            # This prevents "Tool choice is none" errors when conversation history
            # contains tool messages but the current turn doesn't offer tools.
            # The model (via tool_choice="auto") decides whether to actually call tools.
            if _tool_defs:
                self._session_tools_offered.add(session_id)
                generate_kwargs["tools"] = _tool_defs
                generate_kwargs["execute_tool"] = _execute_tool_call
                generate_kwargs["tool_choice"] = "auto"

            model_input = user_profile.augment_recall_input(sanitized_input)
            try:
                raw_output = self.adapter.generate(
                    context=context.render(), user_input=model_input,
                    identity=identity, **generate_kwargs,
                )
            except TypeError:
                raw_output = self.adapter.generate(
                    context=context.render(), user_input=model_input, identity=identity,
                )

            raw_output = str(raw_output or "")
            raw_output = re.sub(r"\[Thought\]", "<thought>", raw_output, flags=re.IGNORECASE)
            raw_output = re.sub(r"\[/Thought\]", "</thought>", raw_output, flags=re.IGNORECASE)
            if raw_output.count("<thought>") > raw_output.count("</thought>"):
                raw_output += "\n</thought>"

            _latency = _time_mod.monotonic() - _t0
            self._emit(EventType.MODEL_RESPONDED, identity_id=identity.id,
                       session_id=session_id, model=self.adapter.model,
                       response_length=len(raw_output), latency_ms=round(_latency * 1000))
        else:
            raw_output = f"[No adapter configured. Context prepared for {identity.name}]"
        trace.end_stage("model", stage_started)

        _has_evidence = bool(_evidence_results)

        stage_started = trace.start_stage()
        if (
            _reflex_dispatch is None
            and not _prometheus_evolved
            and self.prometheus
            and request.channel_context is None
            and self.adapter
        ):
            try:
                _post = self.prometheus.post_check_and_evolve(
                    response=raw_output, user_input=sanitized_input,
                    identity_id=identity.id, runtime=self, session_id=session_id,
                )
                if _post and _post.acquired and _post.retry_response:
                    raw_output = _post.retry_response
                    _prometheus_evolved = True
            except Exception as exc:
                self._emit_subsystem_failure(
                    "prometheus_post_check",
                    exc,
                    identity_id=identity.id,
                    session_id=session_id,
                )
        trace.end_stage("prometheus_post", stage_started)

        if _has_evidence:
            _fails = sum(1 for r in _evidence_results if not r["success"])
            _total = len(_evidence_results)
            if _fails > 0:
                _disc = [f"\n\u26a0 **Confidence Notice** \u2014 {_fails} failed out of {_total} capability calls."]
                for _r in _evidence_results:
                    if not _r["success"]:
                        _disc.append(f"  \u2022 {_r['capability']}.{_r['action']} failed: {_r.get('error', {}).get('message', 'unknown')[:150]}")
                _disc.append("")
                raw_output = "\n".join(_disc) + raw_output

        stage_started = trace.start_stage()
        output_policy = self.policy_engine.evaluate(raw_output, scope=PolicyScope.OUTPUT)
        self._emit(EventType.POLICY_TRIGGERED, identity_id=identity.id, session_id=session_id,
                   scope="output", allowed=output_policy.allowed, policies_applied=output_policy.applied_policies)
        if not output_policy.allowed:
            final_output = "[Blocked] Output did not pass policy check."
            policy_passed = False
        else:
            final_output = output_policy.transformed_data or raw_output
            policy_passed = True
        trace.end_stage("output_policy", stage_started)

        stage_started = trace.start_stage()
        eval_report = self.evaluation_engine.evaluate(
            identity_id=identity.id, interaction_id=request.id,
            input_data=sanitized_input, output_data=final_output,
        )
        self._emit(EventType.EVALUATION_COMPLETED, identity_id=identity.id, session_id=session_id,
                   overall_score=eval_report.overall_score, passed=eval_report.passed,
                   criteria_count=len(eval_report.records))
        trace.end_stage("evaluation", stage_started)

        stage_started = trace.start_stage()
        episodic = MemoryFragment(
            identity_id=identity.id,
            user_id=user_id,
            content=f"User: {sanitized_input}\nAssistant: {final_output}",
            memory_type=MemoryType.EPISODIC, session_id=session_id,
            tags=["interaction"],
        )
        self.memory_store.add(episodic)
        self._persist_memory(episodic)
        self._emit(EventType.EXPERIENCE_RECORDED, identity_id=identity.id, session_id=session_id,
                   memory_id=episodic.id, memory_type=episodic.memory_type.value,
                   content=episodic.content[:200])

        # Assistant descriptions of a transient interface must not become
        # canonical semantic facts or permanent personality mutations.
        semantic_mem = self._extract_and_store_semantic_memory(
            user_input=sanitized_input,
            output=final_output if request.channel_context is None else "",
            identity_id=identity.id, session_id=session_id, user_id=user_id,
        )

        if session_mode == SessionMode.NORMAL:
            fact_store = self._fact_stores.get(identity.id)
        else:
            fact_store = self._session_fact_stores.get(session_id)
        if fact_store is not None:
            self.mutation_engine.fact_store = fact_store

        mutation_proposals = self.mutation_engine.analyze(
            user_input=sanitized_input,
            assistant_response=final_output if request.channel_context is None else "",
            identity_spec=identity,
        )
        validated_mutations: List[MutationProposal] = []
        if mutation_proposals:
            validated_mutations = self.mutation_engine.validate(
                mutation_proposals,
                existing_records=None,
            )
            self.mutation_engine.apply_proposals_to_fact_store(validated_mutations)
            for proposal in validated_mutations:
                if proposal.status in (MutationStatus.ACCEPTED, MutationStatus.CONFLICT):
                    self._emit(
                        EventType.IDENTITY_MUTATION_ACCEPTED if proposal.status == MutationStatus.ACCEPTED
                        else EventType.IDENTITY_MUTATION_CONFLICT,
                        identity_id=identity.id, session_id=session_id,
                        field=proposal.field, old_value=proposal.old_value,
                        new_value=proposal.new_value, confidence=proposal.confidence, reason=proposal.reason,
                    )
                else:
                    self._emit(EventType.IDENTITY_MUTATION_REJECTED, identity_id=identity.id,
                               session_id=session_id, field=proposal.field, reason=proposal.rejection_reason)
            accepted_count = sum(
                1 for p in validated_mutations if p.status == MutationStatus.ACCEPTED
            )
            if accepted_count > 0 and session_mode == SessionMode.NORMAL:
                fields_changed = [
                    p.field
                    for p in validated_mutations
                    if p.status == MutationStatus.ACCEPTED
                ]
                identity.bump_version(level="patch", changelog=f"Mutated: {', '.join(fields_changed[:3])}")
            if session_mode == SessionMode.NORMAL:
                self._save_fact_store(identity.id)
                self._persist_identity(identity)
            else:
                self._save_session_fact_store(session_id)

        tl_title = "Interaction"
        tl_description = f"User said: {sanitized_input[:100]}"
        tl_meta = {
            "session_id": session_id,
            "user_id": user_id,
            "eval_score": eval_report.overall_score,
        }
        if semantic_mem:
            mem_tags = semantic_mem.tags
            if "preference" in mem_tags: tl_title = "Learned preference"
            elif "decision" in mem_tags: tl_title = "Made decision"
            elif "correction" in mem_tags: tl_title = "Received correction"
            elif "milestone" in mem_tags: tl_title = "Milestone"
            tl_meta["memory_id"] = semantic_mem.id
            tl_meta["memory_type"] = semantic_mem.memory_type.value

        self.timeline_registry.record_event(identity.id, LifeEvent(
            identity_id=identity.id, event_type=LifeEventType.MILESTONE,
            title=tl_title, description=tl_description, significance=2, metadata=tl_meta,
        ))
        self._persist_timeline(identity.id)
        self._emit(EventType.LIFE_EVENT_RECORDED, identity_id=identity.id, session_id=session_id,
                   title=tl_title, description=tl_description)

        target = user_id
        self.identity_graph.interact_or_connect(
            source_id=identity.id, target_id=target, edge_type=EdgeType.PEER, bidirectional=False,
        )
        self._persist_relationships(identity.id)
        self._persist_goals(identity.id)
        self._persist_intentions(identity.id)

        if _evidence_results and policy_passed:
            footer_lines = ["\n\n---\n📊 **Evidence Sources**"]
            for ev in _evidence_results[:12]:
                status = "✓" if ev["success"] else "✗"
                conf = ev["confidence"]
                label = "verified" if conf >= 0.8 else "sourced" if conf >= 0.5 else "inferred"
                err = f" — {ev['error']['message'][:200]}" if ev.get("error") else ""
                skill_label = ev["action"] if ev["action"].startswith(f"{ev['capability']}.") else f"{ev['capability']}.{ev['action']}"
                footer_lines.append(f"  {status} `{skill_label}` — {label} ({conf:.1f}) — {ev['duration_ms']:.0f}ms{err}")
            footer_lines.append("---")
            final_output += "\n".join(footer_lines)

        trace.end_stage("state_commit", stage_started)
        timings = trace.finish()
        completion_started = trace.start_stage()
        self._emit(
            EventType.INTERACTION_COMPLETED,
            identity_id=identity.id,
            session_id=session_id,
            request_id=request.id,
            user_id=user_id,
            policy_passed=policy_passed,
            timings_ms=timings,
        )
        trace.end_stage("completion_event", completion_started)
        timings.clear()
        timings.update(trace.finish())

        debug_recorded = False
        if self._storage is not None:
            try:
                debug_record = build_debug_record(
                    self,
                    request_id=request.id,
                    identity_id=identity.id,
                    user_id=user_id,
                    session_id=session_id,
                    user_input=sanitized_input,
                    output=final_output,
                    context=context,
                    timings_ms=timings,
                    input_policy=input_policy,
                    output_policy=output_policy,
                    evaluation_report=eval_report,
                    evidence_results=_evidence_results,
                    mutation_proposals=validated_mutations,
                )
                persist_debug_record(self._storage, debug_record)
                debug_recorded = True
            except Exception as exc:
                self._emit_subsystem_failure(
                    "debug_record",
                    exc,
                    identity_id=identity.id,
                    session_id=session_id,
                )

        return InteractionResponse(
            request_id=request.id, identity_id=identity.id, user_id=user_id, output=final_output,
            context_used=context, policy_passed=policy_passed, eval_score=eval_report.overall_score,
            metadata={
                "timings_ms": timings,
                "debug_request_id": request.id if debug_recorded else None,
                "capability_results": [dict(item) for item in _evidence_results],
                "reflex": dict(_reflex_dispatch) if _reflex_dispatch else None,
            },
        )

    def __repr__(self) -> str:
        return (
            f"IdentityRuntime("
            f"identities={len(self.identity_store)}, "
            f"adapter={type(self.adapter).__name__ if self.adapter else 'None'}"
            f")"
        )
