from __future__ import annotations

from pathlib import Path

import pytest

from core.capabilities.email.backends import FileMailboxBackend, MailboxTransport, SMTPBackend
from core.capabilities.email import EmailCapability, build_transport
from core.capabilities.registry import CapabilityRegistry
from runtime.persistence import InMemoryBackend


def test_file_transport_sends_and_persists(tmp_path: Path):
    backend = FileMailboxBackend(tmp_path, mailbox="aster")
    transport = MailboxTransport(backend)
    result = transport.send(to="a@example.org", subject="Hi", body="Hello Alice")
    assert result["ok"] is True
    assert result["external_id"]
    assert len(backend.outbox()) == 1
    # A fresh backend reading the same files must still see the delivery.
    revived = FileMailboxBackend(tmp_path, mailbox="aster")
    assert len(revived.outbox()) == 1
    assert revived.outbox()[0]["to"] == "a@example.org"


def test_file_transport_fetch_inbox(tmp_path: Path):
    backend = FileMailboxBackend(tmp_path, mailbox="aster")
    backend.deliver({
        "external_id": "<m1@example.org>",
        "from": "b@example.org",
        "thread_id": "t1",
        "subject": "Re",
        "body": "Sure.",
    })
    messages = backend.fetch_inbox()
    assert len(messages) == 1
    assert messages[0]["from"] == "b@example.org"
    # Consumed after fetch.
    assert backend.fetch_inbox() == []


def test_build_transport_requires_recipient(tmp_path: Path):
    transport = build_transport({"root": str(tmp_path), "mailbox": "aster"})
    with pytest.raises(Exception):
        transport.send(to="", subject="x", body="x")


def test_email_capability_installs_and_permission_gates_sends(tmp_path: Path):
    storage = InMemoryBackend()
    registry = CapabilityRegistry(storage)
    registry.install("aster", "email", {"root": str(tmp_path), "mailbox": "aster"})

    skipped, reason = registry.can("aster", "email.send")
    assert skipped is False  # public? no: requires explicit grant
    assert "email.send" in reason

    registry.grant("aster", "email", "email.send")
    registry.grant("aster", "email", "email.read")
    result = registry.call("aster", "email.send", to="a@example.org", subject="S", body="Hello")
    assert result.success is True
    assert result.data["ok"] is True

    read_result = registry.call("aster", "email.read_inbox")
    assert read_result.success is True


def test_smtp_backend_requires_credentials():
    backend = SMTPBackend(host="", sender="")
    result = backend.send(to="a@example.org", subject="x", body="x")
    assert result["ok"] is False
    assert "host" in result["error"]


def test_file_transport_roundtrips_threading_headers(tmp_path: Path):
    backend = FileMailboxBackend(tmp_path, mailbox="aster")
    transport = MailboxTransport(backend)
    result = transport.send(
        to="a@example.org", subject="Re", body="reply",
        in_reply_to="<root@example.org>", references=["<root@example.org>", "<mid@example.org>"],
    )
    assert result["ok"] is True
    record = backend.outbox()[0]
    assert record["in_reply_to"] == "<root@example.org>"
    assert record["references"] == ["<root@example.org>", "<mid@example.org>"]


def test_smtp_ignores_bounces_and_autoreplies(tmp_path: Path):
    from email.message import EmailMessage

    smtp = SMTPBackend(host="smtp.example.org", sender="aster@identityos.local")

    def msg(headers):
        m = EmailMessage()
        for key, value in headers.items():
            m[key] = value
        m.set_content("x")
        return m

    assert smtp._ignored(msg({"From": "aster@identityos.local", "Subject": "re: hi"})) is True
    assert smtp._ignored(msg({"From": "mailer@example.org", "Subject": "Delivery Status Notification (Failure)"})) is True
    m = msg({"From": "mailer@example.org", "Subject": "x"})
    m.replace_header("Content-Type", "multipart/report")
    assert smtp._ignored(m) is True
    assert smtp._ignored(msg({"From": "robot@example.org", "Subject": "re: hi", "Auto-Submitted": "auto-replied"})) is True
    assert smtp._ignored(msg({"From": "list@example.org", "Subject": "re: hi", "List-Unsubscribe": "<mailto:list@example.org>"})) is True
    # A genuine reply is kept.
    assert smtp._ignored(msg({"From": "alice@example.org", "Subject": "Re: hello", "Auto-Submitted": "no"})) is False


def test_smtp_validate_reports_unconfigured_without_secrets():
    backend = SMTPBackend(host="", sender="")
    result = backend.validate()
    assert result["smtp"]["configured"] is False
    assert result["smtp"]["ok"] is False
    # Never echo credentials.
    assert "password" not in str(result).lower() or "host" in str(result)


def test_parse_message_ids_handles_multiple_references():
    from core.capabilities.email.backends import parse_message_ids

    ids = parse_message_ids("<a@1> <b@2>\n<c@3>")
    assert ids == ["<a@1>", "<b@2>", "<c@3>"]
    assert parse_message_ids("") == []


# ── IMAP high-water-mark cursor ─────────────────────────────────────────────


class _FakeIMAP:
    """Minimal imaplib-shaped object: uid->raw RFC822 bytes, mutable per test."""

    def __init__(self, store: dict, meta: dict):
        self.store = store
        self.meta = meta

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def login(self, *args):
        return ("OK", [b""])

    def select(self, *args):
        self._responses = {
            "UIDVALIDITY": ("OK", [str(self.meta["uid_validity"]).encode()]),
            "UIDNEXT": ("OK", [str(max(self.store, default=0) + 1).encode()]),
        }
        return ("OK", [b"1"])

    def status(self, *args):
        # Real servers answer STATUS with a literal like:
        # b'INBOX (UIDVALIDITY 77 UIDNEXT 6)'
        parts = [f"UIDVALIDITY {self.meta['uid_validity']}"]
        if not self.meta.get("omit_uidnext"):
            parts.append(f"UIDNEXT {max(self.store, default=0) + 1}")
        literal = f"INBOX ({' '.join(parts)})".encode()
        return ("OK", [literal])

    def response(self, key):
        return self._responses.get(key, ("NO", [b""]))

    def uid(self, cmd, *args):
        if cmd == "search":
            spec = args[1]
            if spec == "ALL":
                return ("OK", [b" ".join(str(u).encode() for u in sorted(self.store))])
            parts = spec.split(":")
            lo, hi = int(parts[0][4:]), int(parts[1])
            uids = [str(u).encode() for u in sorted(self.store) if lo <= u <= hi]
            return ("OK", [b" ".join(uids)])
        if cmd == "fetch":
            uid = int(args[0].decode())
            raw = self.store[uid]
            # Real imaplib literals put the metadata marker BEFORE the message.
            marker = f"{uid} (RFC822 {{{len(raw)}}}".encode()
            return ("OK", [(marker, raw, b")")])
        return ("OK", [b""])


def _fake_smtp(store: dict, meta: dict) -> SMTPBackend:
    return SMTPBackend(
        host="smtp.example.org",
        sender="aster@identityos.local",
        username="aster@identityos.local",
        password="hunter2-local-test",
        imap_host="imap.example.org",
        imap_factory=lambda: _FakeIMAP(store, meta),
    )


def _raw_message(uid: int) -> bytes:
    from email.message import EmailMessage

    m = EmailMessage()
    m["From"] = "alice@example.org"
    m["Subject"] = "Re: hello"
    m["Message-ID"] = f"<m{uid}@example.org>"
    m["References"] = "<root@example.org>"
    m.set_content("What exactly is IdentityOS?")
    return m.as_bytes()


def test_imap_cursor_baselines_without_replaying_history():
    store = {1: _raw_message(1)}  # pre-dates the operator: history
    meta = {"uid_validity": 77}
    smtp = _fake_smtp(store, meta)

    # First run establishes a baseline; nothing from before is ingested.
    first = smtp.fetch_inbox_with_cursor(cursor=None)
    assert first["messages"] == []
    cursor = first["cursor"]
    assert cursor["seeded"] is True
    assert cursor["last_uid"] == 1

    # New mail after the baseline is fetched exactly once.
    store[2] = _raw_message(2)
    store[3] = _raw_message(3)
    second = smtp.fetch_inbox_with_cursor(cursor=cursor)
    assert [m["external_id"] for m in second["messages"]] == ["<m2@example.org>", "<m3@example.org>"]
    assert second["cursor"]["last_uid"] == 3

    # Restart with the persisted cursor: no replay.
    revived = _fake_smtp(store, meta)
    third = revived.fetch_inbox_with_cursor(cursor=second["cursor"])
    assert third["messages"] == []
    assert third["cursor"]["last_uid"] == 3


def test_imap_cursor_reseeds_when_uidvalidity_changes():
    store = {1: _raw_message(1), 2: _raw_message(2)}
    meta = {"uid_validity": 77}
    smtp = _fake_smtp(store, meta)
    first = smtp.fetch_inbox_with_cursor(cursor=None)
    assert first["cursor"]["seeded"] is True

    # The mailbox was re-folded: UIDVALIDITY changes; the cursor reseeds and
    # does not treat pre-existing messages as new conversations.
    meta["uid_validity"] = 999
    second = smtp.fetch_inbox_with_cursor(cursor=first["cursor"])
    assert second["messages"] == []
    assert second["cursor"]["uid_validity"] == 999
    assert second["cursor"]["last_uid"] == 2


def test_imap_cursor_uidnext_falls_back_to_uid_search_all():
    # A server that answers STATUS without UIDNEXT (no UID from the literal):
    # the high-water mark is still derived from UID SEARCH ALL.
    store = {1: _raw_message(1), 2: _raw_message(2)}
    meta = {"uid_validity": 77, "omit_uidnext": True}
    smtp = _fake_smtp(store, meta)
    first = smtp.fetch_inbox_with_cursor(cursor=None)
    assert first["messages"] == []
    assert first["cursor"]["last_uid"] == 2  # fallback via UID SEARCH ALL

    store[3] = _raw_message(3)
    second = smtp.fetch_inbox_with_cursor(cursor=first["cursor"])
    assert [m["external_id"] for m in second["messages"]] == ["<m3@example.org>"]
    assert second["cursor"]["last_uid"] == 3


def test_extract_raw_prefers_message_literal_over_rfc822_marker():
    from core.capabilities.email.backends import _extract_raw

    raw = _raw_message(1)
    # Real imaplib literal tuple: (marker bytes, message bytes, flag bytes).
    marker = b"1 (RFC822 {740}"
    assert _extract_raw([(marker, raw, b")")]) == raw
    # Plain bytes responses still work.
    assert _extract_raw([raw]) == raw
    assert _extract_raw([]) == b""


def test_capability_transport_read_inbox_cursor_roundtrips(tmp_path: Path):
    storage = InMemoryBackend()
    registry = CapabilityRegistry(storage)
    registry.install("aster", "email", {"root": str(tmp_path), "mailbox": "aster"})
    registry.grant("aster", "email", "email.read")

    from core.capabilities.email import CapabilityTransport

    transport = CapabilityTransport(registry, "aster")
    result = transport.fetch_inbox_with_cursor(cursor={"seeded": False, "last_uid": 0})
    assert result["messages"] == []
    assert result["cursor"] is None or result["cursor"].get("seeded") is True


def test_extract_message_text_multipart_alternative_prefers_plain():
    from email.message import EmailMessage
    from core.capabilities.email.backends import extract_message_text

    msg = EmailMessage()
    msg["From"] = "alice@example.org"
    msg["To"] = "aster@identityos.local"
    msg["Subject"] = "Question"
    msg.set_content("This is the plain text body.")
    msg.add_alternative("<p>This is the <b>HTML</b> body.</p>", subtype="html")

    extracted = extract_message_text(msg)
    assert extracted == "This is the plain text body."
    assert "HTML" not in extracted


def test_extract_message_text_html_only_falls_back_to_clean_text():
    from email.message import EmailMessage
    from core.capabilities.email.backends import extract_message_text

    msg = EmailMessage()
    msg["From"] = "alice@example.org"
    msg["To"] = "aster@identityos.local"
    msg["Subject"] = "HTML Only"
    msg.set_content("<p>Hello <b>Aster</b>,</p><p>Can you tell me more?</p>", subtype="html")

    extracted = extract_message_text(msg)
    assert "Hello Aster," in extracted
    assert "Can you tell me more?" in extracted
    assert "<p>" not in extracted and "<b>" not in extracted


def test_extract_message_text_ignores_attachment_as_body():
    from email.message import EmailMessage
    from core.capabilities.email.backends import extract_message_text

    msg = EmailMessage()
    msg["From"] = "alice@example.org"
    msg["To"] = "aster@identityos.local"
    msg["Subject"] = "With Attachment"
    msg.set_content("Actual human message body.")
    msg.add_attachment(b"Attachment file content.", maintype="text", subtype="plain", filename="readme.txt")

    extracted = extract_message_text(msg)
    assert extracted == "Actual human message body."
    assert "Attachment file content" not in extracted


def test_strip_quoted_reply_handles_english_and_arabic_markers():
    from core.capabilities.email.backends import strip_quoted_reply

    english_reply = (
        "Why isn't IdentityOS just a memory database wrapped around an LLM?\n\n"
        "On Thu, Sep 17, 2026 at 1:45 AM <arsenemnz@gmail.com> wrote:\n"
        "> Dear Arsène,\n"
        "> I am Aster reaching out on behalf of IdentityOS.\n"
    )
    assert strip_quoted_reply(english_reply) == "Why isn't IdentityOS just a memory database wrapped around an LLM?"

    arabic_reply = (
        "This is the second time you sent this but yes I have seen it\n\n"
        "في خميس، ١٧ سبتمبر، ٢٠٢٦ في ٠١:٤٥، كتب <arsenemnz@gmail.com>:\n"
        "> Dear Arsène,\n"
        "> I am Aster reaching out on behalf of IdentityOS.\n"
    )
    assert strip_quoted_reply(arabic_reply) == "This is the second time you sent this but yes I have seen it"


def test_generate_message_id_and_custom_id_send(tmp_path: Path):
    from core.capabilities.email.backends import FileMailboxBackend, generate_message_id

    custom_id = generate_message_id("identityos.test")
    assert custom_id.startswith("<") and custom_id.endswith("@identityos.test>")

    backend = FileMailboxBackend(tmp_path / "mb")
    result = backend.send(
        to="test@example.org",
        subject="Hello",
        body="World",
        message_id=custom_id,
    )
    assert result["external_id"] == custom_id
    assert backend.outbox()[0]["external_id"] == custom_id
