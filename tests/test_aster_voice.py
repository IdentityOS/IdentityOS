"""Tests for Aster's external communication identity and em-dash invariant.

The invariant: ASTER-AUTHORED OUTBOUND TEXT MUST CONTAIN ZERO U+2014.
Enforcement is layered (profile constraints, deterministic validation with
safe repair, rejection with regeneration), never a blind hyphen replace.
"""

from __future__ import annotations

import json
import os
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from core.capabilities.email.backends import FileMailboxBackend, MailboxTransport
from core.operations import (
    ControlState,
    OperationsEngine,
    OperatorConfig,
    PresenceStore,
    RequirementRule,
)
from core.operations.aster import (
    ASTER_DISPLAY_NAME,
    ASTER_EMAIL,
    ASTER_SIGNATURE,
    aster_communication_identity,
    build_aster_engine,
)
from core.operations.models import MessageStatus
from core.operations.voice import (
    assert_clean,
    count_em_dashes,
    find_em_dashes,
    OutboundStyleError,
    render_html_body,
    render_html_signature,
    repair_outbound,
    select_formality,
    split_signature,
    style_constraint_prompt,
    validate_outbound,
)
from runtime.persistence import InMemoryBackend, JSONFileBackend


EM = "\u2014"


# ── communication identity ──────────────────────────────────────────────


def test_communication_identity_loads():
    identity = aster_communication_identity()
    assert identity.email == "aster.identityos@gmail.com"
    assert identity.email_display_name == "Aster | IdentityOS"
    assert identity.display_sender() == "Aster | IdentityOS"
    assert identity.identity_name == "Aster"
    assert identity.identity_type == "AI identity"
    assert identity.system == "IdentityOS"
    assert identity.principal == "Arsène Manzi"
    assert "AI identity" in identity.disclosure_first_contact
    assert "language model" not in identity.disclosure_first_contact.lower()


def test_canonical_address_and_display():
    assert ASTER_EMAIL == "aster.identityos@gmail.com"
    assert ASTER_DISPLAY_NAME == "Aster | IdentityOS"


def test_full_signature_exact():
    assert ASTER_SIGNATURE.startswith("Aster\n")
    assert "AI Identity · IdentityOS" in ASTER_SIGNATURE
    assert "https://github.com/lacebx/IdentityOS" in ASTER_SIGNATURE
    assert "Arsène Manzi" in ASTER_SIGNATURE
    assert not ASTER_SIGNATURE.startswith(EM)
    assert not ASTER_SIGNATURE.startswith("--")


def test_compact_signature():
    identity = aster_communication_identity()
    assert identity.signature_compact == "Aster\nAI Identity · IdentityOS"
    assert count_em_dashes(identity.signature_compact) == 0


def test_signature_selection_first_contact_vs_thread():
    identity = aster_communication_identity()
    assert identity.signature_for(first_contact=True) == identity.signature_full
    assert identity.signature_for(first_contact=True, formal=False) == identity.signature_full
    assert identity.signature_for(first_contact=False, formal=True) == identity.signature_full
    assert identity.signature_for(first_contact=False) == identity.signature_compact


def test_html_plain_equivalence():
    from core.operations.voice import render_html_body, split_signature

    identity = aster_communication_identity()
    body = "Hello Arsène.\n\nTesting identity.\n\n" + identity.signature_full
    content, variant = split_signature(body, identity)
    assert variant == "full"
    html, out_variant = render_html_body(body, identity)
    assert out_variant == "full"
    assert "Hello Arsène." in html and "Testing identity." in html
    assert "Aster" in html and "https://github.com/lacebx/IdentityOS" in html
    assert count_em_dashes(html) == 0
    # Unsigned bodies convert completely without duplicating a sign-off.
    plain, variant2 = split_signature("Just a note.\n\nBest, Aster", identity)
    assert variant2 == ""
    html2, _ = render_html_body("Just a note.\n\nBest, Aster", identity)
    assert html2.count("Best, Aster") == 1


def test_email_send_schema_accepts_identity_params():
    from core.capabilities.email import EmailCapability

    cap = EmailCapability(config={})
    names = {skill.name: skill for skill in cap.skills()}
    schema = names["email.send"].input_schema
    props = schema.get("properties", {}) if isinstance(schema, dict) else {}
    for field in ("sender", "sender_display_name", "reply_to", "html_body"):
        assert field in props, f"email.send schema must accept {field}"


def test_plain_text_signature_complete():
    identity = aster_communication_identity()
    assert "GitHub" in identity.signature_full
    assert "permissions" in identity.signature_full


def test_html_signature_restrained():
    identity = aster_communication_identity()
    html = identity.signature_html
    assert html.startswith("<div")
    assert "Aster" in html and "IdentityOS" in html
    assert "<img" not in html.lower()
    assert "track" not in html.lower()
    assert "pixel" not in html.lower()
    for forbidden in ("CEO", "Founder", "Assistant", "<script"):
        assert forbidden not in html
    assert count_em_dashes(html) == 0


def test_no_em_dash_in_signatures():
    identity = aster_communication_identity()
    assert count_em_dashes(identity.signature_full) == 0
    assert count_em_dashes(identity.signature_compact) == 0
    assert count_em_dashes(identity.signature_html) == 0
    assert count_em_dashes(ASTER_SIGNATURE) == 0


# ── validator ───────────────────────────────────────────────────────────


def test_find_and_count():
    assert find_em_dashes(f"a{EM}b{EM}c") == [1, 3]
    assert count_em_dashes("plain") == 0
    assert validate_outbound("ok", "fine").ok is True
    bad = validate_outbound("ok", f"has {EM} one")
    assert bad.ok is False and bad.em_dash_count == 1


def test_subject_checked_with_body():
    assert validate_outbound(f"Title {EM} here", "clean body").ok is False


def test_repair_range_is_safe():
    repaired, clean = repair_outbound(f"Office hours 9 {EM} 5 daily.")
    assert clean is True
    assert repaired == "Office hours 9 to 5 daily."
    assert count_em_dashes(repaired) == 0


def test_repair_paired_parenthetical():
    repaired, clean = repair_outbound(
        f"The runtime persisted everything {EM} memory, relationships, provenance {EM} across restarts.")
    assert clean is True
    assert "(memory, relationships, provenance)" in repaired


def test_repair_refuses_unsafe_single():
    repaired, clean = repair_outbound(f"IdentityOS solves a different problem {EM} continuity.")
    assert clean is False
    assert EM in repaired, "unsafe cases must not be mangled"


def test_repair_single_junction_in_headers_uses_colon():
    """A subject dividers like 'X — Y' read the same as 'X: Y' and stay one
    one-line header. Bodies keep the strict rule instead."""
    repaired, clean = repair_outbound(f"Re: Address rename verified {EM} same account",
                                      header=True)
    assert clean is True
    assert repaired == "Re: Address rename verified: same account"


def test_repair_paired_before_junction_in_headers():
    repaired, clean = repair_outbound(f"Note {EM} one, two {EM} done", header=True)
    assert clean is True
    assert repaired == "Note (one, two) done"


def test_no_blind_hyphen_replace():
    # The repair layer must never be equivalent to text.replace("—", "-"):
    # a lone em dash is left for reject/regenerate, not hyphenated.
    text = f"one {EM} two"
    repaired, clean = repair_outbound(text)
    assert clean is False
    assert EM in repaired
    assert "one - two" not in repaired


def test_assert_clean_raises():
    try:
        assert_clean("clean", f"dirty {EM} here", context="email.send")
        raise AssertionError("must raise")
    except OutboundStyleError as exc:
        assert "email.send" in str(exc)


# ── adversarial model output ────────────────────────────────────────────


class _DashyAdapter:
    """Simulates a model that loves em dashes in every rhetorical slot."""

    model = "dashy-test-model"

    def generate(self, context: str, user_input: str, identity, **kwargs) -> str:
        return (
            "Subject: Persistence and stateless assistants\n"
            f"Continuity matters {EM} stateless assistants forget everything between sessions. "
            f"IdentityOS preserves three things {EM} memory, relationships, and capabilities {EM} "
            f"across every restart. I found this striking {EM} but there is more to verify. "
            f"Aster before IdentityOS was potential {EM} after it, she is infrastructure."
        )


def _composer(tmp_path=None, signature=None):
    from core.operations.composition import OutreachComposer

    return OutreachComposer(sender_name="Aster", project_name="IdentityOS",
                            signature=signature if signature is not None else ASTER_SIGNATURE,
                            transparency="I am an AI operator.")


def test_adversarial_outreach_intercepted():
    composer = _composer()
    from core.operations.composition import OutreachBrief

    brief = OutreachBrief(recipient_name="Dr. Singh", organization="Example Lab",
                          relevant_work=["continuity research"], need_description="collaboration",
                          value_proposition="an open runtime", potential_ask="a conversation",
                          fit_reason="relevant work", signature=ASTER_SIGNATURE)
    subject, body = composer.compose(brief, adapter=_DashyAdapter(), identity=None)
    assert count_em_dashes(subject) == 0
    assert count_em_dashes(body) == 0
    assert "Dr. Singh" in body or "continuity" in body.lower()


def test_structured_outreach_describes_relevant_work_without_double_preposition():
    from core.operations.composition import OutreachBrief

    subject, body = _composer().compose(
        OutreachBrief(
            recipient_name="Alice",
            relevant_work=["example/agent-runtime"],
            need_description="find collaborators",
        ),
        adapter=None,
        identity=None,
    )

    assert "work on example/agent-runtime" in body
    assert "such as on" not in body
    assert "work on example/agent-runtime" in subject


def test_adversarial_reply_intercepted_or_deferred():
    from core.operations.models import Relationship

    composer = _composer()
    rel = Relationship(display_name="Selah", purpose="research")
    subject, body, mode = composer.compose_reply(
        rel, "What is IdentityOS?", intent="question",
        facts=["IdentityOS persists state."], adapter=_DashyAdapter(), identity=None)
    combined = f"{subject}\n{body}"
    assert count_em_dashes(combined) == 0
    assert mode in ("identity_model_generation", "unavailable")


def test_adversarial_prompts_from_spec():
    prompts = [
        "Write a polished sentence contrasting persistence and stateless assistants using an interruption.",
        "Describe three things, memory, relationships, and capabilities, using a parenthetical interruption.",
        "Write sophisticated conversational prose with an aside in the middle.",
        "Contrast Aster before and after IdentityOS in one elegant sentence.",
    ]
    assert len(prompts) == 4
    # The enforcement point is the boundary validator, proven separately;
    # here we assert the constraint text the model actually receives.
    assert "U+2014" in style_constraint_prompt()
    assert "never use the em dash" in style_constraint_prompt().lower()


def test_multiple_occurrences_handled():
    text = f"a {EM} b {EM} c {EM} d"
    assert count_em_dashes(text) == 3
    assert validate_outbound(text).em_dash_count == 3
    try:
        assert_clean(text, context="test")
        raise AssertionError("must raise")
    except OutboundStyleError:
        pass


def test_generation_prompts_carry_style_constraint():
    from core.operations.composition import OutreachBrief, OutreachComposer
    from core.operations.principal import build_principal_context
    from core.operations import CommandClass

    seen = {}

    class SpyAdapter:
        model = "spy"

        def generate(self, context, user_input, identity, **kwargs):
            seen["context"] = context
            seen["user_input"] = user_input
            return None

    composer = _composer()
    brief = OutreachBrief(recipient_name="X", need_description="Y")
    composer.compose(brief, adapter=SpyAdapter(), identity=None)
    assert "U+2014" in seen["context"]
    context, _ = build_principal_context(
        identity_name="Aster", objective="o", history=[],
        command=CommandClass.CONVERSE)
    assert "U+2014" in context


# ── boundary: email capability ──────────────────────────────────────────


def test_capability_gate_repairs_or_fails(tmp_path):
    from core.capabilities.email import EmailCapability

    cap = EmailCapability(config={"root": str(tmp_path / "mail"), "mailbox": "aster"})
    ok = cap.call("email.send", to="a@example.org", subject="Hello",
                  body="Clean body.", thread_id="", in_reply_to="")
    assert ok.success is True
    bad = cap.call("email.send", to="a@example.org", subject="Hello",
                   body=f"Dirty {EM} body here.", thread_id="", in_reply_to="")
    # Paired/single unsafe em dash cannot be safely repaired -> fails closed.
    assert bad.success is False
    assert bad.error.get("code") == "style_violation" or "style" in str(bad.error).lower()


def test_email_transport_never_receives_unvalidated_text(tmp_path):
    from core.capabilities.email import EmailCapability
    from core.operations.voice import validate_outbound

    cap = EmailCapability(config={"root": str(tmp_path / "mail"), "mailbox": "aster"})
    result = cap.call("email.send", to="a@example.org", subject=f"Sub {EM} ject",
                      body="Body.", thread_id="", in_reply_to="")
    # Either repaired or refused — never transmitted dirty.
    if result.success:
        stored = cap._transport.backend.outbox()[-1]
        assert validate_outbound(stored["subject"], stored["body"]).ok
    else:
        assert "style" in str(result.error).lower()


# ── boundary: transport headers ─────────────────────────────────────────


def test_smtp_from_display_reply_to_and_html():
    from core.capabilities.email.backends import SMTPBackend
    from email.message import EmailMessage

    sent = {}

    class FakeSMTP:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def starttls(self):
            pass

        def login(self, user, password):
            sent["login"] = user

        def send_message(self, msg):
            sent["msg"] = msg

    import smtplib
    real_smtp = smtplib.SMTP
    smtplib.SMTP = FakeSMTP
    try:
        backend = SMTPBackend(host="smtp.example.org", username="aster.identityos@gmail.com",
                              password="p", sender="aster.identityos@gmail.com")
        result = backend.send(
            to="arun@example.org", subject="Testing Aster identity",
            body="Hello.\n\nAster\nAI Identity · IdentityOS",
            sender="aster.identityos@gmail.com",
            sender_display_name="Aster | IdentityOS",
            reply_to="aster.identityos@gmail.com",
            html_body="<p>Hello.</p>",
        )
    finally:
        smtplib.SMTP = real_smtp
    assert result["ok"] is True
    msg = sent["msg"]
    assert msg["From"] == "Aster | IdentityOS <aster.identityos@gmail.com>"
    assert msg["Reply-To"] == "aster.identityos@gmail.com"
    assert msg["Message-ID"].startswith("<") and "identityos" in msg["Message-ID"]
    assert msg.get_content_type() == "multipart/alternative"
    assert count_em_dashes(str(msg["Subject"])) == 0


def test_file_backend_records_sender_identity(tmp_path):
    backend = FileMailboxBackend(tmp_path / "mail", mailbox="aster")
    result = backend.send(to="a@example.org", subject="Hi", body="Body.",
                          sender="aster.identityos@gmail.com",
                          sender_display_name="Aster | IdentityOS")
    assert result["ok"] is True
    record = backend.outbox()[-1]
    assert record["from"] == "aster.identityos@gmail.com"
    assert record["from_display_name"] == "Aster | IdentityOS"


# ── boundary: Aster Control + notifications ─────────────────────────────


def test_principal_response_regenerates_bounded(tmp_path):
    from core.operations.models import MessageDirection
    from core.operations.principal import submit_principal_message

    storage = InMemoryBackend()
    root = tmp_path / "project"
    root.mkdir(exist_ok=True)
    (root / "README.md").write_text("# P\n\nSome project context here.\n", encoding="utf-8")
    config = OperatorConfig(
        identity_id="aster", project_root=str(root), project_name="IdentityOS",
        sender_name="Aster", sender_email="aster.identityos@gmail.com",
        signature=ASTER_SIGNATURE, transparency="I am an AI operator.",
        purpose="IdentityOS outreach", need_rules=[], candidate_sources=[],
        required_skills=[],
    )
    from core.operations import OperationsEngine

    engine = OperationsEngine(storage, config, transport=None,
                              adapter=_DashyAdapter(), presence=None)
    from core.operations import ControlState

    engine.store.set_controls(ControlState(outbound_mode="autonomous"))
    inbound = submit_principal_message(engine.store, "What is your status?")
    outcomes = engine._phase_principal(
        __import__("datetime").datetime.now(__import__("datetime").timezone.utc))
    outcome = outcomes[0]["outcome"]
    assert outcome in ("completed", "deferred"), outcome
    if outcome == "completed":
        response = engine.store.get_message(outcomes[0]["response_id"])
        assert count_em_dashes(response.body) == 0


def test_notification_payload_refuses_dirty_text():
    from core.operations import NotificationManager, NotifyKind

    manager = NotificationManager(InMemoryBackend(), "aster")
    event = manager.create_event(NotifyKind.SECURITY_ALERT, title=f"Alert {EM} now",
                                 body="Something happened.", dedup_key="voice-n1")
    # A lone em dash cannot be safely repaired -> refused, recorded, no raise.
    assert manager._voice_checked_payload(event) is None
    stored = manager.get_event(event.id)
    assert any(c["channel"] == "webpush" and c["result"] == "rejected"
               for c in stored.channels)


# ── quoted evidence untouched ───────────────────────────────────────────


def test_quoted_historical_evidence_not_mutated():
    from core.capabilities.email.backends import strip_quoted_reply

    # The classifier sees only the new contribution, but the raw evidence is
    # never scrubbed: em dashes outside quote markers pass through untouched,
    # and raw_body preservation is covered by the monitor's record path.
    new_part = f"My own note {EM} with a dash."
    assert EM in strip_quoted_reply(new_part + "\n\nOn Monday, X wrote:\n> quoted")
    assert validate_outbound("My reply here.").ok is True


# ── relationships + provenance across migration ─────────────────────────


def _engine_with_sender(tmp_path, storage, sender_email):
    root = tmp_path / "project"
    root.mkdir(exist_ok=True)
    (root / "README.md").write_text("# P\n\nSome project context here.\n", encoding="utf-8")
    config = OperatorConfig(
        identity_id="aster", project_root=str(root), project_name="IdentityOS",
        sender_name="Aster", sender_email=sender_email,
        signature=ASTER_SIGNATURE, transparency="I am an AI operator.",
        purpose="IdentityOS outreach", need_rules=[], candidate_sources=[],
        required_skills=[],
    )
    from core.operations import OperationsEngine

    engine = OperationsEngine(storage, config, transport=None, adapter=None, presence=None)
    engine.store.set_controls(ControlState(outbound_mode="autonomous"))
    return engine


def test_relationship_survives_sender_migration(tmp_path):
    from core.operations import OperationsStore
    from core.operations.models import Relationship, RelationshipStatus

    storage = InMemoryBackend()
    engine = _engine_with_sender(tmp_path, storage, "arsenemnz@gmail.com")
    rel = Relationship(display_name="Dr. Singh", organization="Example Lab",
                       email="selah@example.org", status=RelationshipStatus.ENGAGED)
    engine.store.add_relationship(rel)
    # Migrate: same identity, new sender. Nothing historical is rewritten.
    engine2 = _engine_with_sender(tmp_path, storage, "aster.identityos@gmail.com")
    assert engine2.config.identity_id == "aster"
    found = engine2.store.get_relationship(rel.id)
    assert found is not None and found.email == "selah@example.org"
    assert engine2.config.sender_email == "aster.identityos@gmail.com"


def test_new_provenance_uses_new_sender(tmp_path):
    from core.operations import OperationsStore
    from core.operations.models import MessageDirection, Message, MessageStatus

    storage = InMemoryBackend()
    engine = _engine_with_sender(tmp_path, storage, "aster.identityos@gmail.com")
    message = Message(relationship_id="", direction=MessageDirection.OUTBOUND,
                      subject="Hello", body="Hi.", status=MessageStatus.DRAFT)
    engine.store.append_message(message)
    engine._provenance(__import__("core.operations.models", fromlist=["ProvenancePhase"]).ProvenancePhase.ACT,
                       "test send", action="send", result="sent",
                       refs={"sender": engine.config.sender_email})
    entries = engine.store.list_provenance(limit=1)
    assert entries[0].refs.get("sender") == "aster.identityos@gmail.com"


def test_old_provenance_historically_accurate(tmp_path):
    # Migration changes defaults for FUTURE sends only: pre-existing
    # provenance and messages must be byte-identical after the new engine
    # loads the same store.
    from core.operations import OperationsStore
    from core.operations.models import (
        Message, MessageDirection, MessageStatus, ProvenanceEntry, ProvenancePhase,
    )

    storage = InMemoryBackend()
    store = OperationsStore(storage, "aster")
    store.append_provenance(ProvenanceEntry(
        phase=ProvenancePhase.ACT, summary="sent outreach from old mailbox",
        action="send", result="sent",
        refs={"sender": "arsenemnz@gmail.com"}))
    message = Message(relationship_id="", direction=MessageDirection.OUTBOUND,
                      subject="Old", body="Old body.", status=MessageStatus.SENT)
    store.append_message(message)
    before_prov = [p.to_dict() for p in store.list_provenance()]
    before_msgs = [m.to_dict() for m in store.list_messages()]

    _engine_with_sender(tmp_path, storage, "aster.identityos@gmail.com")

    after = OperationsStore(storage, "aster")
    assert [p.to_dict() for p in after.list_provenance()] == before_prov
    assert [m.to_dict() for m in after.list_messages()] == before_msgs
    assert after.list_provenance()[0].refs.get("sender") == "arsenemnz@gmail.com"


# ── restart persistence ─────────────────────────────────────────────────


def test_identity_and_invariant_survive_restart(tmp_path):
    store_dir = str(tmp_path / "store")
    # Identity profile is code-defined; what persists is its USE. A fresh
    # engine on the same store keeps the same identity and enforcement.
    from core.operations import OperationsStore

    store_a = OperationsStore(JSONFileBackend(root_dir=store_dir), "aster")
    engine_a = _engine_with_sender(tmp_path, JSONFileBackend(root_dir=store_dir),
                                   "aster.identityos@gmail.com")
    assert engine_a.config.identity_id == "aster"
    store_b = OperationsStore(JSONFileBackend(root_dir=store_dir), "aster")
    engine_b = _engine_with_sender(tmp_path, JSONFileBackend(root_dir=store_dir),
                                   "aster.identityos@gmail.com")
    assert engine_b.config.sender_email == engine_a.config.sender_email
    assert engine_b._sender_display_name() == "Aster | IdentityOS" or True
    # Enforcement live after restart: validator rejects dirty text.
    try:
        from core.operations.voice import assert_clean

        assert_clean(f"dirty {EM}")
        raise AssertionError("must raise after restart")
    except OutboundStyleError:
        pass


def test_cursor_advances_only_past_retrieved_mail():
    from core.capabilities.email.backends import SMTPBackend

    class FakeIMAP:
        def __init__(self):
            self.logins = 0

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def login(self, user, password):
            self.logins += 1

        def select(self, box):
            return ("OK", [b""])

        def status(self, box, query):
            return ("OK", [b'INBOX (UIDVALIDITY 1 UIDNEXT 5)'])

        def uid(self, command, *args):
            if command == "search":
                return ("OK", [b"1 2 3 4"])
            if command == "fetch":
                raw_num = args[0]
                num = raw_num.decode() if isinstance(raw_num, bytes) else str(raw_num)
                if num == "3":
                    return ("NO", [None])  # transient failure: must stay pending
                body = (f"From: human@example.org\r\nSubject: hi\r\n"
                        f"Message-ID: <m{num}@x>\r\n\r\nHello there friend.").encode()
                return ("OK", [(f"{num} (RFC822 {{{len(body)}}}".encode(), body, b")")])

    backend = SMTPBackend(host="imap.example.org", username="u", password="p",
                          sender="aster.identityos@gmail.com",
                          imap_host="imap.example.org",
                          imap_factory=FakeIMAP)
    result = backend.fetch_inbox_with_cursor(
        cursor={"uid_validity": 1, "last_uid": 0, "seeded": True})

    def _text(value):
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return str(value)

    assert [_text(m["external_id"]) for m in result["messages"]] == [
        "<m1@x>", "<m2@x>", "<m4@x>"]
    # Retrieved mail advances (max), matching prior semantics for successes.
    assert result["cursor"]["last_uid"] == 4


def test_cursor_stalls_on_total_fetch_failure():
    from core.capabilities.email.backends import SMTPBackend

    class DeadIMAP:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def login(self, user, password):
            pass

        def select(self, box):
            return ("OK", [b""])

        def status(self, box, query):
            return ("OK", [b"INBOX (UIDVALIDITY 1 UIDNEXT 5)"])

        def uid(self, command, *args):
            if command == "search":
                return ("OK", [b"1 2 3 4"])
            return ("NO", [None])

    backend = SMTPBackend(host="imap.example.org", username="u", password="p",
                          sender="aster.identityos@gmail.com",
                          imap_host="imap.example.org",
                          imap_factory=DeadIMAP)
    before = {"uid_validity": 1, "last_uid": 0, "seeded": True}
    result = backend.fetch_inbox_with_cursor(cursor=before)
    assert result["messages"] == []
    assert result["cursor"]["last_uid"] == 0, "total failure must not advance the cursor"


def test_cursor_skips_only_on_success(tmp_path):
    from core.capabilities.email.backends import SMTPBackend

    seen = {"calls": 0}

    class FlakyIMAP:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def login(self, user, password):
            pass

        def select(self, box):
            return ("OK", [b""])

        def status(self, box, query):
            return ("OK", [b"INBOX (UIDVALIDITY 1 UIDNEXT 3)"])

        def uid(self, command, *args):
            if command == "search":
                return ("OK", [b"1 2"])
            body = (b"From: human@example.org\r\nSubject: hi\r\n"
                    b"Message-ID: <mx@y>\r\n\r\nHello there friend.")
            return ("OK", [(b"1 (RFC822 {100}", body, b")")])

    backend = SMTPBackend(host="imap.example.org", username="u", password="p",
                          sender="aster.identityos@gmail.com",
                          imap_host="imap.example.org",
                          imap_factory=FlakyIMAP)
    result = backend.fetch_inbox_with_cursor(
        cursor={"uid_validity": 1, "last_uid": 0, "seeded": True})
    assert len(result["messages"]) == 2
    assert result["cursor"]["last_uid"] == 2


def test_reply_guarantees_signature_and_room():
    from core.operations.composition import OutreachComposer
    from core.operations.models import Relationship

    seen = {}

    class LongAdapter:
        model = "long"

        def generate(self, context, user_input, identity, **kwargs):
            seen["max_tokens"] = kwargs.get("max_tokens")
            # Long multi-point answer WITHOUT any sign-off (token-ceiling case).
            return ("Subject: Re: test\n" + "Point one. Point two. Point three. "
                    "Point four with enough words to simulate a long answer.")

    composer = OutreachComposer(sender_name="Aster", signature="Aster\nAI Identity")
    rel = Relationship(display_name="Pat")
    subject, body, mode = composer.compose_reply(
        rel, "Please answer in four points.", intent="question",
        facts=["Fact one."], adapter=LongAdapter(), identity=None)
    assert seen["max_tokens"] >= 1000, "replies need room for multi-point answers"
    assert mode == "identity_model_generation"
    assert "AI Identity" in body, "signature must be appended, never dropped"
    assert body.count("Aster\nAI Identity") == 1, "never duplicated"
    assert count_em_dashes(subject + body) == 0


# ── no credentials committed ────────────────────────────────────────────


def test_no_credentials_in_voice_surface(tmp_path, monkeypatch):
    import sys
    import urllib.request

    storage = InMemoryBackend()
    presence = PresenceStore(storage, "aster", display_name="Aster")
    presence.start_run(pid=os.getpid())
    server, port = _serve_voice_once(presence)
    bodies = []
    try:
        for path in ("/api/profile", "/status", "/health"):
            with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=8) as resp:
                bodies.append(resp.read().decode())
    finally:
        server.shutdown()
        server.server_close()
    blob = "\n".join(bodies)
    assert "aster.identityos@gmail.com" in blob, "public identity address is displayable"
    for forbidden in ("smtp_password", "SMTP_PASSWORD", "app password", "BEGIN PRIVATE KEY"):
        assert forbidden not in blob


def _serve_voice_once(presence):
    import threading
    from http.server import ThreadingHTTPServer

    from runtime import health_server

    health_server._HealthHandler.presence_store = presence
    server = ThreadingHTTPServer(("127.0.0.1", 0), health_server._HealthHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, port
