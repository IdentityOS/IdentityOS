"""Identity Runtime — Unified FastAPI Service

Routes all interactions through the IdentityRuntime orchestrator,
which runs the full pipeline: policy → context → LLM → evaluate → store.
"""

import logging
import os
import secrets
import threading
import time
from typing import Any, Dict, List, Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from adapters import build_adapter_from_env
from core.evaluation import register_default_criteria
from core.relationships import EdgeType
from runtime.orchestrator import IdentityRuntime, InteractionRequest, InteractionResponse
from runtime.persistence import JSONFileBackend

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Identity Runtime API",
    description="Portable AI identity layer — own your AI's soul, not just its prompt.",
    version="2.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Try reading API keys from .env file if present
_env_file = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")
if os.path.isfile(_env_file):
    try:
        with open(_env_file) as _f:
            for _line in _f:
                _line = _line.strip()
                if _line and not _line.startswith("#") and "=" in _line:
                    _k, _v = _line.split("=", 1)
                    os.environ.setdefault(_k.strip(), _v.strip().strip("\"'"))
    except Exception:
        pass

# Initialize the runtime orchestrator with persistence and optional adapter
_store_path = os.environ.get("IDENTITY_STORE_PATH", ".identity_store")
storage = JSONFileBackend(root_dir=_store_path)

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
logger.info(
    "IdentityOS runtime starting: project_root=%s store=%s",
    _project_root,
    os.path.abspath(_store_path),
)

try:
    adapter = build_adapter_from_env()
except (ImportError, ValueError) as exc:
    adapter = None
    logger.warning("Failed to configure model adapter: %s", exc)

from adapters import describe_adapter

logger.info("Model adapter chain: %s", describe_adapter(adapter))

runtime = IdentityRuntime(storage=storage, adapter=adapter)
register_default_criteria(runtime.evaluation_engine)
loaded = runtime.load_persisted()
if loaded:
    logger.info(f"Loaded {loaded} persisted identity/ies from .identity_store/")


# --- Request / Response Models ---

class ContextRequest(BaseModel):
    message: str
    identity_id: str
    user_id: str
    session_id: Optional[str] = None

class ContextResponse(BaseModel):
    augmented_context: str
    identity_name: str
    memories_used: int
    session_id: str

class EvaluateRequest(BaseModel):
    message: str
    response: str
    identity_id: str
    user_id: str
    session_id: Optional[str] = None

class EvaluateResponse(BaseModel):
    memories_stored: int
    summary: str
    tags: List[str]

class MemoriesResponse(BaseModel):
    identity_id: str
    user_id: str
    memories: List[dict]
    total: int

class CreateIdentityRequest(BaseModel):
    identity_id: str
    name: str
    identity_class: str = "agent"
    persona: str = ""
    role: str = ""

class ProcessRequest(BaseModel):
    message: str
    identity_id: str
    user_id: str
    session_id: Optional[str] = None

class ProcessResponse(BaseModel):
    output: str
    identity_id: str
    user_id: str
    session_id: str
    policy_passed: bool
    eval_score: Optional[float] = None
    session_mode: str = "normal"
    timings_ms: Dict[str, float] = Field(default_factory=dict)
    generation_provenance: Optional[Dict[str, Any]] = None


class MemoryRequest(BaseModel):
    identity_id: str
    content: str
    user_id: str = ""
    memory_type: str = "semantic"
    tags: List[str] = Field(default_factory=list)


class GoalRequest(BaseModel):
    identity_id: str
    title: str
    description: str = ""
    priority: str = "medium"
    scope: str = "persistent"
    success_criteria: str = ""


class RelationshipRequest(BaseModel):
    identity_id: str
    entity_id: str
    trust_level: float = Field(default=0.5, ge=0.0, le=1.0)
    edge_type: str = "friend"
    context: str = ""


class TimelineRequest(BaseModel):
    identity_id: str
    event_type: str = "milestone"
    title: str
    description: str = ""
    significance: int = Field(default=3, ge=1, le=5)


class IdentityRequest(BaseModel):
    identity_id: str


class _FixedWindowRateLimiter:
    """Small process-local API limiter with deterministic, testable behavior."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._windows: Dict[str, tuple[int, int]] = {}

    def allow(self, key: str, limit: int, now: Optional[float] = None) -> bool:
        if limit <= 0:
            return True
        minute = int((time.time() if now is None else now) // 60)
        with self._lock:
            window, count = self._windows.get(key, (minute, 0))
            if window != minute:
                window, count = minute, 0
            if count >= limit:
                return False
            self._windows[key] = (window, count + 1)
            return True

    def reset(self) -> None:
        with self._lock:
            self._windows.clear()


_rate_limiter = _FixedWindowRateLimiter()


def _configured_api_keys() -> List[str]:
    raw = os.environ.get("IDENTITY_API_KEYS") or os.environ.get("IDENTITY_API_KEY", "")
    return [key.strip() for key in raw.split(",") if key.strip()]


@app.middleware("http")
async def enforce_api_access(request: Request, call_next):
    """Apply optional API-key authentication and a per-client rate limit.

    Authentication remains opt-in for backwards-compatible local development.
    Production deployments enable it with ``IDENTITY_API_KEY`` or a
    comma-separated ``IDENTITY_API_KEYS`` value.
    """
    if request.url.path in {"/health", "/docs", "/redoc", "/openapi.json"}:
        return await call_next(request)

    configured = _configured_api_keys()
    supplied = request.headers.get("x-api-key", "")
    authorization = request.headers.get("authorization", "")
    if not supplied and authorization.lower().startswith("bearer "):
        supplied = authorization[7:].strip()
    if configured and not any(secrets.compare_digest(supplied, key) for key in configured):
        return JSONResponse(
            status_code=401,
            content={"error": "unauthorized", "message": "A valid API key is required."},
        )

    try:
        limit = int(os.environ.get("IDENTITY_RATE_LIMIT_PER_MINUTE", "120"))
    except ValueError:
        limit = 120
    client_host = request.client.host if request.client else "unknown"
    bucket = supplied or client_host
    if not _rate_limiter.allow(bucket, limit):
        return JSONResponse(
            status_code=429,
            headers={"Retry-After": "60"},
            content={
                "error": "rate_limit_exceeded",
                "message": f"Rate limit of {limit} requests per minute exceeded.",
            },
        )
    return await call_next(request)


# --- Endpoints ---

@app.get("/")
async def root():
    return {
        "service": "Identity Runtime",
        "version": "2.0.0",
        "status": "running",
        "tagline": "Every AI deserves its own soul.",
    }


@app.get("/health")
async def health():
    return {"status": "ok"}


def _load_identity_or_404(identity_id: str):
    identity = runtime.load(identity_id)
    if not identity:
        raise HTTPException(status_code=404, detail=f"Identity '{identity_id}' not found")
    return identity


@app.post("/process", response_model=ProcessResponse)
async def process(req: ProcessRequest):
    """
    Full pipeline: resolve identity → policy check → compose context →
    (adapter) → policy check → evaluate → store → respond.

    Intended for SDK / agentic use where the caller wants the runtime
    to manage the entire lifecycle.
    """
    session_id = req.session_id or runtime.start_session(
        req.identity_id,
        user_id=req.user_id,
    )

    # Auto-load identity from disk if not already in memory.
    # The /identity POST endpoint writes to disk but does not register
    # with the runtime, so we must load on first use.
    if not runtime.identity_store.get(req.identity_id):
        runtime.load(req.identity_id)

    request = InteractionRequest(
        identity_id=req.identity_id,
        user_id=req.user_id,
        user_input=req.message,
        session_id=session_id,
    )

    result: InteractionResponse = runtime.process(request)

    if not result.policy_passed and "not found" in result.output.lower():
        raise HTTPException(status_code=404, detail=result.output)

    return ProcessResponse(
        output=result.output,
        identity_id=result.identity_id,
        user_id=result.user_id,
        session_id=session_id,
        policy_passed=result.policy_passed,
        eval_score=result.eval_score,
        session_mode=runtime.get_session_mode(session_id).value,
        timings_ms=result.metadata.get("timings_ms", {}),
        generation_provenance=result.metadata.get("generation_provenance"),
    )


@app.post("/chat", response_model=ProcessResponse)
async def chat(req: ProcessRequest):
    """Public chat alias for the full IdentityOS processing pipeline."""
    return await process(req)


@app.post("/memory")
async def create_memory(req: MemoryRequest):
    """Persist a memory with explicit identity and user provenance."""
    _load_identity_or_404(req.identity_id)
    from core.memory import MemoryFragment, MemoryType

    try:
        memory_type = MemoryType(req.memory_type.lower())
    except ValueError as exc:
        allowed = ", ".join(item.value for item in MemoryType)
        raise HTTPException(
            status_code=400,
            detail=f"Unknown memory_type '{req.memory_type}'. Allowed: {allowed}",
        ) from exc
    fragment = MemoryFragment(
        identity_id=req.identity_id,
        user_id=runtime._resolved_user_id(req.identity_id, req.user_id),
        content=req.content,
        memory_type=memory_type,
        tags=req.tags,
    )
    runtime.memory_store.add(fragment)
    runtime._persist_memory(fragment)
    return {"id": fragment.id, "status": "stored", "memory": fragment.to_dict()}


@app.post("/goal")
async def create_goal(req: GoalRequest):
    """Create and durably persist a goal."""
    _load_identity_or_404(req.identity_id)
    from core.goals import Goal, GoalPriority, GoalScope

    try:
        priority = GoalPriority[req.priority.upper()]
        scope = GoalScope(req.scope.lower())
    except (KeyError, ValueError) as exc:
        raise HTTPException(
            status_code=400,
            detail="priority must be low|medium|high|critical and scope must be immediate|session|persistent|lifelong",
        ) from exc
    goal = Goal(
        title=req.title,
        description=req.description,
        priority=priority,
        scope=scope,
        success_criteria=req.success_criteria,
        metadata={"identity_id": req.identity_id},
    )
    runtime.goal_engine.add(goal)
    runtime._persist_goals(req.identity_id)
    return {"status": "created", "goal": goal.to_dict()}


@app.post("/relationship")
async def create_relationship(req: RelationshipRequest):
    """Create or update an identity relationship and persist the graph edge."""
    _load_identity_or_404(req.identity_id)
    from identity_graph.graph import EdgeType, TrustLevel

    try:
        edge_type = EdgeType(req.edge_type.lower())
    except ValueError as exc:
        allowed = ", ".join(item.value for item in EdgeType)
        raise HTTPException(
            status_code=400,
            detail=f"Unknown edge_type '{req.edge_type}'. Allowed: {allowed}",
        ) from exc
    trust_level = (
        TrustLevel.ABSOLUTE if req.trust_level >= 0.9
        else TrustLevel.HIGH if req.trust_level >= 0.7
        else TrustLevel.MEDIUM if req.trust_level >= 0.4
        else TrustLevel.LOW if req.trust_level >= 0.1
        else TrustLevel.NONE
    )
    edge = runtime.identity_graph.connect(
        source_id=req.identity_id,
        target_id=req.entity_id,
        edge_type=edge_type,
        trust_level=trust_level,
        context=req.context,
    )
    runtime._persist_relationships(req.identity_id)
    return {
        "status": "recorded",
        "relationship": {
            "id": edge.id,
            "source_id": edge.source_id,
            "target_id": edge.target_id,
            "edge_type": edge.edge_type.value,
            "trust_level": edge.trust_level.value,
            "strength": edge.strength,
            "context": edge.context,
        },
    }


@app.post("/timeline")
async def create_timeline_event(req: TimelineRequest):
    """Record and persist a chronological identity event."""
    _load_identity_or_404(req.identity_id)
    from core.timeline import LifeEvent, LifeEventType

    try:
        event_type = LifeEventType(req.event_type.lower())
    except ValueError as exc:
        allowed = ", ".join(item.value for item in LifeEventType)
        raise HTTPException(
            status_code=400,
            detail=f"Unknown event_type '{req.event_type}'. Allowed: {allowed}",
        ) from exc
    event = LifeEvent(
        identity_id=req.identity_id,
        event_type=event_type,
        title=req.title,
        description=req.description,
        significance=req.significance,
    )
    runtime.timeline_registry.record_event(req.identity_id, event)
    runtime._persist_timeline(req.identity_id)
    return {"status": "recorded", "event_id": event.id}


@app.post("/constitution")
async def inspect_constitution(req: IdentityRequest):
    """Return the governing constitution and laws for an identity."""
    _load_identity_or_404(req.identity_id)
    from identityos.identity import IdentityObject

    return IdentityObject(runtime, req.identity_id).constitution()


@app.post("/export")
async def export_identity(req: IdentityRequest):
    """Export a complete portable identity snapshot as JSON."""
    _load_identity_or_404(req.identity_id)
    from identityos.identity import IdentityObject

    return IdentityObject(runtime, req.identity_id).export()


@app.post("/context", response_model=ContextResponse)
async def get_context(req: ContextRequest):
    """
    Compose identity context (without invoking an LLM).
    Useful when the caller manages their own LLM call externally.
    """
    identity = runtime.load(req.identity_id)
    if not identity:
        raise HTTPException(status_code=404, detail=f"Identity '{req.identity_id}' not found")

    session_id = req.session_id or f"{req.user_id}_{req.identity_id}"
    user_id = runtime._resolved_user_id(req.identity_id, req.user_id)
    ctx = runtime.context_composer.compose(
        identity=identity,
        memory_store=runtime.memory_store,
        skill_registry=runtime.skill_registry,
        goal_engine=runtime.goal_engine,
        identity_graph=runtime.identity_graph,
        user_profile=runtime._get_user_profile(req.identity_id, user_id),
        user_id=user_id,
        query=req.message,
        session_id=session_id,
    )

    memories_used = ctx.memory_block.count("\n  [") if ctx.memory_block else 0

    logger.info(f"Context built for identity={req.identity_id} user={req.user_id} memories={memories_used}")

    return ContextResponse(
        augmented_context=ctx.render(),
        identity_name=identity.name,
        memories_used=memories_used,
        session_id=session_id,
    )


@app.post("/evaluate", response_model=EvaluateResponse)
async def evaluate(req: EvaluateRequest):
    """
    Evaluate an exchange (user message + LLM response) and decide
    what's worth remembering. Called after every LLM response.

    Uses the same classification and storage pipeline as process()
    via IdentityRuntime._extract_and_store_semantic_memory().
    """
    identity = runtime.load(req.identity_id)
    if not identity:
        raise HTTPException(status_code=404, detail=f"Identity '{req.identity_id}' not found")

    session_id = req.session_id or f"{req.user_id}_{req.identity_id}"

    report = runtime.evaluation_engine.evaluate(
        identity_id=req.identity_id,
        interaction_id=session_id,
        input_data=req.message,
        output_data=req.response,
    )

    stored = runtime._extract_and_store_semantic_memory(
        user_input=req.message,
        output=req.response,
        identity_id=req.identity_id,
        session_id=session_id,
        user_id=req.user_id,
    )

    # External assistants do not pass through IdentityRuntime.process(), so
    # record their platform-partitioned user relationship here. This keeps the
    # relationship graph truthful without asking content scripts to mutate it.
    runtime.identity_graph.interact_or_connect(
        source_id=req.identity_id,
        target_id=req.user_id,
        edge_type=EdgeType.PEER,
        bidirectional=False,
    )
    runtime._persist_relationships(req.identity_id)

    if stored:
        mem_type = stored.tags[-1] if len(stored.tags) > 1 else "general"
        logger.info(f"Stored {mem_type} memory for {req.identity_id}: {req.message[:60]}")
        return EvaluateResponse(
            memories_stored=1,
            summary=f"Stored {mem_type}: {req.message[:100]}",
            tags=[mem_type],
        )

    return EvaluateResponse(
        memories_stored=0,
        summary=f"Not memorable (score={report.overall_score:.2f})",
        tags=[],
    )


@app.post("/identity")
async def create_identity(req: CreateIdentityRequest):
    """Create a new identity."""
    from identityos import Identity
    identity = Identity.create(
        name=req.name,
        identity_id=req.identity_id,
        identity_class=req.identity_class,
        persona=req.persona,
        role=req.role,
        storage_path=os.environ.get("IDENTITY_STORE_PATH", ".identity_store"),
    )
    logger.info(f"Identity created via API: {req.identity_id} ({req.name})")
    return {"id": identity.id, "name": identity.name, "status": "created"}


@app.get("/identity/{identity_id}")
async def get_identity(identity_id: str):
    """Get a loaded identity spec by ID."""
    identity = runtime.load(identity_id)
    if not identity:
        raise HTTPException(status_code=404, detail=f"Identity '{identity_id}' not found")
    return identity.to_dict()


@app.get("/identity")
async def list_identities():
    """List all available identity IDs (loaded + stored)."""
    loaded = {s.id for s in runtime.list_identities()}
    stored = set(storage.list_identities())
    all_ids = sorted(loaded | stored)
    return {"identities": all_ids}


@app.get("/memories/{user_id}/{identity_id}", response_model=MemoriesResponse)
async def get_memories(user_id: str, identity_id: str, limit: int = 50):
    """Get stored memories for an identity."""
    memories = runtime.memory_store.by_user(
        identity_id=identity_id,
        user_id=user_id,
    )[:limit]
    return MemoriesResponse(
        identity_id=identity_id,
        user_id=user_id,
        memories=[m.to_dict() for m in memories],
        total=len(memories),
    )


@app.delete("/memories/{user_id}/{identity_id}")
async def clear_memories(user_id: str, identity_id: str):
    """Clear one user's memories without erasing other users or shared state."""
    deleted = runtime.memory_store.clear_user(identity_id, user_id)
    if runtime._storage is not None:
        runtime._storage.delete_user_memories(identity_id, user_id)
    return {"deleted": deleted, "message": "Memories cleared."}


@app.get("/session/{session_id}")
async def get_session(session_id: str):
    """Inspect session state (mode, active identity)."""
    identity_id = runtime._sessions.get(session_id)
    user_id = runtime._session_users.get(session_id)
    mode = runtime.get_session_mode(session_id)
    return {
        "session_id": session_id,
        "identity_id": identity_id,
        "user_id": user_id,
        "session_mode": mode.value,
        "is_isolated": mode.value != "normal",
    }


if __name__ == "__main__":
    uvicorn.run("runtime.main:app", host="0.0.0.0", port=8000, reload=False)
