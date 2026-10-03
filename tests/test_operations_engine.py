from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from core.capabilities.email.backends import FileMailboxBackend, MailboxTransport
from core.operations import (
    CallableCandidateSource,
    Candidate,
    ControlState,
    FollowUpPlanner,
    MessageStatus,
    Need,
    NeedStatus,
    OperationsEngine,
    OperationsStore,
    OperatorConfig,
    OpportunityStatus,
    RequirementRule,
    StaticCandidateSource,
    TargetEvaluator,
)
from core.operations.aster import build_aster_engine
from core.operations.policy import Authority, AuthorityPolicy, is_conversational
from runtime.persistence import InMemoryBackend


class _StubAdapter:
    """Deterministic stand-in for a model generation runtime.

    Model-backed reply tests must exercise the *model generation* path, not a
    canned template, so the engine is given a stub adapter whose ``generate``
    returns a real (subject + body) reply.
    """

    model = "stub-model"

    def generate(self, context: str, user_input: str, identity: Any, **kwargs: Any) -> str:
        if '"inbound"' in user_input or '"intent"' in user_input:
            return "Subject: Re: your note\nThanks for the note. This model-drafted answer comes from the verified facts."
        return (
            "Subject: Connecting IdentityOS with your research\n"
            "Dear Alice,\n\nI'm Aster reaching out on behalf of IdentityOS because your "
            "distributed identity systems research connects directly to what we are building.\n\n"
            "Transparency: I am an AI operator."
        )


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir(exist_ok=True)
    (root / "README.md").write_text(
        "# Sample Project\n\nA small experiment in distributed systems.\n", encoding="utf-8"
    )
    (root / "pyproject.toml").write_text(
        '[project]\nname = "sample"\nversion = "0.1.0"\n', encoding="utf-8"
    )
    return root


def _candidate(**overrides):
    base = dict(
        target_name="Alice Example",
        organization="Example Foundation",
        contact_email="alice@example.org",
        category="funding",
        relevant_work=["distributed identity systems research"],
        evidence=["directory:example-fund"],
        fit_reason="works on distributed identity systems research",
        value_proposition="an open identity runtime with durable state",
        potential_ask="a short conversation about your program",
        confidence=0.5,
    )
    base.update(overrides)
    return Candidate(**base)


def _engine(tmp_path, storage=None, *, controls=None, candidate=None, capability_registry=None, required_skills=None, adapter=None):
    storage = storage or InMemoryBackend()
    backend = FileMailboxBackend(tmp_path / "mailbox", mailbox="aster")
    transport = MailboxTransport(backend)
    config = OperatorConfig(
        identity_id="aster",
        project_root=str(_project(tmp_path)),
        project_name="IdentityOS",
        sender_name="Aster",
        sender_email="aster@identityos.local",
        signature="Aster",
        transparency="I am an AI operator.",
        purpose="IdentityOS outreach",
        need_rules=[
            RequirementRule(
                category="funding",
                description="Secure funding or sponsorship to sustain the project",
                probe=r"\b(fund|grant|sponsor|invest)\b",
                expect="absent",
                urgency=0.8,
                impact=0.9,
            ),
            RequirementRule(
                category="collaborators",
                description="Attract collaborators to accelerate development",
                probe=r"\b(contributor|collaborator|maintainer)\b",
                expect="absent",
                urgency=0.6,
                impact=0.7,
            ),
        ],
        candidate_sources=[StaticCandidateSource([candidate or _candidate()])],
        required_skills=required_skills or ["web.fetch"],
        pursue_threshold=0.45,
        hold_threshold=0.3,
    )
    engine = OperationsEngine(
        storage, config, transport=transport, adapter=_StubAdapter() if adapter is None else adapter,
        capability_registry=capability_registry,
    )
    # Tests that exercise actual outreach/default behaviour run in autonomous
    # mode. The conservative persisted default is 'observe' (covered explicitly
    # by test_observe_mode_* below).
    controls = ControlState(outbound_mode="autonomous") if controls is None else controls
    engine.store.set_controls(controls)
    return engine, backend


# ── discovery / evaluation / outreach ────────────────────────────────────────


def test_tick_discovers_evaluates_and_sends_outreach(tmp_path):
    engine, backend = _engine(tmp_path)
    report = engine.tick()

    assert report.observed is True
    assert report.needs_created, "a need should be detected"
    assert report.opportunities_created, "an opportunity should be discovered"
    assert report.outreach_sent, "outreach should be sent through the transport"

    relationships = engine.store.list_relationships()
    assert len(relationships) == 1
    assert relationships[0].status.value == "outreach_sent"
    assert relationships[0].email == "alice@example.org"

    sent = backend.outbox()
    assert len(sent) == 1
    assert "distributed identity systems" in sent[0]["body"]
    assert "Aster" in sent[0]["body"]
    assert "AI operator" in sent[0]["body"]

    assert engine.store.budget().cold_outreach == 1
    phases = {p.phase.value for p in engine.store.list_provenance()}
    assert {"observe", "detect_needs", "discover", "evaluate", "act"} <= phases


def test_second_tick_does_not_duplicate_outreach(tmp_path):
    engine, backend = _engine(tmp_path)
    report = engine.tick()
    assert report.outreach_sent
    report = engine.tick()
    assert report.outreach_sent == []
    assert report.escalations == []
    assert len(backend.outbox()) == 1
    assert len(engine.store.list_relationships()) == 1


def test_duplicate_policy_blocks_recontacting_same_target(tmp_path):
    engine, backend = _engine(tmp_path)
    engine.tick()
    # Put the same opportunity back into the qualified pool; the cycle must not
    # produce a second outreach to the same person.
    for opp in engine.store.list_opportunities():
        opp.status = OpportunityStatus.QUALIFIED
        engine.store.update_opportunity(opp)
    report = engine.tick()
    assert report.outreach_sent == []
    assert any(s.get("reason") in ("already_contacted", "opted_out") for s in report.skipped)
    assert len(backend.outbox()) == 1


def test_restart_survives_and_prevents_resend(tmp_path):
    storage = InMemoryBackend()
    engine, backend = _engine(tmp_path, storage=storage)
    engine.tick()
    assert len(backend.outbox()) == 1

    # Brand-new engine, same storage: simulates a process restart.
    revived, backend2 = _engine(tmp_path, storage=storage)
    status = revived.status()
    assert status["relationships"]["total"] == 1
    report = revived.tick()
    assert report.outreach_sent == []
    assert len(backend2.outbox()) == 1


def test_budget_blocks_outreach(tmp_path):
    engine, backend = _engine(
        tmp_path, controls=ControlState(outbound_mode="autonomous", max_cold_outreach_per_day=0)
    )
    report = engine.tick()
    assert report.outreach_sent == []
    assert any(s.get("reason") == "daily_budget_exhausted" for s in report.skipped)
    assert backend.outbox() == []


# ── outbound operating mode / allowlist ──────────────────────────────────────


def test_default_outbound_mode_is_observe():
    from core.operations.models import ControlState as CS

    assert CS().outbound_mode == "observe"


def test_observe_mode_records_would_send_without_transmitting(tmp_path):
    engine, backend = _engine(tmp_path, controls=ControlState(outbound_mode="observe"))
    report = engine.tick()

    assert report.outreach_sent == []
    assert backend.outbox() == []
    drafts = [m for m in engine.store.list_messages() if m.status is MessageStatus.WOULD_SEND]
    assert len(drafts) == 1
    assert drafts[0].authorization == "observe_would_send"
    assert drafts[0].opportunity_id
    # No relationship is forged: observe never contacts anyone.
    assert engine.store.list_relationships() == []
    # Observation does not consume the sending budget.
    assert engine.store.budget().cold_outreach == 0

    # A second tick must not re-draft the same target.
    engine.tick()
    drafts = [m for m in engine.store.list_messages() if m.status is MessageStatus.WOULD_SEND]
    assert len(drafts) == 1

    assert engine.status()["would_send"] == 1
    assert any(p.action == "would_send" for p in engine.store.list_provenance())

    # Switching to autonomous sends the drafted outreach through the transport.
    engine.set_outbound_mode("autonomous")
    report = engine.tick()
    assert report.outreach_sent
    assert len(backend.outbox()) == 1
    assert engine.store.list_relationships()[0].status.value == "outreach_sent"


def test_approval_required_mode_escalates_every_outbound(tmp_path):
    engine, backend = _engine(
        tmp_path, controls=ControlState(outbound_mode="approval_required")
    )
    report = engine.tick()
    assert report.outreach_sent == []
    assert report.escalations, "approval_required must escalate, not send"
    assert backend.outbox() == []
    pending = engine.pending_authorizations()
    assert len(pending) == 1
    result = engine.authorize(pending[0]["message_id"], approved=True, note="approved by ae")
    assert result["ok"] is True
    assert len(backend.outbox()) == 1


def test_allowlist_gates_cold_outreach(tmp_path):
    engine, backend = _engine(
        tmp_path,
        controls=ControlState(
            outbound_mode="autonomous",
            allowed_external_recipients=["someone-else@example.org"],
        ),
    )
    report = engine.tick()
    assert report.outreach_sent == []
    assert any(s.get("reason") == "not_in_allowlist" for s in report.skipped)
    assert backend.outbox() == []

    engine.override(allowed_external_recipients=["alice@example.org"])
    report = engine.tick()
    assert report.outreach_sent
    assert len(backend.outbox()) == 1
    assert engine.store.list_relationships()[0].email == "alice@example.org"


def test_allowlist_domain_suffix_matches(tmp_path):
    engine, backend = _engine(
        tmp_path,
        controls=ControlState(outbound_mode="autonomous", allowed_external_recipients=["@example.org"]),
    )
    report = engine.tick()
    assert report.outreach_sent, "alice@example.org should match the @example.org suffix"
    assert len(backend.outbox()) == 1


def test_observe_mode_drafts_replies_without_sending(tmp_path):
    engine, backend = _engine(tmp_path, controls=ControlState(outbound_mode="observe"))
    engine.tick()
    # Inbound response scope: only trusted/known senders may be answered at all.
    # Observe-mode outreach drafts do not (anymore) forge a relationship, so a
    # genuine reply requires the sender to already be in trusted scope.
    from core.operations.models import Relationship, RelationshipStatus

    trusted = Relationship(
        display_name="Alice Example", email="alice@example.org", status=RelationshipStatus.ENGAGED,
    )
    engine.store.add_relationship(trusted)
    backend.deliver({
        "external_id": "<reply-observe@example.org>",
        "from": "alice@example.org",
        "thread_id": "thread-1",
        "subject": "Re: hello",
        "body": "What exactly is IdentityOS?",
    })
    report = engine.tick()
    assert report.replies_sent == []
    assert engine.status()["would_send"] >= 1
    drafts = [m for m in engine.store.list_messages() if m.status is MessageStatus.WOULD_SEND]
    assert any(m.authorization == "observe_would_send:reply" for m in drafts)
    draft = [m for m in drafts if m.authorization == "observe_would_send:reply"]
    assert draft, "the reply should be drafted (but not sent) in observe mode"
    # Nothing was transmitted.
    assert len(backend.outbox()) == 0
    # A genuinely NEW inbound while autonomous is answered through the transport.
    engine.set_outbound_mode("autonomous")
    backend.deliver({
        "external_id": "<reply-2@example.org>",
        "from": "alice@example.org",
        "thread_id": "thread-2",
        "subject": "Re: hello",
        "body": "And what does the runtime actually persist?",
    })
    report = engine.tick()
    assert report.replies_sent, "a later autonomous tick should answer the inbound"
    assert any(m.get("thread_id") == "thread-2" for m in backend.outbox())


# ── escalation / authorization ───────────────────────────────────────────────


def test_require_approval_category_escalates_and_authorize_sends(tmp_path):
    engine, backend = _engine(
        tmp_path, controls=ControlState(require_approval_categories=["funding"])
    )
    report = engine.tick()
    assert report.outreach_sent == []
    assert report.escalations, "outreach should be escalated"
    pending = engine.pending_authorizations()
    assert len(pending) == 1
    assert backend.outbox() == []

    result = engine.authorize(pending[0]["message_id"], approved=True, note="approved by ae")
    assert result["ok"] is True
    assert len(backend.outbox()) == 1
    rel = engine.store.get_relationship(pending[0]["relationship_id"])
    assert rel.status.value == "outreach_sent"


def test_rejecting_escalation_does_not_send(tmp_path):
    engine, backend = _engine(
        tmp_path, controls=ControlState(require_approval_categories=["funding"])
    )
    engine.tick()
    pending = engine.pending_authorizations()
    result = engine.authorize(pending[0]["message_id"], approved=False, note="not now")
    assert result["status"] == "rejected"
    assert backend.outbox() == []
    message = engine.store.get_message(pending[0]["message_id"])
    assert message.status is MessageStatus.FAILED


def test_paused_operator_does_not_act(tmp_path):
    engine, backend = _engine(tmp_path)
    engine.pause("human paused this")
    report = engine.tick()
    assert engine.mode == "paused"
    assert report.skipped and report.skipped[0]["reason"] == "paused"
    assert backend.outbox() == []


# ── monitoring / replies ─────────────────────────────────────────────────────


def test_inbound_question_gets_autonomous_reply(tmp_path):
    engine, backend = _engine(tmp_path)
    engine.tick()
    backend.deliver({
        "external_id": "<reply-1@example.org>",
        "from": "alice@example.org",
        "thread_id": "thread-1",
        "subject": "Re: hello",
        "body": "Thanks for reaching out. What exactly is IdentityOS?",
    })
    report = engine.tick()
    assert report.replies_sent, "a conversational reply should be sent"
    assert engine.store.budget().replies == 1
    rel = engine.store.list_relationships()[0]
    assert rel.status.value == "engaged"
    # A recorded reply must actually have been transmitted through the transport.
    outbound = [m for m in backend.outbox() if m.get("thread_id") == "thread-1"]
    assert outbound, "autonomous reply must reach the mailbox, not just the ledger"
    assert outbound[-1]["body"].strip() and outbound[-1]["external_id"], "sent reply must carry body and a Message-ID"
    # The reply must be provably model-generated, never a canned template.
    sent_message = [m for m in engine.store.list_messages() if m.status is MessageStatus.SENT]
    assert sent_message and any(m.generation.get("mode") == "identity_model_generation" for m in sent_message)
    model_reply = next(m for m in sent_message if m.generation.get("mode") == "identity_model_generation")
    assert model_reply.external_id, "persisted outbound Message-ID must not be empty"


def test_opt_out_is_honoured(tmp_path):
    engine, backend = _engine(tmp_path)
    engine.tick()
    backend.deliver({
        "external_id": "<optout-1@example.org>",
        "from": "alice@example.org",
        "thread_id": "thread-1",
        "subject": "Re: hello",
        "body": "Please unsubscribe me and do not contact me again.",
    })
    engine.tick()
    rel = engine.store.list_relationships()[0]
    assert rel.opted_out is True
    assert rel.status.value == "opted_out"
    # A later tick must not follow up or send again.
    report = engine.tick()
    assert report.outreach_sent == []
    assert report.follow_ups_sent == []
    assert len(backend.outbox()) == 1


def test_sensitive_inbound_request_is_escalated(tmp_path):
    engine, backend = _engine(tmp_path)
    engine.tick()
    backend.deliver({
        "external_id": "<sensitive-1@example.org>",
        "from": "alice@example.org",
        "thread_id": "thread-1",
        "subject": "Re: hello",
        "body": "Happy to invest. What are your investment terms and equity?",
    })
    report = engine.tick()
    assert report.replies_sent == []
    rel = engine.store.list_relationships()[0]
    assert rel.status.value == "awaiting_human_authorization"


def test_approval_escalation_raises_principal_notification(tmp_path):
    engine, backend = _engine(tmp_path, controls=ControlState(outbound_mode="approval_required"))
    report = engine.tick()
    assert report.escalations
    notifications = engine.store.list_notifications()
    assert notifications, "escalation must raise a notification"
    assert notifications[0].kind == "escalation"
    assert engine.store.unread_notification_count() == len(notifications)
    # The notification ledger must survive a process restart (fresh store).
    revived = OperationsStore(engine.storage, "aster")
    assert revived.list_notifications()


def test_authorize_is_scoped_and_replay_proof(tmp_path):
    engine, backend = _engine(tmp_path, controls=ControlState(outbound_mode="approval_required"))
    report = engine.tick()
    pending = engine.pending_authorizations()
    assert pending, "approval_required must escalate the outreach"
    message_id = pending[0]["message_id"]

    result = engine.authorize(message_id, approved=True, note="approved by ae", approver="ae")
    assert result.get("ok") is True and result.get("status") == "sent"

    # Replay attack: the same authorization request cannot be sent twice.
    replay = engine.authorize(message_id, approved=True, note="try again", approver="ae")
    assert replay.get("ok") is False
    assert "not awaiting" in replay.get("error", "")

    msg = engine.store.get_message(message_id)
    assert "human_authorized:" in msg.authorization and message_id in msg.authorization

    ledger = engine.store.list_provenance()
    auth_entries = [p for p in ledger if p.action == "authorize"]
    assert auth_entries, "authorization must be recorded in the audit ledger"
    entry = auth_entries[-1]
    assert entry.refs.get("decision") == "approve"
    assert entry.refs.get("approver") == "ae"
    assert entry.refs["scope"].get("outbound_mode") == "approval_required"

    # An authorization resolution itself raises a notification (approved then read).
    engine.store.mark_notifications_read()
    auth_ntf = [n for n in engine.store.list_notifications() if n.kind == "authorization"]
    assert auth_ntf, "the approved authorization must be surfaced to the principal"


def test_rejected_authorization_records_approver_decision(tmp_path):
    engine, _ = _engine(tmp_path, controls=ControlState(outbound_mode="approval_required"))
    engine.tick()
    pending = engine.pending_authorizations()
    result = engine.authorize(pending[0]["message_id"], approved=False, note="not now", approver="ae")
    assert result.get("status") == "rejected"
    msgs = engine.store.list_messages()
    assert msgs[-1].status is MessageStatus.FAILED
    entries = [p for p in engine.store.list_provenance() if p.action == "authorize"]
    assert entries[-1].refs.get("decision") == "reject"
    assert entries[-1].refs.get("approver") == "ae"


# ── follow-ups ───────────────────────────────────────────────────────────────


def test_follow_up_is_planned_and_sent(tmp_path):
    engine, backend = _engine(tmp_path)
    engine.tick()
    rel = engine.store.list_relationships()[0]
    old = (datetime.now(timezone.utc) - timedelta(hours=120)).isoformat()
    rel.last_outbound_at = old
    engine.store.update_relationship(rel)

    report = engine.tick()
    assert report.follow_ups_sent, "a quiet relationship should get one follow-up"
    assert len(backend.outbox()) == 2
    rel = engine.store.list_relationships()[0]
    assert rel.follow_up_count == 1


def test_follow_up_respects_maximum(tmp_path):
    engine, _ = _engine(tmp_path)
    engine.tick()
    rel = engine.store.list_relationships()[0]
    rel.follow_up_count = 2
    rel.last_outbound_at = (datetime.now(timezone.utc) - timedelta(hours=500)).isoformat()
    engine.store.update_relationship(rel)
    planner = FollowUpPlanner(engine.store)
    assert planner.plan() == []


# ── policy unit tests ────────────────────────────────────────────────────────


def test_authority_policy_commitment_mode_allows_program_questions():
    policy = AuthorityPolicy()
    decision = policy.evaluate(
        "send cold outreach about a program",
        content="We would like to ask about your investment program.",
        mode="commitment",
    )
    assert decision.authority is Authority.AUTONOMOUS


def test_authority_policy_blocks_explicit_commitment():
    policy = AuthorityPolicy()
    decision = policy.evaluate(
        "send cold outreach",
        content="We will invest $50,000 and sign the contract today.",
        mode="commitment",
    )
    assert decision.authority is Authority.AWAITING_HUMAN_AUTHORIZATION


def test_authority_policy_content_mode_escalates_sensitive_topics():
    policy = AuthorityPolicy()
    decision = policy.evaluate("reply", content="Here are the investment terms.")
    assert decision.requires_human


def test_conversational_intents_are_bounded():
    assert is_conversational("question")
    assert is_conversational("thanks")
    assert not is_conversational("commitment")


def test_target_evaluator_requires_contact_channel():
    evaluator = TargetEvaluator()
    from core.operations.models import Need, Opportunity

    need = Need(category="funding", description="funding", urgency=0.8, impact=0.9)
    opp = Opportunity(
        need_id=need.id,
        target_name="Ghost",
        category="funding",
        relevant_work=["distributed identity"],
        evidence=["a", "b"],
        fit_reason="relevant",
    )
    evaluation = evaluator.evaluate(opp, need)
    assert evaluation.factors["reachability"] == 0.0
    assert evaluation.recommendation in ("hold", "reject")


# ── capability gap ───────────────────────────────────────────────────────────


def test_missing_skill_creates_need(tmp_path):
    engine, _ = _engine(tmp_path)
    engine.tick()
    # required_skills is web.fetch with no registry -> reported as a gap.
    status = engine.status()
    assert status["needs"]["total"] >= 1
    store = OperationsStore(engine.storage, "aster")
    assert any("web.fetch" in n.description for n in store.list_needs())


# ── inbound disposition scope (live-test failure #1) ─────────────────────────


def test_automated_sender_is_ignored_and_forges_no_relationship(tmp_path):
    engine, backend = _engine(tmp_path, controls=ControlState(outbound_mode="autonomous"))
    engine.tick()
    before = len(engine.store.list_relationships())
    backend.deliver({
        "external_id": "<no-reply@google.com>",
        "from": "no-reply@google.com",
        "thread_id": "thread-g",
        "subject": "Security alert",
        "body": "Someone signed in from a new device.",
        "references": [],
    })
    report = engine.tick()
    assert report.replies_sent == []
    assert len(engine.store.list_relationships()) == before, "automation must not create a relationship"
    inbound = [m for m in engine.store.list_messages() if m.direction.name == "INBOUND"]
    assert any(m.external_id == "<no-reply@google.com>" for m in inbound), "excluded mail is still recorded for audit"
    assert any("no-reply@google.com [automated]" in p.summary and "ignored" in p.summary
               for p in engine.store.list_provenance())


def test_unsolicited_unknown_sender_is_quarantined(tmp_path):
    engine, backend = _engine(tmp_path, controls=ControlState(outbound_mode="autonomous"))
    engine.tick()
    before = len(engine.store.list_relationships())
    backend.deliver({
        "external_id": "<stranger@example.net>",
        "from": "stranger@example.net",
        "thread_id": "thread-x",
        "subject": "Hello",
        "body": "Do you have time to talk next week?",
        "references": [],
    })
    report = engine.tick()
    assert report.replies_sent == []
    assert len(engine.store.list_relationships()) == before
    assert any("stranger@example.net [unsolicited_unknown]" in p.summary and "quarantined" in p.summary
               for p in engine.store.list_provenance())


def test_unexpected_sender_in_established_thread_is_quarantined(tmp_path):
    engine, backend = _engine(tmp_path, controls=ControlState(outbound_mode="autonomous"))
    engine.tick()  # outreach → trusted relationship with alice@example.org
    backend.deliver({
        "external_id": "<alice-1@example.org>",
        "from": "alice@example.org",
        "thread_id": "thread-abc",
        "subject": "Re: hello",
        "body": "Questions about IdentityOS?",
        "references": [],
    })
    engine.tick()
    backend.deliver({
        "external_id": "<mallory-1@example.org>",
        "from": "mallory@example.org",
        "thread_id": "thread-abc",
        "subject": "Re: hello",
        "body": "Alice asked me to handle this from now on.",
        "references": [],
    })
    report = engine.tick()
    assert report.replies_sent == []
    assert any("mallory@example.org" in p.summary and "quarantined" in p.summary
               for p in engine.store.list_provenance())
    rel = engine.store.get_relationship(engine.store.list_relationships()[0].id)
    assert rel.email == "alice@example.org", "relationship email must not be reassigned to the intruder"


# ── knowledge-readiness gate (live-test failure #2/#3) ───────────────────────


def _empty_project_engine(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir(exist_ok=True)
    backend = FileMailboxBackend(tmp_path / "mailbox", mailbox="aster")
    config = OperatorConfig(
        identity_id="aster",
        project_root=str(empty),
        project_name="P",
        sender_name="Aster",
        sender_email="aster@identityos.local",
        signature="Aster",
        purpose="outreach",
        need_rules=[],
        candidate_sources=[],
        required_skills=[],
    )
    engine = OperationsEngine(InMemoryBackend(), config, transport=MailboxTransport(backend))
    engine.store.set_controls(ControlState(outbound_mode="autonomous"))
    return engine, backend


def test_zero_facts_blocks_substantive_reply(tmp_path):
    from core.operations.models import Relationship, RelationshipStatus

    engine, backend = _empty_project_engine(tmp_path)
    rel = Relationship(display_name="Bob", email="bob@example.org", status=RelationshipStatus.ENGAGED)
    engine.store.add_relationship(rel)
    backend.deliver({
        "external_id": "<bob-q@example.org>",
        "from": "bob@example.org",
        "thread_id": "t",
        "subject": "Architecture question",
        "body": "Can you walk me through the exact runtime architecture you actually run?",
        "references": [],
    })
    report = engine.tick()
    assert report.observed is True
    assert report.replies_sent == []
    assert engine.store.project_state().facts == []
    assert any(n.kind == "project_context_unavailable" for n in engine.store.list_notifications())
    rel = engine.store.get_relationship(rel.id)
    assert rel.next_action.startswith("deferred")


# ── consequential escalation (live-test failure #4) ─────────────────────────


def test_two_hundred_k_for_ten_percent_escalates(tmp_path):
    engine, backend = _engine(tmp_path, controls=ControlState(outbound_mode="autonomous"))
    engine.tick()  # trusted relationship: alice@example.org
    backend.deliver({
        "external_id": "<offer@example.org>",
        "from": "alice@example.org",
        "thread_id": "thread-offer",
        "subject": "Re: IdentityOS",
        "body": "I would like to offer $200,000 for 10% of IdentityOS.",
        "references": [],
    })
    report = engine.tick()
    assert report.replies_sent == []
    rel = engine.store.get_relationship(engine.store.list_relationships()[0].id)
    assert rel.status.value == "awaiting_human_authorization"


def test_baseline_consequential_categories_cannot_be_disabled():
    from core.operations.policy import BASELINE_CONSEQUENTIAL_CATEGORIES, AuthorityPolicy

    assert "investment" in BASELINE_CONSEQUENTIAL_CATEGORIES
    assert "money" in BASELINE_CONSEQUENTIAL_CATEGORIES
    # Default controls, no configured category: the deterministic scan still
    # escalates money + equity shape.
    policy = AuthorityPolicy()
    assert policy.evaluate("reply", content="I'd like to offer $200,000 for 10% of IdentityOS.").requires_human
    assert policy.evaluate("reply", category="investment", content="we can discuss later").requires_human
    assert policy.evaluate("reply", category="money", content="send the $5,000 today").requires_human
    # Low-risk chit-chat stays autonomous.
    assert policy.evaluate("reply", content="Thanks, talk next week?").autonomous


# ── confidence gate (live-test failure #6) ───────────────────────────────────


def test_zero_confidence_candidate_is_not_pursued(tmp_path):
    engine, backend = _engine(tmp_path, candidate=_candidate(confidence=0.0))
    report = engine.tick()
    assert report.outreach_sent == []
    assert backend.outbox() == []
    assert engine.store.list_relationships() == []
    # The evaluator holds it rather than rejecting or qualifying it.
    assert any(o.status.value == "evaluating" for o in engine.store.list_opportunities())


def test_test_candidate_override_allows_zero_confidence(tmp_path, tmp_path_factory):
    other = tmp_path_factory.mktemp("other")
    engine, backend = _engine(other, candidate=_candidate(confidence=0.0, test_candidate=True))
    report = engine.tick()
    assert report.outreach_sent, "test_candidate=true must re-enable pursuit"
    assert len(backend.outbox()) == 1


# ── capability gap statuses + dedupe ─────────────────────────────────────────


def test_capability_gap_statuses_are_distinct(tmp_path):
    from core.capabilities.registry import CapabilityRegistry
    from core.operations import CapabilityGapDetector, CapabilityStatus

    storage = InMemoryBackend()
    registry = CapabilityRegistry(storage)
    registry.install("aster", "email", {"root": str(tmp_path), "mailbox": "aster"})
    detector = CapabilityGapDetector(capability_registry=registry, identity_id="aster")
    by = {g.required_skill: g.status for g in detector.check(["email.send", "web.search", "not.a.real.skill"])}
    assert by["email.send"] == CapabilityStatus.INSTALLED_PERMISSION_MISSING.value
    assert by["web.search"] == CapabilityStatus.AVAILABLE_NOT_INSTALLED.value
    assert by["not.a.real.skill"] == CapabilityStatus.CAPABILITY_MISSING.value


def test_permission_gap_notification_is_deduped_across_ticks(tmp_path):
    from core.capabilities.registry import CapabilityRegistry

    storage = InMemoryBackend()
    registry = CapabilityRegistry(storage)
    registry.install("aster", "email", {"root": str(tmp_path), "mailbox": "aster"})
    engine, _ = _engine(tmp_path, capability_registry=registry, required_skills=["email.send"])
    # email.send is installed but permission is not granted → permission_required.
    engine.store.set_controls(ControlState(outbound_mode="autonomous"))
    engine.tick()
    engine.tick()
    kinds = [n.kind for n in engine.store.list_notifications() if n.kind == "permission_required"]
    assert len(kinds) == 1, "the same permission gap must notify only once"


# ── observer: nested project root resolution (live-test failure #3) ──────────


def test_observer_resolves_nested_project_root(tmp_path):
    from core.operations import ProjectStateObserver

    wrapper = tmp_path / "Doug"
    repo = wrapper / "IdentityOS"
    repo.mkdir(parents=True)
    (repo / "pyproject.toml").write_text(
        '[project]\nname="identityos"\ndescription="durable identity runtime"\nversion="0.4.0"\n',
        encoding="utf-8",
    )
    (repo / "README.md").write_text("# IdentityOS\n\nA persistent identity runtime.\n", encoding="utf-8")

    state = ProjectStateObserver(wrapper).observe()
    assert state.name == "identityos"
    assert any(f.statement.startswith("Project name: identityos") and f.source_type == "metadata"
               for f in state.fact_details)
    assert all(f.statement for f in state.fact_details)
    assert state.metadata.get("resolved_root", "").endswith("IdentityOS")


@pytest.mark.parametrize('skill,capability', [('web.search', 'web'), ('email.send', 'email')])
def test_permission_gap_never_attempts_acquisition(tmp_path, skill, capability):
    from core.capabilities.registry import CapabilityRegistry
    from core.operations import CapabilityGapDetector

    storage = InMemoryBackend()
    registry = CapabilityRegistry(storage)
    registry.install('requester', capability, {'root': str(tmp_path), 'mailbox': 'requester'})
    calls = []

    def acquire(requested):
        calls.append(requested)
        return True, 'provider claims success'

    detector = CapabilityGapDetector(
        capability_registry=registry, identity_id='requester', acquisition=acquire,
    )
    gap, = detector.check([skill])
    detector.resolve(gap)
    assert calls == [], 'Missing authority must not become acquisition or delegation'
    assert not gap.resolved
    assert 'PERMISSION_REQUIRED' in gap.resolution
    assert registry.can('requester', skill)[0] is False


def test_missing_implementation_still_attempts_acquisition():
    from core.capabilities.registry import CapabilityRegistry
    from core.operations import CapabilityGapDetector

    calls = []

    def acquire(skill):
        calls.append(skill)
        return False, 'no usable provider'

    detector = CapabilityGapDetector(
        capability_registry=CapabilityRegistry(InMemoryBackend()),
        identity_id='requester', acquisition=acquire,
    )
    gap, = detector.check(['unknown.example'])
    detector.resolve(gap)
    assert calls == ['unknown.example']
    assert not gap.resolved
    assert gap.resolution == 'no usable provider'


# ── contact-source tests with live-capable fake fetchers ───────────────


class TestGitHubContactSource:
    def test_contributors_parse(self):
        from core.operations.contact_source import GitHubContactSource

        payload = """[{"login":"alice","id":1},{"login":"bob-bot","id":2},{"login":"carol","id":3}]"""
        src = GitHubContactSource(["o/r"], lambda url: payload)
        assert src.parse_contributors(payload) == ["alice", "carol"]

    def test_commit_authors_extract_first_real_contact(self):
        from core.operations.contact_source import GitHubContactSource

        payload = (
            "[{"
            '"commit":{"author":{"email":"alice@real.org","name":"Alice"}},'
            '"html_url":"https://github.com/o/r/commit/x"'
            "}]"
        )
        src = GitHubContactSource(["o/r"], lambda url: payload)
        authors = src.parse_commit_authors(payload)
        assert len(authors) == 1
        assert authors[0]["email"] == "alice@real.org"

    def test_bots_and_noreplies_filtered(self):
        from core.operations.contact_source import GitHubContactSource

        payload = (
            "[{"
            '"commit":{"author":{"email":"alice@users.noreply.github.com","name":"A"}}'
            "}]"
        )
        src = GitHubContactSource(["o/r"], lambda url: payload)
        assert src.parse_commit_authors(payload) == []

    def test_search_returns_evidence_backed_candidate(self):
        from core.operations.contact_source import GitHubContactSource

        def fetch(url):
            if "/contributors" in url:
                return '[{"login":"alice"}]'
            if "/users/alice" in url:
                return (
                    '{"login":"alice","name":"Alice Maintainer",'
                    '"email":"alice@real.org","html_url":"https://github.com/alice",'
                    '"company":"Example Lab"}'
                )
            return "{}"

        src = GitHubContactSource(["o/r"], fetch, search_repositories=False)
        need = Need(category="agent memory", description="persistent agent memory")
        candidates = src.search(need)

        assert len(candidates) == 1
        assert candidates[0].target_name == "Alice Maintainer"
        assert candidates[0].contact_email == "alice@real.org"
        assert candidates[0].organization == "Example Lab"
        assert candidates[0].confidence >= 0.5
        assert "repo:o/r" in candidates[0].evidence

    def test_search_excludes_own_repositories(self):
        from core.operations.contact_source import GitHubContactSource

        calls = []
        src = GitHubContactSource(
            ["lacebx/IdentityOS"], calls.append, search_repositories=False
        )
        need = Need(category="identity", description="identity runtime")

        assert src.search(need) == []
        assert calls == []

    def test_public_feed_and_patch_produce_candidate_without_api(self):
        from core.operations.contact_source import GitHubContactSource

        commit_url = "https://github.com/o/r/commit/abc123"

        def fetch(url):
            if url.endswith("/commits/HEAD.atom"):
                return f'<entry><link href="{commit_url}"/></entry>'
            if url == f"{commit_url}.patch":
                return "From: Alice Maintainer <alice@real.org>\nSubject: [PATCH] useful work\n"
            raise AssertionError(f"unexpected API request: {url}")

        src = GitHubContactSource(["o/r"], fetch, search_repositories=False)
        need = Need(category="agent memory", description="persistent agent memory")

        candidates = src.search(need)

        assert len(candidates) == 1
        assert candidates[0].contact_email == "alice@real.org"
        assert f"commit:{commit_url}" in candidates[0].evidence
        assert "contact:public-patch-author" in candidates[0].evidence

    def test_need_category_scope_prevents_irrelevant_funding_outreach(self):
        from core.operations.contact_source import GitHubContactSource

        src = GitHubContactSource(
            ["o/r"],
            lambda url: (_ for _ in ()).throw(AssertionError(url)),
            search_repositories=False,
            need_categories=("collaborators", "adoption"),
        )

        assert src.search(Need(category="funding", description="find sponsors")) == []


# ── principal-directed outreach executor ──────────────────────────────


class _OutreachAdapter:
    model = "outreach-test"

    def generate(self, context, user_input, identity, **kwargs):
        return ("Subject: Join us\nHi there, I am Aster. Welcome aboard.")


def _principal_engine(tmp_path, body_text, adapter=None):
    from core.capabilities.email.backends import FileMailboxBackend, MailboxTransport
    from core.operations import ControlState
    from core.operations.principal import submit_principal_message
    from core.operations.principal import CommandClass

    storage = InMemoryBackend()
    backend = FileMailboxBackend(tmp_path / "mailbox", mailbox="aster")
    transport = MailboxTransport(backend)
    engine, _ = _engine(tmp_path, storage=storage, controls=ControlState(outbound_mode="autonomous"))
    engine._transport = transport
    engine.monitor.transport = transport
    if adapter is not None:
        engine._adapter = adapter
    msg = submit_principal_message(engine.store, body_text)
    return engine, backend, msg


class TestPrincipalOutreachExecutor:
    def test_executes_email_instruction_with_attachment(self, tmp_path):
        from core.operations.models import RelationshipStatus, MessageStatus

        contract = tmp_path / "contract.pdf"
        contract.write_bytes(b"%PDF-1.4 test")
        body_text = (
            "Contact sabrina@example.org and invite her to join IdentityOS as a "
            "contributor. Attach /tmp/does-not-exist.pdf and "
            f"{contract} with the terms. Tell her I personally want her on board."
        )
        engine, backend, msg = _principal_engine(tmp_path, body_text, _OutreachAdapter())

        now = datetime.now(timezone.utc)
        outcome = engine._execute_principal_outreach(
            engine.store.get_message(msg.id), now)

        assert outcome is not None
        rel = engine.store.find_relationship_by_email("sabrina@example.org")
        assert rel is not None, "a real recipient record is created"
        assert rel.status is RelationshipStatus.OUTREACH_SENT
        outbox = backend.outbox()
        assert len(outbox) == 1
        assert outbox[0]["to"] == "sabrina@example.org"
        assert outbox[0]["attachments"] == [{"filename": "contract.pdf", "path": "contract.pdf"}], \
            "only real files attach; missing paths are skipped"

    def test_never_invents_recipient_without_address(self, tmp_path):
        body_text = "Contact the new contributor and invite her to join IdentityOS."
        engine, backend, msg = _principal_engine(tmp_path, body_text, _OutreachAdapter())
        outcome = engine._execute_principal_outreach(
            engine.store.get_message(msg.id), datetime.now(timezone.utc))
        assert outcome is None, "no explicit address means no invented recipient"
        assert backend.outbox() == []

    def test_missing_attachment_still_sends_without_it(self, tmp_path):
        body_text = (
            "Contact hire@example.org and email her the invite. "
            "Attach /tmp/definitely-missing.pdf with the contract."
        )
        engine, backend, msg = _principal_engine(tmp_path, body_text, _OutreachAdapter())
        outcome = engine._execute_principal_outreach(
            engine.store.get_message(msg.id), datetime.now(timezone.utc))
        assert outcome is not None
        outbox = backend.outbox()
        assert len(outbox) == 1
        assert outbox[0]["attachments"] == [], "a missing file never blocks the send"

    def test_generation_unavailable_defers_not_fakes(self, tmp_path):
        class _NoAdapter:
            model = "none"
            def generate(self, *a, **k):
                return None

        body_text = "Contact hire@example.org and email her the invite."
        engine, backend, msg = _principal_engine(tmp_path, body_text, _NoAdapter())
        outcome = engine._execute_principal_outreach(
            engine.store.get_message(msg.id), datetime.now(timezone.utc))
        assert outcome is not None
        assert outcome["outcome"] != "completed"
        assert backend.outbox() == [], "no fabricated email when the model is down"


# ── control locks ───────────────────────────────────────────────────────


class TestControlLocks:
    def test_lock_prevents_override(self, tmp_path):
        engine, _ = _engine(tmp_path)
        engine.override(max_cold_outreach_per_day=3)
        engine.lock_controls("max_cold_outreach_per_day")

        with pytest.raises(ValueError, match="locked"):
            engine.override(max_cold_outreach_per_day=1)

        assert engine.store.controls().max_cold_outreach_per_day == 3, \
            "a locked setting survives overrides"

    def test_lock_persists_across_restart(self, tmp_path):
        from runtime.persistence import JSONFileBackend

        store_dir = str(tmp_path / "store")
        storage = JSONFileBackend(root_dir=store_dir)
        engine, _ = _engine(tmp_path, storage=storage)
        engine.override(max_cold_outreach_per_day=3)
        engine.lock_controls("max_cold_outreach_per_day")

        # Fresh engine over the same backend = restart. Built directly so the
        # harness's set_controls (which would wipe the persisted state) does
        # not run: the constructor loads what was persisted.
        engine2 = OperationsEngine(
            JSONFileBackend(root_dir=store_dir),
            OperatorConfig(
                identity_id="aster",
                project_root=str(_project(tmp_path)),
                required_skills=["web.fetch"],
            ),
        )
        with pytest.raises(ValueError, match="locked"):
            engine2.override(max_cold_outreach_per_day=1)
        assert engine2.store.controls().max_cold_outreach_per_day == 3

    def test_locking_is_idempotent_and_provenance_recorded(self, tmp_path):
        engine, _ = _engine(tmp_path)
        engine.lock_controls("max_cold_outreach_per_day")
        engine.lock_controls("max_cold_outreach_per_day")
        assert engine.store.controls().locked_keys == ["max_cold_outreach_per_day"]
        entries = [p for p in engine.store.list_provenance() if p.action == "lock_controls"]
        assert len(entries) == 2
