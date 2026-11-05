"""
IdentityOS Playground — a first-class developer tool that visualizes
the entire runtime architecture in real time.

Usage:
    python -m runtime.playground
    # -> http://localhost:8000/playground
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import hashlib
import json
import os
import queue
import secrets
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape

from core.cognitive_engine import ComposedContext
from core.evaluation import register_default_criteria
from core.goals import Goal, GoalPriority, GoalScope
from core.identity import IdentitySpec, create_identity
from core.intentions import Intention, IntentionPriority
from identityos.adoption import (
    AdoptionConfigError,
    build_configured_adapter,
    configure_provider,
    list_providers,
    load_config,
    public_status,
    record_verification,
    save_config,
)
from identityos.adoption import initialize as initialize_adoption
from identityos.paths import get_paths
from runtime.event_bus import Event, EventType
from runtime.orchestrator import IdentityRuntime, InteractionRequest, SessionMode
from runtime.persistence import JSONFileBackend

HERE = str(Path(__file__).parent)


def _safe_error(code: str, message: str, *, status_code: int = 400) -> JSONResponse:
    """Return an actionable error without reflecting exception or credential text."""
    return JSONResponse({"error": code, "message": message}, status_code=status_code)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _context_sections(ctx: ComposedContext) -> list[dict]:
    """Extract structured sections from a composed context."""
    sections = []
    blocks = [
        ("Identity", ctx.identity_block, "#58a6ff"),
        ("Memory", ctx.memory_block, "#bc8cff"),
        ("Skills", ctx.skills_block, "#d29922"),
        ("Goals", ctx.goals_block, "#3fb950"),
        ("Relationships", ctx.relationships_block, "#f0883e"),
        ("Motivations", ctx.motivations_block, "#f85149"),
        ("Timeline", ctx.timeline_block, "#58a6ff"),
    ]
    for name, content, color in blocks:
        if content:
            sections.append({
                "name": name,
                "content": content,
                "color": color,
                "chars": len(content),
            })
    for key, content in ctx.custom_blocks.items():
        if content:
            sections.append({
                "name": key,
                "content": content,
                "color": "#8b949e",
                "chars": len(content),
            })
    return sections


# ---------------------------------------------------------------------------
# Pipeline event capture
# ---------------------------------------------------------------------------

@dataclass
class PipelineEvent:
    stage: str
    label: str
    data: dict = field(default_factory=dict)

STAGE_MAP: dict[EventType, PipelineEvent] = {
    EventType.MESSAGE_RECEIVED: PipelineEvent("receive", "Message Received"),
    EventType.POLICY_TRIGGERED: PipelineEvent("policy_in", "Policy Check"),
    EventType.CONTEXT_COMPOSED: PipelineEvent("compose", "Context Composition"),
    EventType.MODEL_REQUESTED: PipelineEvent("adapter", "Adapter Call"),
    EventType.MODEL_RESPONDED: PipelineEvent("adapter", "Adapter Response"),
    EventType.EVALUATION_COMPLETED: PipelineEvent("evaluate", "Evaluation"),
    EventType.EXPERIENCE_RECORDED: PipelineEvent("memory", "Memory Storage"),
    EventType.LIFE_EVENT_RECORDED: PipelineEvent("timeline", "Timeline Update"),
    EventType.RESPONSE_GENERATED: PipelineEvent("response", "Response"),
}


def _capture_pipeline_events(
    runtime: IdentityRuntime,
    request: InteractionRequest,
    on_event: Optional[Callable[[PipelineEvent], None]] = None,
) -> tuple[List[PipelineEvent], Optional[str], Optional[ComposedContext], dict]:
    """Run process() and capture all EventBus events as PipelineEvents."""
    events_queue: queue.Queue[Event] = queue.Queue()
    done_event = threading.Event()
    captured: List[PipelineEvent] = []
    output: Optional[str] = None
    context_used: Optional[ComposedContext] = None
    response_metadata: dict = {}

    def handler(event: Event) -> None:
        events_queue.put(event)

    runtime.event_bus.subscribe_all(handler)

    def run() -> None:
        nonlocal output, context_used, response_metadata
        try:
            resp = runtime.process(request)
            output = resp.output
            context_used = resp.context_used
            response_metadata = dict(resp.metadata or {})
        except Exception as e:
            output = f"[Runtime Error] {e}"
        finally:
            done_event.set()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()

    while thread.is_alive() or not events_queue.empty():
        try:
            event = events_queue.get(timeout=0.05)
        except queue.Empty:
            continue

        pe = STAGE_MAP.get(event.event_type)
        if pe is None:
            continue

        payload = event.payload if hasattr(event, 'payload') else {}

        if event.event_type == EventType.POLICY_TRIGGERED:
            scope = payload.get("scope", "")
            if scope == "input":
                pe = PipelineEvent("policy_in", "Policy Check (Input)")
            elif scope == "output":
                pe = PipelineEvent("policy_out", "Policy Check (Output)")
            else:
                continue

        data: dict = {}
        if event.event_type == EventType.MESSAGE_RECEIVED:
            data["content"] = str(payload.get("content", ""))[:80]
        elif event.event_type == EventType.POLICY_TRIGGERED:
            data["allowed"] = payload.get("allowed", True)
            data["policies"] = payload.get("policies_applied", [])
        elif event.event_type == EventType.CONTEXT_COMPOSED:
            data["token_estimate"] = payload.get("token_estimate", 0)
            data["section_count"] = 0
            data["sections"] = []
        elif event.event_type == EventType.MODEL_REQUESTED:
            data["model"] = payload.get("model", "unknown")
        elif event.event_type == EventType.MODEL_RESPONDED:
            data["response_length"] = payload.get("response_length", 0)
            data["latency_ms"] = payload.get("latency_ms", 0)
        elif event.event_type == EventType.EVALUATION_COMPLETED:
            data["score"] = payload.get("overall_score", 0.0)
            data["passed"] = payload.get("passed", True)
            data["criteria_count"] = payload.get("criteria_count", 0)
        elif event.event_type == EventType.EXPERIENCE_RECORDED:
            data["memory_type"] = payload.get("memory_type", "episodic")
            data["memory_id"] = str(payload.get("memory_id", ""))[:8]
            data["memory_content"] = str(payload.get("content", ""))[:60]
        elif event.event_type == EventType.LIFE_EVENT_RECORDED:
            data["description"] = str(payload.get("description", ""))[:60]
            data["title"] = str(payload.get("title", ""))[:40]

        captured_event = PipelineEvent(stage=pe.stage, label=pe.label, data=data)
        captured.append(captured_event)
        if on_event:
            on_event(captured_event)

    runtime.event_bus.unsubscribe_all(handler)

    # Post-process: enrich compose event with section info from context_used
    if context_used:
        sections = _context_sections(context_used)
        for pe in captured:
            if pe.stage == "compose":
                pe.data["sections"] = [s["name"] for s in sections]
                pe.data["section_count"] = len(sections)
                break

    # Enrich memory events with content from context_used
    if context_used:
        mem_block = context_used.memory_block
        if mem_block:
            for pe in captured:
                if pe.stage == "memory":
                    pe.data["memory_retrieved"] = True
                    mem_lines = [line.strip() for line in mem_block.split('\n') if line.strip()]
                    pe.data["memory_block_snippets"] = [line[:80] for line in mem_lines[:5]]

    # Add synthetic stages with runtime data
    identity_id = request.identity_id
    rel_count = len(runtime.identity_graph.get_relationships(identity_id)) if identity_id else 0
    relationship_event = PipelineEvent("relationship", "Relationship Update", {
        "edge_count": rel_count,
    })
    captured.append(relationship_event)
    if on_event:
        on_event(relationship_event)

    persist_ns = []
    if hasattr(runtime, '_storage') and runtime._storage:
        try:
            persist_ns = runtime._storage.list_namespaces(identity_id) if identity_id else []
        except Exception:
            pass
    persist_event = PipelineEvent("persist", "Persistence", {
        "namespaces": len(persist_ns),
    })
    captured.append(persist_event)
    if on_event:
        on_event(persist_event)

    # Ensure we have a response event
    if output is not None:
        response_event = PipelineEvent("response", "Response", {
            "output": output[:120],
            "output_length": len(output),
        })
        captured.append(response_event)
        if on_event:
            on_event(response_event)

    return captured, output, context_used, response_metadata


# ---------------------------------------------------------------------------
# Persistent runtime manager
# ---------------------------------------------------------------------------

class RuntimeManager:
    """Manages a single IdentityRuntime instance per identity."""

    def __init__(self):
        self._paths = get_paths()
        initialize_adoption(self._paths)
        self._runtime: Optional[IdentityRuntime] = None
        self._sessions: Dict[str, str] = {}
        self._storage = JSONFileBackend(root_dir=str(self._paths.identity_store))

        # Read adapter config from env vars (same convention as runtime/main.py)
        self._adapter_name: Optional[str] = os.environ.get("IDENTITY_ADAPTER") or None
        self._adapter_model: Optional[str] = None
        self._adapter_kwargs: dict = {}
        adapter_config_str = os.environ.get("IDENTITY_ADAPTER_CONFIG", "{}")
        if adapter_config_str:
            try:
                import json as _json
                self._adapter_kwargs = _json.loads(adapter_config_str)
                self._adapter_model = self._adapter_kwargs.get("model")
            except Exception:
                self._adapter_kwargs = {}

    def get_or_create_runtime(self) -> IdentityRuntime:
        if self._runtime is not None:
            return self._runtime
        adapter = None
        try:
            config = load_config(self._paths)
            if config.provider:
                adapter = build_configured_adapter(self._paths)
        except AdoptionConfigError:
            adapter = None
        rt = IdentityRuntime(storage=self._storage, adapter=adapter)
        register_default_criteria(rt.evaluation_engine)
        rt.load_persisted()
        self._runtime = rt
        return rt

    def restart(self) -> None:
        if self._runtime is not None:
            self._runtime.shutdown()
        self._runtime = None
        self._sessions.clear()
        self.get_or_create_runtime()

    def get_runtime(self) -> IdentityRuntime:
        rt = self.get_or_create_runtime()
        return rt

    def list_identities(self) -> List[str]:
        rt = self.get_or_create_runtime()
        specs = rt.identity_store.list_all()
        ids = [s.id for s in specs]
        stored = self._storage.list_identities()
        for sid in stored:
            if sid not in ids:
                ids.append(sid)
        return sorted(set(ids))

    def create_identity(
        self,
        name: str,
        identity_id: Optional[str] = None,
        persona: Optional[str] = None,
        system_prompt: Optional[str] = None,
        adapter: Optional[str] = None,
        model: Optional[str] = None,
    ) -> IdentitySpec:
        rt = self.get_or_create_runtime()
        spec = create_identity(
            name=name,
            identity_id=identity_id,
            persona=persona,
            system_prompt=system_prompt,
        )
        rt.register(spec)
        if adapter:
            self._adapter_name = adapter
            self._adapter_model = model
            self._maybe_configure_adapter(rt)
        return spec

    def _maybe_configure_adapter(self, rt: IdentityRuntime) -> None:
        if self._adapter_name and rt.adapter is None:
            try:
                from adapters import get_adapter
                kwargs = dict(self._adapter_kwargs)
                model = self._adapter_model or kwargs.get("model")
                if model:
                    kwargs["model"] = model
                rt.adapter = get_adapter(self._adapter_name, **kwargs)
            except Exception:
                import sys
                print("[playground] adapter configuration failed", file=sys.stderr)

    def create_onboarding_identity(self, name: str, purpose: str = "") -> IdentitySpec:
        clean_name = str(name or "").strip()
        if not clean_name or len(clean_name) > 80 or any(ch in clean_name for ch in "\r\n\x00"):
            raise ValueError("Name must be 1 to 80 characters on one line")
        if any(spec.name.casefold() == clean_name.casefold() for spec in self.get_runtime().identity_store.list_all()):
            raise ValueError("An identity with that name already exists")
        clean_purpose = str(purpose or "").strip()
        if len(clean_purpose) > 1000:
            raise ValueError("Purpose must be 1000 characters or fewer")
        spec = self.create_identity(name=clean_name, persona=clean_purpose or "A helpful AI identity")
        config = load_config(self._paths)
        config.identity_id = spec.id
        config.onboarding.update({"stage": "provider", "ready": False})
        save_config(config, self._paths)
        return spec

    def configure_onboarding_provider(
        self,
        provider: str,
        *,
        model: str = "",
        base_url: str = "",
        api_key: str = "",
    ) -> dict:
        config = load_config(self._paths)
        if not config.identity_id or not (
            self.get_runtime().identity_store.get(config.identity_id)
            or self.get_runtime().load(config.identity_id)
        ):
            raise ValueError("Create an identity before configuring a provider")
        updated = configure_provider(
            provider,
            model=model or None,
            base_url=base_url or None,
            api_key=api_key or None,
            paths=self._paths,
        )
        self.get_runtime().adapter = build_configured_adapter(self._paths)
        public_provider = dict(public_status(self._paths).get("provider") or {})
        return {
            "configured": True,
            "provider": public_provider,
            "next_stage": updated.onboarding.get("stage", "verification"),
        }

    @staticmethod
    def _public_provenance(value: dict) -> dict:
        return {
            "generation_mode": str(value.get("generation_mode", "")),
            "provider": str(value.get("provider", "")),
            "model": str(value.get("model", "")),
            "latency_ms": value.get("latency_ms"),
        }

    def run_onboarding_inference(self, prompt: str, *, record_ready: bool) -> dict:
        config = load_config(self._paths)
        identity_id = config.identity_id or ""
        clean_prompt = str(prompt or "").strip()
        if not identity_id:
            return {"ready": False, "error": "identity_required", "message": "Create an identity first."}
        if not clean_prompt or len(clean_prompt) > 20_000:
            return {
                "ready": False,
                "error": "invalid_prompt",
                "message": "Enter a message between 1 and 20,000 characters.",
            }
        rt = self.get_runtime()
        identity = rt.identity_store.get(identity_id) or rt.load(identity_id)
        if identity is None:
            return {
                "ready": False,
                "error": "identity_missing",
                "message": "The configured identity could not be loaded.",
            }
        try:
            rt.adapter = build_configured_adapter(self._paths)
        except AdoptionConfigError:
            return {
                "ready": False,
                "error": "provider_not_configured",
                "message": "Choose a provider and check its credential before verification.",
            }
        session_id = self._sessions.get(identity_id)
        if not session_id:
            session_id = rt.start_session(identity_id)
            self._sessions[identity_id] = session_id
        # Connectivity verification must make one genuine provider request, not
        # trigger unrelated capability-evolution work on the interactive path.
        prometheus = rt.prometheus
        if record_ready:
            rt.prometheus = None
        try:
            response = rt.process(InteractionRequest(
                identity_id=identity_id,
                user_input=clean_prompt,
                session_id=session_id,
            ))
        finally:
            rt.prometheus = prometheus
        provenance = dict(response.metadata.get("generation_provenance") or {})
        public_provenance = self._public_provenance(provenance)
        actual = (
            public_provenance["generation_mode"] == "identity_model_generation"
            and bool(public_provenance["provider"])
            and bool(public_provenance["model"])
            and bool(str(response.output or "").strip())
            and not str(response.output).startswith((
                "[No adapter configured", "[Generation unavailable", "[Runtime Error]",
            ))
        )
        if not actual:
            return {
                "ready": False,
                "error": "inference_not_verified",
                "message": (
                    "The provider did not produce a real model response. Check the API key, "
                    "model name, network connection, local model availability, and provider limits."
                ),
                "provenance": public_provenance,
            }
        if record_ready:
            digest = hashlib.sha256(str(response.output).encode("utf-8")).hexdigest()
            record_verification(
                identity_id=identity_id,
                provenance=provenance,
                response_digest=digest,
                paths=self._paths,
            )
        return {
            "ready": True,
            "output": response.output,
            "provenance": public_provenance,
            "identity_id": identity_id,
            "session": self.session_info(identity_id),
        }

    def get_identity_data(self, identity_id: str) -> dict:
        rt = self.get_or_create_runtime()
        spec = rt.identity_store.get(identity_id)

        identities = rt.identity_store.list_all()
        for s in identities:
            if s.id not in [s2.id for s2 in rt.identity_store.list_all()]:
                pass

        identity_dict = spec.to_dict() if spec else {}
        if not identity_dict and self._storage:
            data = self._storage.load(identity_id, "latest_snapshot")
            if data:
                identity_dict = data.get("modules", {}).get("identity", data)

        # Memories
        mems = rt.memory_store.by_identity(identity_id) if identity_id else []
        mem_dicts = [m.to_dict() for m in mems]

        # Timeline
        tl = rt.timeline_registry.get(identity_id)
        tl_events = []
        if tl:
            tl_events = [
                {
                    "id": e.id,
                    "event_type": e.event_type.value,
                    "title": e.title,
                    "description": e.description,
                    "significance": e.significance,
                    "occurred_at": e.occurred_at.isoformat(),
                }
                for e in tl.events()
            ]

        # Goals
        goals = [
            goal for goal in rt.goal_engine.all()
            if goal.metadata.get("identity_id") in (None, identity_id)
        ] if identity_id else []
        goal_dicts = [
            {
                "id": g.id,
                "title": g.title,
                "description": g.description,
                "status": g.status.value,
                "priority": g.priority.name,
                "progress": g.progress,
                "created_at": g.created_at.isoformat(),
            }
            for g in goals
        ]

        rt.intention_engine.check_expiry()
        intentions = [
            intention.to_dict()
            for intention in rt.intention_engine.all()
            if intention.metadata.get("identity_id") in (None, identity_id)
        ] if identity_id else []

        # Relationships
        edges = rt.identity_graph.get_relationships(identity_id) if identity_id else []
        edge_dicts = [
            {
                "id": e.id,
                "source_id": e.source_id,
                "target_id": e.target_id,
                "edge_type": e.edge_type.value,
                "trust_level": e.trust_level.value,
                "strength": e.strength,
                "bidirectional": e.bidirectional,
                "interaction_count": e.interaction_count,
                "established_at": e.established_at.isoformat(),
            }
            for e in edges
        ]

        # Adapter info
        adapter_info = None
        if rt.adapter:
            adapter_info = {
                "type": type(rt.adapter).__name__,
                "model": getattr(rt.adapter, "model", "unknown"),
                "streaming": getattr(rt.adapter, "streaming", False),
            }

        # Current context (structured)
        current_context = ""
        context_sections = []
        if spec:
            try:
                ctx = rt.context_composer.compose(
                    identity=spec,
                    memory_store=rt.memory_store,
                    skill_registry=rt.skill_registry,
                    goal_engine=rt.goal_engine,
                    identity_graph=rt.identity_graph,
                    motivation_engine=rt.motivation_engine,
                    timeline_registry=rt.timeline_registry,
                    query="",
                    top_k_memories=5,
                )
                current_context = ctx.render()
                context_sections = _context_sections(ctx)
            except Exception:
                current_context = "(error composing context)"

        # Eval history
        eval_history = rt.evaluation_engine.history(identity_id) if identity_id else []
        last_eval = None
        if eval_history:
            last = eval_history[-1]
            last_eval = {
                "score": last.overall_score,
                "passed": last.passed,
                "details": last.summarize() if hasattr(last, 'summarize') else "",
                "criteria": [
                    {
                        "name": r.criterion_name,
                        "score": r.score,
                        "outcome": r.outcome.value,
                    }
                    for r in last.records
                ],
            }

        # Persistence files
        persist_files = []
        if self._storage:
            ns = self._storage.list_namespaces(identity_id) if identity_id else []
            persist_files = sorted(ns)

        # Evolution metrics + identity mutation data
        evolution = {
            "interaction_count": (
                len([event for event in tl_events if event.get("event_type") in ("milestone", "creation")])
                if tl_events else 0
            ),
            "memory_count": len(mem_dicts),
            "relationship_count": len(edge_dicts),
            "goal_count": len(goal_dicts),
            "timeline_count": len(tl_events) if tl_events else 0,
        }
        if identity_dict and identity_dict.get("created_at"):
            try:
                created = _dt.fromisoformat(identity_dict["created_at"])
                now = _dt.now(_dt.timezone.utc).replace(tzinfo=None)
                delta = now - created
                evolution["age_seconds"] = int(delta.total_seconds())
                evolution["created_at"] = created.isoformat()
            except Exception:
                pass

        # Identity evolution data from spec
        identity_evolution = {
            "preferences": identity_dict.get("preferences", {}),
            "beliefs": identity_dict.get("beliefs", {}),
            "likes": identity_dict.get("likes", []),
            "dislikes": identity_dict.get("dislikes", []),
            "habits": identity_dict.get("habits", []),
            "communication_tendencies": identity_dict.get("communication_tendencies", {}),
            "mutation_history": identity_dict.get("mutation_history", []),
            "traits": identity_dict.get("traits", []),
        }

        from runtime.debugger import load_debug_record
        from runtime.replay import build_identity_replay

        debug_record = load_debug_record(self._storage, identity_id)
        try:
            replay = build_identity_replay(self._storage, identity_id)
        except ValueError:
            replay = None

        return {
            "identity": identity_dict,
            "memories": mem_dicts,
            "timeline": tl_events,
            "goals": goal_dicts,
            "intentions": intentions,
            "relationships": edge_dicts,
            "adapter": adapter_info,
            "evaluation": last_eval,
            "persistence": persist_files,
            "context": current_context,
            "context_sections": context_sections,
            "evolution": evolution,
            "identity_evolution": identity_evolution,
            "debug": debug_record,
            "replay": replay,
            "session": self.session_info(identity_id),
        }

    def process_message(
        self,
        identity_id: str,
        user_input: str,
        on_event: Optional[Callable[[PipelineEvent], None]] = None,
    ) -> dict:
        rt = self.get_or_create_runtime()
        try:
            adoption_config = load_config(self._paths)
            if adoption_config.identity_id == identity_id and adoption_config.provider:
                rt.adapter = build_configured_adapter(self._paths)
        except AdoptionConfigError:
            pass
        self._maybe_configure_adapter(rt)

        # Ensure identity is loaded
        spec = rt.identity_store.get(identity_id)
        if not spec:
            loaded = rt.load(identity_id)
            if not loaded:
                return {"error": f"Identity '{identity_id}' not found."}

        # Ensure session
        session_id = self._sessions.get(identity_id)
        if not session_id:
            session_id = rt.start_session(identity_id)
            self._sessions[identity_id] = session_id

        # Add a default goal if none exist
        if len(rt.goal_engine) == 0 and identity_id:
            rt.goal_engine.add(Goal(
                title="Learn and grow",
                priority=GoalPriority.MEDIUM,
                scope=GoalScope.PERSISTENT,
                metadata={"identity_id": identity_id},
            ))
            rt._persist_goals(identity_id)

        request = InteractionRequest(
            identity_id=identity_id,
            user_input=user_input,
            session_id=session_id,
        )
        events, output, context_used, response_metadata = _capture_pipeline_events(
            rt, request, on_event=on_event,
        )

        context_text = ""
        context_sections = []
        if context_used:
            context_text = context_used.render()
            context_sections = _context_sections(context_used)

        return {
            "output": output or "",
            "events": [{"stage": e.stage, "label": e.label, "data": e.data} for e in events],
            "context": context_text or "",
            "context_sections": context_sections,
            "session": self.session_info(identity_id),
            "generation_provenance": self._public_provenance(
                dict(response_metadata.get("generation_provenance") or {})
            ),
        }

    def session_info(self, identity_id: str) -> dict:
        session_id = self._sessions.get(identity_id)
        mode = SessionMode.NORMAL
        if session_id and self._runtime:
            mode = self._runtime.get_session_mode(session_id)
        return {"id": session_id, "mode": mode.value}

    def set_session_mode(self, identity_id: str, mode: str) -> dict:
        rt = self.get_or_create_runtime()
        if not (rt.identity_store.get(identity_id) or rt.load(identity_id)):
            raise ValueError(f"Identity '{identity_id}' not found")
        try:
            session_mode = SessionMode(mode.lower())
        except ValueError as exc:
            allowed = ", ".join(item.value for item in SessionMode)
            raise ValueError(f"Unknown session mode. Allowed: {allowed}") from exc
        old_session = self._sessions.pop(identity_id, None)
        if old_session:
            rt.end_session(old_session)
        session_id = rt.start_session(identity_id, mode=session_mode)
        self._sessions[identity_id] = session_id
        return {"id": session_id, "mode": session_mode.value}

    def add_goal(self, identity_id: str, title: str, priority: str = "medium") -> dict:
        rt = self.get_or_create_runtime()
        if not (rt.identity_store.get(identity_id) or rt.load(identity_id)):
            raise ValueError(f"Identity '{identity_id}' not found")
        try:
            goal_priority = GoalPriority[priority.upper()]
        except KeyError as exc:
            raise ValueError("priority must be low, medium, high, or critical") from exc
        goal = Goal(
            title=title,
            priority=goal_priority,
            scope=GoalScope.PERSISTENT,
            metadata={"identity_id": identity_id},
        )
        rt.goal_engine.add(goal)
        rt._persist_goals(identity_id)
        return goal.to_dict()

    def complete_goal(self, identity_id: str, goal_id: str) -> dict:
        rt = self.get_or_create_runtime()
        goal = rt.goal_engine.get(goal_id)
        if not goal or goal.metadata.get("identity_id") not in (None, identity_id):
            raise ValueError("Goal not found")
        goal.mark_completed("Completed from Identity Chat")
        rt._persist_goals(identity_id)
        return goal.to_dict()

    def add_intention(
        self, identity_id: str, description: str, priority: str = "medium", hours: int = 24,
    ) -> dict:
        rt = self.get_or_create_runtime()
        if not (rt.identity_store.get(identity_id) or rt.load(identity_id)):
            raise ValueError(f"Identity '{identity_id}' not found")
        priority_map = {
            "low": IntentionPriority.LOW,
            "medium": IntentionPriority.MEDIUM,
            "high": IntentionPriority.HIGH,
        }
        if priority.lower() not in priority_map or not 1 <= hours <= 8760:
            raise ValueError("priority must be low, medium, or high and hours must be 1..8760")
        intention = Intention(
            description=description,
            priority=priority_map[priority.lower()],
            expires_at=_dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None) + _dt.timedelta(hours=hours),
            metadata={"identity_id": identity_id},
        )
        rt.intention_engine.add(intention)
        rt._persist_intentions(identity_id)
        return intention.to_dict()

    def complete_intention(self, identity_id: str, intention_id: str) -> dict:
        rt = self.get_or_create_runtime()
        intention = rt.intention_engine.get(intention_id)
        if not intention or intention.metadata.get("identity_id") not in (None, identity_id):
            raise ValueError("Intention not found")
        intention.complete("Completed from Identity Chat")
        rt._persist_intentions(identity_id)
        return intention.to_dict()

    def constitution(self, identity_id: str) -> dict:
        from identityos.identity import IdentityObject

        rt = self.get_or_create_runtime()
        if not (rt.identity_store.get(identity_id) or rt.load(identity_id)):
            raise ValueError(f"Identity '{identity_id}' not found")
        return IdentityObject(rt, identity_id).constitution()

    def export_identity(self, identity_id: str) -> dict:
        from identityos.identity import IdentityObject

        rt = self.get_or_create_runtime()
        if not (rt.identity_store.get(identity_id) or rt.load(identity_id)):
            raise ValueError(f"Identity '{identity_id}' not found")
        return IdentityObject(rt, identity_id).export()

    def restart_identity(self, identity_id: str) -> dict:
        rt = self.get_or_create_runtime()
        # Capture pre-restart state
        mems_before = len(rt.memory_store.by_identity(identity_id))
        tl_before = rt.timeline_registry.get(identity_id)
        tl_count = len(tl_before.events()) if tl_before else 0
        goals_before = len(rt.goal_engine.active())
        rels_before = len(rt.identity_graph.get_relationships(identity_id))

        # Restart
        self.restart()
        new_rt = self.get_or_create_runtime()
        new_rt.load(identity_id)
        new_rt._load_persisted_memories(identity_id)

        # Verify
        mems_after = len(new_rt.memory_store.by_identity(identity_id))
        tl_after = new_rt.timeline_registry.get(identity_id)
        tl_count_after = len(tl_after.events()) if tl_after else 0
        goals_after = len(new_rt.goal_engine.active())
        rels_after = len(new_rt.identity_graph.get_relationships(identity_id))

        return {
            "memories_restored": mems_after >= mems_before,
            "memories_count": mems_after,
            "timeline_restored": tl_count_after >= tl_count,
            "timeline_count": tl_count_after,
            "goals_restored": goals_after >= goals_before,
            "goals_count": goals_after,
            "relationships_restored": rels_after >= rels_before,
            "relationships_count": rels_after,
        }


manager = RuntimeManager()

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="IdentityOS Playground")
_CSRF_COOKIE = "identityos_csrf"
_CSRF_HEADER = "x-identityos-csrf"
_CSRF_TOKEN = secrets.token_urlsafe(32)

_jinja_env = Environment(
    loader=FileSystemLoader(HERE + "/templates"),
    autoescape=select_autoescape(["html", "xml"]),
)

app.mount("/playground/static", StaticFiles(directory=HERE + "/static"), name="playground_static")


def _loopback_name(value: str) -> bool:
    return value.lower().rstrip(".") in {"localhost", "127.0.0.1", "::1", "test", "testserver"}


@app.middleware("http")
async def secure_local_browser(request: Request, call_next):
    """Reject remote-host and cross-origin browser requests before route handling."""
    host = request.url.hostname or ""
    if not _loopback_name(host):
        return _safe_error(
            "invalid_host",
            "IdentityOS browser access is limited to this computer.",
            status_code=400,
        )

    origin = request.headers.get("origin", "")
    if origin:
        from urllib.parse import urlparse

        origin_url = urlparse(origin)
        if origin_url.scheme not in {"http", "https"} or not _loopback_name(origin_url.hostname or ""):
            return _safe_error(
                "cross_origin_denied",
                "Cross-origin browser requests are not allowed.",
                status_code=403,
            )

    if request.method not in {"GET", "HEAD", "OPTIONS"} and request.url.path.startswith("/onboarding/api/"):
        cookie_token = request.cookies.get(_CSRF_COOKIE, "")
        header_token = request.headers.get(_CSRF_HEADER, "")
        if not (
            cookie_token
            and header_token
            and secrets.compare_digest(cookie_token, _CSRF_TOKEN)
            and secrets.compare_digest(header_token, _CSRF_TOKEN)
        ):
            return _safe_error(
                "csrf_failed",
                "Refresh onboarding and try again.",
                status_code=403,
            )

    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self' https://unpkg.com; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    )
    if request.url.path.startswith("/onboarding"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/", include_in_schema=False)
async def root_page():
    return RedirectResponse("/onboarding", status_code=307)


@app.get("/onboarding", response_class=HTMLResponse)
async def onboarding_page(request: Request):
    template = _jinja_env.get_template("onboarding.html")
    response = HTMLResponse(template.render(request=request, csrf_token=_CSRF_TOKEN))
    response.set_cookie(
        _CSRF_COOKIE,
        _CSRF_TOKEN,
        httponly=True,
        samesite="strict",
        secure=request.url.scheme == "https",
        path="/",
    )
    return response


@app.get("/onboarding/api/state")
async def onboarding_state():
    state = public_status(manager._paths)
    identity_id = state.get("identity_id")
    identity = manager.get_runtime().identity_store.get(identity_id) if identity_id else None
    state["identity"] = (
        {"id": identity.id, "name": identity.name, "purpose": identity.persona}
        if identity else None
    )
    return JSONResponse(state)


@app.get("/onboarding/api/providers")
async def onboarding_providers():
    return JSONResponse({"providers": list_providers()})


@app.get("/onboarding/api/history")
async def onboarding_history():
    config = load_config(manager._paths)
    if not config.identity_id:
        return JSONResponse({"messages": []})
    runtime = manager.get_runtime()
    messages: list[dict[str, str]] = []
    for memory in runtime.memory_store.by_identity(config.identity_id)[-50:]:
        if "interaction" not in getattr(memory, "tags", []):
            continue
        content = str(getattr(memory, "content", ""))
        if content.startswith("User: ") and "\nAssistant: " in content:
            user_text, assistant_text = content[6:].split("\nAssistant: ", 1)
            messages.extend((
                {"role": "user", "content": user_text},
                {"role": "assistant", "content": assistant_text},
            ))
    return JSONResponse({"messages": messages})


@app.post("/onboarding/api/identity")
async def onboarding_create_identity(body: dict):
    try:
        spec = manager.create_onboarding_identity(
            str(body.get("name", "")),
            str(body.get("purpose", "")),
        )
    except ValueError as exc:
        return _safe_error("invalid_identity", str(exc), status_code=400)
    return JSONResponse({
        "created": True,
        "identity": {"id": spec.id, "name": spec.name, "purpose": spec.persona},
        "next_stage": "provider",
    })


@app.post("/onboarding/api/provider")
async def onboarding_configure_provider(body: dict):
    try:
        result = manager.configure_onboarding_provider(
            str(body.get("provider", "")),
            model=str(body.get("model", "")),
            base_url=str(body.get("base_url", "")),
            api_key=str(body.get("api_key", "")),
        )
    except (AdoptionConfigError, ValueError):
        return _safe_error(
            "provider_configuration_failed",
            "The provider could not be configured. Check the credential, model, and provider URL.",
            status_code=400,
        )
    return JSONResponse(result)


@app.post("/onboarding/api/verify")
async def onboarding_verify(body: dict):
    prompt = str(body.get("prompt") or "Reply with one short sentence confirming that inference is working.")
    try:
        result = manager.run_onboarding_inference(prompt, record_ready=True)
    except Exception:
        result = {
            "ready": False,
            "error": "inference_failed",
            "message": "Inference failed. Check the provider, credential, model, and network connection.",
        }
    return JSONResponse(result, status_code=200 if result.get("ready") else 422)


@app.post("/onboarding/api/chat")
async def onboarding_chat(body: dict):
    status = public_status(manager._paths)
    if not status.get("onboarding", {}).get("ready"):
        return _safe_error(
            "verification_required",
            "Verify the provider before starting the conversation.",
            status_code=409,
        )
    try:
        result = manager.run_onboarding_inference(str(body.get("message", "")), record_ready=False)
    except Exception:
        result = {
            "ready": False,
            "error": "inference_failed",
            "message": "The provider could not generate a response. Check its status and try again.",
        }
    return JSONResponse(result, status_code=200 if result.get("ready") else 422)


@app.get("/playground", response_class=HTMLResponse)
async def playground_page(request: Request):
    template = _jinja_env.get_template("playground.html")
    html = template.render(request=request)
    return HTMLResponse(html)


@app.get("/playground/api/identities")
async def api_list_identities():
    ids = manager.list_identities()
    return JSONResponse(ids)


@app.post("/playground/api/identities")
async def api_create_identity(body: dict):
    identity_id = body.get("identity_id")
    name = body.get("name", "New Identity")
    persona = body.get("persona")
    system_prompt = body.get("system_prompt")
    adapter = body.get("adapter")
    model = body.get("model")
    spec = manager.create_identity(
        name=name,
        identity_id=identity_id,
        persona=persona,
        system_prompt=system_prompt,
        adapter=adapter,
        model=model,
    )
    return JSONResponse({"id": spec.id, "name": spec.name})


@app.get("/playground/api/identity/{identity_id}")
async def api_get_identity(identity_id: str):
    data = manager.get_identity_data(identity_id)
    return JSONResponse(data)


@app.get("/playground/api/debug/{identity_id}")
async def api_get_debug_record(identity_id: str, interaction: Optional[str] = None):
    from runtime.debugger import load_debug_record

    record = load_debug_record(manager._storage, identity_id, interaction)
    if record is None:
        return JSONResponse({"error": "debug record not found"}, status_code=404)
    return JSONResponse(record)


@app.get("/playground/api/replay/{identity_id}")
async def api_get_replay(identity_id: str):
    from runtime.replay import build_identity_replay

    try:
        replay = build_identity_replay(manager._storage, identity_id)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    return JSONResponse(replay)


@app.post("/playground/api/chat")
async def api_chat(body: dict):
    identity_id = body.get("identity_id", "")
    user_input = body.get("user_input", "")
    if not identity_id:
        return JSONResponse({"error": "identity_id is required"}, status_code=400)
    if not user_input:
        return JSONResponse({"error": "user_input is required"}, status_code=400)
    result = manager.process_message(identity_id, user_input)
    return JSONResponse(result)


@app.post("/playground/api/chat/stream")
async def api_chat_stream(body: dict):
    """Stream live pipeline events and chunked output as newline-delimited JSON."""
    identity_id = body.get("identity_id", "")
    user_input = body.get("user_input", "")
    if not identity_id or not user_input:
        return JSONResponse(
            {"error": "identity_id and user_input are required"}, status_code=400,
        )

    async def generate():
        stream_queue: queue.Queue[dict] = queue.Queue()
        done = threading.Event()

        def emit(event: PipelineEvent) -> None:
            stream_queue.put({
                "type": "event",
                "event": {"stage": event.stage, "label": event.label, "data": event.data},
            })

        def run() -> None:
            try:
                result = manager.process_message(identity_id, user_input, on_event=emit)
                if result.get("error"):
                    stream_queue.put({"type": "error", "error": result["error"]})
                    return
                stream_queue.put({
                    "type": "meta",
                    "context": result.get("context", ""),
                    "context_sections": result.get("context_sections", []),
                    "session": result.get("session", {}),
                })
                output = result.get("output", "")
                for offset in range(0, len(output), 48):
                    stream_queue.put({"type": "chunk", "text": output[offset:offset + 48]})
                stream_queue.put({"type": "done"})
            except Exception as exc:
                stream_queue.put({"type": "error", "error": str(exc)})
            finally:
                done.set()

        threading.Thread(target=run, daemon=True).start()
        while not done.is_set() or not stream_queue.empty():
            try:
                item = stream_queue.get_nowait()
            except queue.Empty:
                await asyncio.sleep(0.02)
                continue
            yield json.dumps(item, default=str) + "\n"

    return StreamingResponse(generate(), media_type="application/x-ndjson")


@app.post("/playground/api/session")
async def api_set_session(body: dict):
    try:
        session = manager.set_session_mode(body.get("identity_id", ""), body.get("mode", "normal"))
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse(session)


@app.post("/playground/api/goals")
async def api_add_goal(body: dict):
    title = str(body.get("title", "")).strip()
    if not title:
        return JSONResponse({"error": "title is required"}, status_code=400)
    try:
        goal = manager.add_goal(body.get("identity_id", ""), title, body.get("priority", "medium"))
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse(goal)


@app.post("/playground/api/goals/{goal_id}/complete")
async def api_complete_goal(goal_id: str, body: dict):
    try:
        goal = manager.complete_goal(body.get("identity_id", ""), goal_id)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    return JSONResponse(goal)


@app.post("/playground/api/intentions")
async def api_add_intention(body: dict):
    description = str(body.get("description", "")).strip()
    if not description:
        return JSONResponse({"error": "description is required"}, status_code=400)
    try:
        intention = manager.add_intention(
            body.get("identity_id", ""),
            description,
            body.get("priority", "medium"),
            int(body.get("hours", 24)),
        )
    except (TypeError, ValueError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse(intention)


@app.post("/playground/api/intentions/{intention_id}/complete")
async def api_complete_intention(intention_id: str, body: dict):
    try:
        intention = manager.complete_intention(body.get("identity_id", ""), intention_id)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    return JSONResponse(intention)


@app.get("/playground/api/constitution/{identity_id}")
async def api_constitution(identity_id: str):
    try:
        return JSONResponse(manager.constitution(identity_id))
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)


@app.get("/playground/api/export/{identity_id}")
async def api_export(identity_id: str):
    try:
        data = manager.export_identity(identity_id)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    safe_name = "".join(char for char in identity_id if char.isalnum() or char in "-_") or "identity"
    return Response(
        json.dumps(data, indent=2, default=str),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}.json"'},
    )


@app.post("/playground/api/configure-adapter")
async def api_configure_adapter(body: dict):
    adapter_type = str(body.get("adapter", ""))
    if not adapter_type:
        return JSONResponse({"error": "adapter type is required"}, status_code=400)
    try:
        config = configure_provider(
            adapter_type,
            model=str(body.get("model") or "") or None,
            base_url=str(body.get("base_url") or "") or None,
            api_key=str(body.get("api_key") or "") or None,
            paths=manager._paths,
        )
        manager.get_runtime().adapter = build_configured_adapter(manager._paths)
    except AdoptionConfigError:
        return _safe_error(
            "provider_configuration_failed",
            "The adapter could not be configured. Check the credential, model, and provider URL.",
            status_code=400,
        )
    provider = dict(public_status(manager._paths).get("provider") or {})
    return JSONResponse({
        "configured": True,
        "adapter": provider.get("name", adapter_type),
        "model": provider.get("model", ""),
        "message": "Configuration saved. Run verification before treating the provider as ready.",
        "ready": bool(config.onboarding.get("ready")),
    })


@app.post("/playground/api/restart")
async def api_restart(body: dict):
    identity_id = body.get("identity_id", "")
    result = manager.restart_identity(identity_id)
    return JSONResponse(result)
