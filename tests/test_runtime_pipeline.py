"""
Tests for the full IdentityRuntime pipeline:

- Semantic memory extraction during process()
- Timeline updates
- Relationship graph updates
- Memory persistence across restarts
- Identity isolation
- Session isolation
- No-adapter pipeline still stores memories
- Evaluation triggers persistence
"""

import json
from pathlib import Path

import pytest

from core.cognitive_engine import ContextComposer
from core.evaluation import register_default_criteria
from core.identity import create_identity
from core.memory import MemoryType
from runtime.orchestrator import (
    IdentityRuntime,
    InteractionRequest,
    _serialize_tool_result,
)
from runtime.persistence import JSONFileBackend


@pytest.fixture
def runtime():
    storage = JSONFileBackend(root_dir=str(Path(__file__).parent / ".test_store"))
    rt = IdentityRuntime(storage=storage)
    register_default_criteria(rt.evaluation_engine)
    yield rt
    # Cleanup
    import shutil
    shutil.rmtree(str(Path(__file__).parent / ".test_store"), ignore_errors=True)


@pytest.fixture
def identity(runtime):
    spec = create_identity(name="TestBot", identity_id="test-bot")
    runtime.register(spec)
    return spec


class TestToolResultBoundary:
    def test_small_results_are_preserved(self):
        result = _serialize_tool_result({"items": [1, 2, 3]}, 256)
        assert json.loads(result) == {"items": [1, 2, 3]}

    def test_large_results_are_explicitly_bounded(self):
        result = _serialize_tool_result({"items": ["x" * 5000]}, 512)
        decoded = json.loads(result)

        assert len(result) <= 512
        assert decoded["truncated"] is True
        assert decoded["original_chars"] > 5000
        assert decoded["result_excerpt"]

    def test_runtime_sanitizes_tool_result_limit(self):
        assert IdentityRuntime(max_tool_result_chars=1).max_tool_result_chars == 256

    def test_runtime_sanitizes_tool_catalog_limit(self):
        assert IdentityRuntime(max_tools_per_request=0).max_tools_per_request == 1

    def test_runtime_passes_total_generation_budget_to_adapter(self, tmp_path):
        class RecordingAdapter:
            model = "budget-test"

            def __init__(self):
                self.kwargs = {}

            def generate(self, context, user_input, identity, **kwargs):
                self.kwargs = kwargs
                return "bounded"

        adapter = RecordingAdapter()
        runtime = IdentityRuntime(
            storage=JSONFileBackend(root_dir=str(tmp_path / "store")),
            adapter=adapter,
            generation_budget_seconds=7.5,
        )
        identity = create_identity("Budget Bot", identity_id="budget-bot")
        runtime.register(identity)

        runtime.process(InteractionRequest(
            identity_id=identity.id,
            user_input="hello",
            session_id=runtime.start_session(identity.id),
        ))

        assert adapter.kwargs["_generation_budget"] == 7.5

    def test_runtime_resolves_dotted_alias_only_for_an_offered_tool(self, tmp_path):
        class DottedToolAdapter:
            model = "dotted-tool-test"

            def __init__(self):
                self.tool_result = ""

            def generate(self, context, user_input, identity, **kwargs):
                self.tool_result = kwargs["execute_tool"]("datetime.now", {"tz_name": "UTC"})
                return self.tool_result

        adapter = DottedToolAdapter()
        storage = JSONFileBackend(root_dir=str(tmp_path / "store"))
        runtime = IdentityRuntime(storage=storage, adapter=adapter)
        identity = create_identity("Alias Bot", identity_id="alias-bot")
        runtime.register(identity)
        runtime.capability_registry.install(identity.id, "datetime")
        session_id = runtime.start_session(identity.id)

        runtime.process(InteractionRequest(
            identity_id=identity.id,
            user_input="What is the time?",
            session_id=session_id,
        ))

        assert "Unknown tool" not in adapter.tool_result
        assert json.loads(adapter.tool_result)["timezone"] == "UTC"


class TestSemanticExtraction:
    """process() must extract and store semantic memories from user input."""

    def test_preference_extracted(self, runtime, identity):
        sid = runtime.start_session(identity.id)
        req = InteractionRequest(
            identity_id=identity.id,
            user_input="My favorite color is green.",
            session_id=sid,
        )
        runtime.process(req)
        mems = runtime.memory_store.by_identity(identity_id=identity.id)
        semantic = [m for m in mems if m.memory_type == MemoryType.SEMANTIC]
        assert len(semantic) >= 1
        assert any("green" in m.content for m in semantic)

    def test_decision_extracted(self, runtime, identity):
        sid = runtime.start_session(identity.id)
        req = InteractionRequest(
            identity_id=identity.id,
            user_input="I've decided to use Python for this project.",
            session_id=sid,
        )
        runtime.process(req)
        mems = runtime.memory_store.by_identity(identity_id=identity.id)
        semantic = [m for m in mems if m.memory_type == MemoryType.SEMANTIC]
        assert len(semantic) >= 1
        assert any("Python" in m.content for m in semantic)

    def test_general_input_not_extracted(self, runtime, identity):
        sid = runtime.start_session(identity.id)
        req = InteractionRequest(
            identity_id=identity.id,
            user_input="What time is it?",
            session_id=sid,
        )
        runtime.process(req)
        mems = runtime.memory_store.by_identity(identity_id=identity.id)
        semantic = [m for m in mems if m.memory_type == MemoryType.SEMANTIC]
        # General questions should NOT be extracted as semantic memories
        assert len(semantic) == 0

    def test_episodic_always_stored(self, runtime, identity):
        sid = runtime.start_session(identity.id)
        req = InteractionRequest(
            identity_id=identity.id,
            user_input="What time is it?",
            session_id=sid,
        )
        runtime.process(req)
        mems = runtime.memory_store.by_identity(identity_id=identity.id)
        episodic = [m for m in mems if m.memory_type == MemoryType.EPISODIC]
        assert len(episodic) >= 1

    def test_multiple_interactions_accumulate(self, runtime, identity):
        sid = runtime.start_session(identity.id)
        inputs = [
            "My favorite color is blue.",
            "I love Python programming.",
            "I hate waiting in lines.",
        ]
        for text in inputs:
            runtime.process(InteractionRequest(
                identity_id=identity.id, user_input=text, session_id=sid,
            ))
        mems = runtime.memory_store.by_identity(identity_id=identity.id)
        # 3 EPISODIC + up to 3 SEMANTIC (depending on classification)
        assert len(mems) >= 3
        semantic = [m for m in mems if m.memory_type == MemoryType.SEMANTIC]
        assert len(semantic) >= 1


class TestMemoryPersistence:
    """Memories must survive runtime restart via StorageBackend."""

    def test_memories_persist_after_restart(self):
        store_path = str(Path(__file__).parent / ".test_store_persist")
        storage1 = JSONFileBackend(root_dir=store_path)
        rt1 = IdentityRuntime(storage=storage1)
        register_default_criteria(rt1.evaluation_engine)
        spec = create_identity(name="PersistBot", identity_id="persist-bot")
        rt1.register(spec)
        sid = rt1.start_session("persist-bot")
        rt1.process(InteractionRequest(
            identity_id="persist-bot",
            user_input="My favorite color is red.",
            session_id=sid,
        ))
        assert len(rt1.memory_store) >= 1

        # Simulate restart
        storage2 = JSONFileBackend(root_dir=store_path)
        rt2 = IdentityRuntime(storage=storage2)
        register_default_criteria(rt2.evaluation_engine)
        rt2.load_persisted()

        mems = rt2.memory_store.by_identity(identity_id="persist-bot")
        assert len(mems) >= 1
        semantic = [m for m in mems if m.memory_type == MemoryType.SEMANTIC]
        assert any("red" in m.content for m in semantic)

        import shutil
        shutil.rmtree(store_path, ignore_errors=True)


class TestIdentityIsolation:
    """Multiple identities must have isolated memory, timeline, and graph."""

    def test_memories_isolated(self, runtime):
        bot1 = create_identity(name="Bot1", identity_id="bot1")
        bot2 = create_identity(name="Bot2", identity_id="bot2")
        runtime.register(bot1)
        runtime.register(bot2)

        sid1 = runtime.start_session("bot1")
        sid2 = runtime.start_session("bot2")
        runtime.process(InteractionRequest(
            identity_id="bot1", user_input="Bot1 secret", session_id=sid1,
        ))
        runtime.process(InteractionRequest(
            identity_id="bot2", user_input="Bot2 secret", session_id=sid2,
        ))

        mems1 = runtime.memory_store.by_identity(identity_id="bot1")
        mems2 = runtime.memory_store.by_identity(identity_id="bot2")
        assert all("Bot1" in m.content for m in mems1)
        assert all("Bot2" in m.content for m in mems2)

    def test_context_composer_isolates_identities(self, runtime):
        bot1 = create_identity(name="Bot1", identity_id="bot1")
        bot2 = create_identity(name="Bot2", identity_id="bot2")
        runtime.register(bot1)
        runtime.register(bot2)

        sid1 = runtime.start_session("bot1")
        sid2 = runtime.start_session("bot2")
        runtime.process(InteractionRequest(
            identity_id="bot1", user_input="Bot1 data", session_id=sid1,
        ))
        runtime.process(InteractionRequest(
            identity_id="bot2", user_input="Bot2 data", session_id=sid2,
        ))

        cc = ContextComposer()
        ctx1 = cc.compose(identity=bot1, memory_store=runtime.memory_store, query=None)
        ctx2 = cc.compose(identity=bot2, memory_store=runtime.memory_store, query=None)

        mem_block_1 = ctx1.memory_block or ""
        mem_block_2 = ctx2.memory_block or ""
        assert "Bot2" not in mem_block_1
        assert "Bot1" not in mem_block_2


class TestSessionIsolation:
    """Multiple sessions for the same identity must not leak."""

    def test_sessions_isolated(self, runtime, identity):
        sid_a = runtime.start_session(identity.id)
        sid_b = runtime.start_session(identity.id)

        runtime.process(InteractionRequest(
            identity_id=identity.id, user_input="Session A only", session_id=sid_a,
        ))
        runtime.process(InteractionRequest(
            identity_id=identity.id, user_input="Session B only", session_id=sid_b,
        ))

        mems_a = runtime.memory_store.by_session(sid_a)
        mems_b = runtime.memory_store.by_session(sid_b)
        assert all("Session A" in m.content for m in mems_a)
        assert all("Session B" in m.content for m in mems_b)


class TestNoAdapterPipeline:
    """Even without an LLM adapter, the pipeline must store memories."""

    def test_no_adapter_stores_memories(self):
        storage = JSONFileBackend(root_dir=str(Path(__file__).parent / ".test_store_noadapter"))
        rt = IdentityRuntime(storage=storage)
        register_default_criteria(rt.evaluation_engine)
        spec = create_identity(name="NoAdapterBot", identity_id="no-adapter")
        rt.register(spec)

        sid = rt.start_session("no-adapter")
        resp = rt.process(InteractionRequest(
            identity_id="no-adapter",
            user_input="I enjoy hiking on weekends.",
            session_id=sid,
        ))
        assert "No adapter configured" in resp.output

        mems = rt.memory_store.by_identity(identity_id="no-adapter")
        assert len(mems) >= 1
        semantic = [m for m in mems if m.memory_type == MemoryType.SEMANTIC]
        assert any("hiking" in m.content for m in semantic)

        import shutil
        shutil.rmtree(str(Path(__file__).parent / ".test_store_noadapter"), ignore_errors=True)


class TestTimelineUpdates:
    """process() must record timeline life events."""

    def test_timeline_recorded(self, runtime, identity):
        sid = runtime.start_session(identity.id)
        runtime.process(InteractionRequest(
            identity_id=identity.id, user_input="Hello", session_id=sid,
        ))
        timeline = runtime.timeline_registry.get(identity.id)
        assert timeline is not None
        events = timeline.events()
        # Creation + at least one interaction
        assert len(events) >= 2

    def test_multiple_interactions_multiple_events(self, runtime, identity):
        sid = runtime.start_session(identity.id)
        for i in range(3):
            runtime.process(InteractionRequest(
                identity_id=identity.id, user_input=f"Message {i}", session_id=sid,
            ))
        timeline = runtime.timeline_registry.get(identity.id)
        events = timeline.events()
        # Creation + 3 interactions
        assert len(events) >= 4


class TestRelationshipUpdates:
    """process() must update the relationship graph."""

    def test_relationship_created(self, runtime, identity):
        sid = runtime.start_session(identity.id)
        runtime.process(InteractionRequest(
            identity_id=identity.id, user_input="Hello", session_id=sid,
        ))
        edges = runtime.identity_graph.get_relationships(identity.id)
        assert len(edges) >= 1
        assert edges[0].source_id == identity.id


class TestEvaluateEndpointConsistency:
    """The /evaluate endpoint must maintain consistency with process()."""

    def test_evaluate_and_process_share_classification(self, runtime, identity):
        """Both paths should classify 'my favorite' as a preference."""
        from core.evaluation import classify_memory_type, is_worth_remembering

        message = "My favorite color is green."
        response = "[No adapter configured]"

        assert is_worth_remembering(message, response) is True
        assert classify_memory_type(message, response) == "preference"

        # process() should produce the same classification
        sid = runtime.start_session(identity.id)
        runtime.process(InteractionRequest(
            identity_id=identity.id, user_input=message, session_id=sid,
        ))
        mems = runtime.memory_store.by_identity(identity_id=identity.id)
        semantic = [m for m in mems if m.memory_type == MemoryType.SEMANTIC]
        assert len(semantic) >= 1
        assert semantic[0].memory_type == MemoryType.SEMANTIC
