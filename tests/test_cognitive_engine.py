"""Tests for core.cognitive_engine module."""

from core.cognitive_engine import (
    ComposedContext,
    ContextComposer,
    SessionMode,
    cap_custom_block_overage,
)
from core.identity import IdentitySpec
from core.memory import MemoryFragment, MemoryStore, MemoryType
from core.relationships import IdentityGraph


class TestComposedContext:
    def test_render_empty(self):
        ctx = ComposedContext()
        assert ctx.render() == ""

    def test_render_with_content(self):
        ctx = ComposedContext(identity_block="I am Alice.", memory_block="You like coffee.")
        rendered = ctx.render()
        assert "I am Alice." in rendered
        assert "You like coffee." in rendered

    def test_token_estimate(self):
        ctx = ComposedContext(identity_block="hello world")
        assert ctx.token_estimate(1.0) == len("hello world")


class TestContextComposer:
    def test_compose_identity_only(self):
        composer = ContextComposer(
            include_memory=False,
            include_skills=False,
            include_goals=False,
            include_relationships=False,
        )
        identity = IdentitySpec(id="id1", name="TestBot", role="assistant")
        ctx = composer.compose(identity=identity)
        assert identity.name in ctx.identity_block
        assert ctx.memory_block == ""

    def test_compose_with_memory(self):
        composer = ContextComposer(include_skills=False, include_goals=False)
        identity = IdentitySpec(id="id2", name="MemBot")
        store = MemoryStore()
        store.add(MemoryFragment(
            identity_id="id2",
            content="User loves Python",
            memory_type=MemoryType.SEMANTIC,
        ))
        ctx = composer.compose(
            identity=identity,
            memory_store=store,
            query="Python",
        )
        assert "Python" in ctx.memory_block

    def test_compose_with_graph(self):
        composer = ContextComposer(
            include_memory=False,
            include_skills=False,
            include_goals=False,
            include_relationships=True,
        )
        identity = IdentitySpec(id="id3", name="RelBot")
        graph = IdentityGraph()
        from identity_graph.graph import TrustLevel
        graph.connect("id3", "user1", trust_level=TrustLevel.HIGH)
        ctx = composer.compose(identity=identity, identity_graph=graph)
        assert "user1" in ctx.relationships_block


class TestSessionModePrompting:
    def test_roleplay_session_emits_session_mode_block(self):
        composer = ContextComposer(
            include_memory=False,
            include_skills=False,
            include_goals=False,
            include_relationships=False,
            include_motivations=False,
            include_timeline=False,
            include_synthesis=False,
        )
        identity = IdentitySpec(id="id4", name="RPer", role="assistant")
        ctx = composer.compose(identity=identity, session_mode=SessionMode.ROLEPLAY)
        assert "ROLEPLAY SESSION" in ctx.session_mode_block

    def test_normal_session_has_no_session_mode_block(self):
        composer = ContextComposer(include_memory=False)
        identity = IdentitySpec(id="id5", name="PlainBot")
        ctx = composer.compose(identity=identity, session_mode=SessionMode.NORMAL)
        assert not getattr(ctx, "session_mode_block", None)

    def test_render_orders_blocks_with_separator(self):
        ctx = ComposedContext(identity_block="I", memory_block="M", skills_block="S")
        rendered = ctx.render(separator="|")
        assert rendered == "I|M|S"

    def test_cap_custom_block_overage_trims_largest_custom_block(self):
        """Grounding arrives post-compose as a custom block; without a cap it
        pushed chat prompts past Groq's 8k TPM tier (observed 8.8k)."""
        ctx = ComposedContext(identity_block="id", memory_block="m" * 100)
        ctx.custom_blocks["self_knowledge"] = "g" * 1000
        ctx.custom_blocks["executive_state"] = "e" * 100
        cap_custom_block_overage(ctx, max_tokens=100)  # 400 char budget
        assert len(ctx.custom_blocks["self_knowledge"]) < 1000
        assert ctx.custom_blocks["executive_state"] == "e" * 100
        assert "<truncated" in ctx.custom_blocks["self_knowledge"] or "truncated" in ctx.custom_blocks["self_knowledge"]
        assert ctx.identity_block == "id"
        assert len(ctx.render()) <= 400

    def test_cap_custom_block_overage_noop_within_budget(self):
        ctx = ComposedContext(identity_block="small")
        ctx.custom_blocks["self_knowledge"] = "small too"
        cap_custom_block_overage(ctx, max_tokens=100)
        assert ctx.custom_blocks["self_knowledge"] == "small too"

    def test_custom_blocks_within_budget_untouched(self):
        ctx = ComposedContext(
            identity_block="id",
            custom_blocks={"self_knowledge": "grounding"},
        )
        assert "grounding" in ctx.render()
