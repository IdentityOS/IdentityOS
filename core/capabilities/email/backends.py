"""
core/capabilities/email/backends.py

Mailbox backends for the email capability.

Two backends are provided:

* :class:`FileMailboxBackend` — a deterministic, file-backed mailbox used for
  tests, demos, and offline operation.  It performs *real* filesystem writes,
  so evidence about what was sent is genuine.
* :class:`SMTPBackend` — a real outbound SMTP sender with optional IMAP inbox
  polling, gated behind credentials that are never stored in identity state.

A :class:`MailboxTransport` adapts either backend to the operations engine's
``send`` / ``fetch_inbox`` protocol.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable, Optional


class MailboxError(RuntimeError):
    pass


def parse_message_ids(value: str) -> list[str]:
    """Return the RFC 5322 message-ids (``<...>`` tokens) inside a header value."""
    return list(re.findall(r"<[^<>\s]+>", value or ""))


def _extract_raw(fetched: Any) -> bytes:
    """Pick the message literal bytes out of an imaplib fetch result.

    imaplib returns lists of items shaped either as ``(payload, flags)`` tuples
    (literal responses) or plain byte strings. A literal tuple is
    ``(b'UID (RFC822 {size}', <message bytes>, b')')`` on real servers, so the
    message literal is always the **longest** ``bytes`` item — the metadata
    prefix can otherwise be mistaken for a (headerless) message.
    """
    container = fetched[0] if fetched else None
    candidates: list[bytes] = []
    if isinstance(container, (tuple, list)):
        for item in container:
            if isinstance(item, (bytes, bytearray)):
                candidates.append(bytes(item))
    elif isinstance(container, (bytes, bytearray)):
        candidates.append(bytes(container))
    if not candidates:
        return b""
    return max(candidates, key=len)


def parse_rfc_date(raw: Any) -> Optional[datetime]:
    """Parse an RFC 2822 ``Date`` header into an aware UTC datetime.

    Returns None for absent, malformed, or unparsable values rather than
    raising or guessing. A sender-claimed timestamp must never be invented:
    callers that cannot establish a real time record the fact as unknown.
    """
    import email.utils

    if not raw:
        return None
    try:
        parsed_dt = email.utils.parsedate_to_datetime(str(raw))
    except (TypeError, ValueError):
        return None
    if parsed_dt is None:
        return None
    if parsed_dt.tzinfo is None:
        parsed_dt = parsed_dt.replace(tzinfo=timezone.utc)
    return parsed_dt.astimezone(timezone.utc)


def _header_date(parsed: Any) -> str:
    """RFC 2822 ``Date`` header as an ISO 8601 UTC string ("" when absent).

    ``parsed`` is duck-typed: both the IMAP backend's dict shape and an
    ``email.message.Message`` expose ``.get``.
    """
    try:
        raw = parsed.get("Date")
    except AttributeError:
        return ""
    parsed_dt = parse_rfc_date(raw)
    return parsed_dt.isoformat() if parsed_dt else ""


def gmail_immutable_ids(parsed: Any) -> tuple[Optional[str], Optional[str]]:
    """Return Gmail's immutable ``(X-GM-MSGID, X-GM-THRID)`` hex ids.

    Gmail exposes its internal ids only as X- headers on IMAP fetches, and
    only when ``(X-GM-MSGID X-GM-THRID)`` (or the full header set) is
    requested. They are the stable keys for a conversation and for exactly-once
    dedupe, and are distinct from the RFC Message-ID, which is forgeable and
    which mail clients rewrite. Non-Gmail servers (or a fetch that omitted the
    headers) return ``(None, None)`` and the caller falls back to the RFC id.
    """
    try:
        msgid = parsed.get("X-GM-MSGID") or parsed.get("X-Gmail-Message-Id")
        thread = parsed.get("X-GM-THRID") or parsed.get("X-Gmail-Thread-Id")
    except Exception:
        return None, None
    msgid = str(msgid).strip() if msgid else None
    thread = str(thread).strip() if thread else None
    if msgid is not None and not re.fullmatch(r"[0-9a-fA-F]{8,}", msgid):
        msgid = None
    if thread is not None and not re.fullmatch(r"[0-9a-fA-F]{8,}", thread):
        thread = None
    return msgid, thread


def generate_message_id(domain: str = "identityos") -> str:
    """Return a fresh RFC 5322 ``Message-ID`` header value (``<...@...>``).

    Generated *before* transmission so the exact identifier can be persisted in
    ``Message.external_id`` without depending on the transport echoing it back.
    """
    return f"<{uuid.uuid4().hex}@{domain or 'identityos'}>"


def _part_is_attachment(part: Any) -> bool:
    disposition = str(part.get_content_disposition() or "").lower()
    if disposition == "attachment":
        return True
    if disposition == "inline":
        return bool(part.get_filename())
    return bool(part.get_filename())


def _decode_part_text(part: Any) -> str:
    """Decode one text MIME part to a Unicode string (CTE/charset aware)."""
    try:
        payload = part.get_content()
    except Exception:
        return ""
    if isinstance(payload, bytes):
        payload = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
    return str(payload or "").replace("\r\n", "\n").replace("\r", "\n")


_HTML_BLOCK_TAGS = {
    "p", "div", "br", "li", "tr", "ul", "ol", "blockquote", "section",
    "h1", "h2", "h3", "h4", "h5", "h6", "address", "pre",
}


def _html_to_text(html: str) -> str:
    """Convert HTML to plain text via the stdlib parser (no regex MIME tricks)."""
    if not html:
        return ""

    class _Extractor(HTMLParser):
        def __init__(self) -> None:
            super().__init__(convert_charrefs=True)
            self._parts: list[str] = []
            self._skip = False

        def handle_starttag(self, tag, attrs) -> None:
            tag = str(tag).lower()
            if tag in ("script", "style"):
                self._skip = True
            if tag in _HTML_BLOCK_TAGS:
                self._parts.append("\n")

        def handle_endtag(self, tag) -> None:
            tag = str(tag).lower()
            if tag in ("script", "style"):
                self._skip = False
            if tag in _HTML_BLOCK_TAGS:
                self._parts.append("\n")

        def handle_data(self, data) -> None:
            if not self._skip:
                self._parts.append(data)

    parser = _Extractor()
    parser.feed(html)
    body = "".join(parser._parts)
    body = re.sub(r"[ \t]+", " ", body)
    while "\n\n\n" in body:
        body = body.replace("\n\n\n", "\n\n")
    return body.strip()


def extract_message_text(parsed: Any) -> str:
    """Return the human-readable text of a parsed email message.

    Standards-based extraction: prefers the first non-attachment
    ``text/plain`` part, falls back to non-attachment ``text/html`` (decoded to
    text), and never uses an attachment as the body.  ``multipart/alternative``
    copies are *not* concatenated — exactly one rendering is returned, with
    line endings normalized to ``\\n``.
    """
    if parsed is None:
        return ""
    parts = list(parsed.walk()) if hasattr(parsed, "walk") else [parsed]
    plain_parts: list[str] = []
    html_parts: list[str] = []
    for part in parts:
        if not hasattr(part, "get_content_type"):
            continue
        if part.is_multipart():
            continue
        if _part_is_attachment(part):
            continue
        ctype = str(part.get_content_type() or "").lower()
        if ctype == "text/plain":
            plain_parts.append(_decode_part_text(part))
        elif ctype == "text/html":
            html_parts.append(_decode_part_text(part))
    if plain_parts:
        text = "\n".join(p for p in plain_parts if p)
    elif html_parts:
        text = _html_to_text("\n".join(p for p in html_parts if p))
    else:
        text = ""
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


_QUOTED_MARKER_RE = re.compile(
    r"^\s*(?:"
    r"On\b.*?\bwrote\s*[::]?\s*$"                          # On Thu, Sep 17, 2026 ... wrote:
    r"|في\b.*?\bكتب\b.*[،:]\s*$"                           # في ...، كتب <addr>:
    r"|-----Original Message-----"
    r"|----- ?Forwarded Message ?-----"
    r"|Begin forwarded message"
    r")\s*$",
    re.IGNORECASE,
)


def strip_quoted_reply(text: str) -> str:
    """Return only the *new* contribution of a reply email.

    Removes quoted blocks (lines prefixed with ``>``), and truncates everything
    from the first leading-separator marker (``On ... wrote:``, the Arabic
    ``في ... ، كتب ...:`` form, or a forwarded-message guard).  The caller is
    expected to preserve the unmodified text (e.g. ``raw_body``) for the audit
    trail; the returned new text is what intent/classification run against.
    """
    lines = (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    kept: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            kept.append(line.rstrip())
            continue
        if stripped.startswith(">") or stripped.startswith("|"):
            continue
        if _QUOTED_MARKER_RE.match(stripped):
            break
        kept.append(line.rstrip())
    body = "\n".join(kept).strip()
    while "\n\n\n" in body:
        body = body.replace("\n\n\n", "\n\n")
    return body


class FileMailboxBackend:
    """A real, file-backed mailbox. Deliveries and receipts are persisted JSON."""

    def __init__(self, root: str | Path, *, mailbox: str = "default") -> None:
        self.root = Path(root)
        self.mailbox = mailbox
        self.root.mkdir(parents=True, exist_ok=True)

    @property
    def _outbox(self) -> Path:
        return self.root / f"outbox-{self.mailbox}.json"

    @property
    def _inbox(self) -> Path:
        return self.root / f"inbox-{self.mailbox}.json"

    def _read(self, path: Path) -> list[dict[str, Any]]:
        if not path.is_file():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        return data if isinstance(data, list) else []

    def _write(self, path: Path, items: list[dict[str, Any]]) -> None:
        path.write_text(json.dumps(items, indent=2), encoding="utf-8")

    def send(
        self,
        *,
        to: str,
        subject: str,
        body: str,
        thread_id: str = "",
        in_reply_to: str = "",
        references: Optional[Iterable[str]] = None,
        message_id: str = "",
        sender: str = "",
        sender_display_name: str = "",
        reply_to: str = "",
        html_body: str = "",
    ) -> dict[str, Any]:
        if not to:
            raise MailboxError("recipient address is required")
        message_id = message_id or generate_message_id("identityos.local")
        record = {
            "external_id": message_id,
            "thread_id": thread_id or f"thread-{uuid.uuid4().hex[:10]}",
            "in_reply_to": in_reply_to,
            "references": list(references or []),
            "to": to,
            "from": sender,
            "from_display_name": sender_display_name,
            "reply_to": reply_to,
            "subject": subject,
            "body": body,
            "html_body": html_body,
            "sent_at": time.time(),
        }
        items = self._read(self._outbox)
        items.append(record)
        self._write(self._outbox, items)
        return {
            "ok": True,
            "external_id": message_id,
            "thread_id": record["thread_id"],
            "evidence": [f"file-mailbox:{self._outbox}", f"to:{to}", f"subject:{subject}"],
        }

    def fetch_inbox(self) -> list[dict[str, Any]]:
        items = self._read(self._inbox)
        # Inbound messages are considered consumed once fetched.
        self._write(self._inbox, [])
        return items

    def outbox(self) -> list[dict[str, Any]]:
        return self._read(self._outbox)

    def deliver(self, message: dict[str, Any]) -> None:
        """Test/demo helper: place a message in the inbox."""
        items = self._read(self._inbox)
        items.append(message)
        self._write(self._inbox, items)


class SMTPBackend:
    """Real SMTP/IMAP using the standard library.

    Credentials come from the environment (or capability config) and are never
    persisted.  ``fetch_inbox`` skips automated/bounce messages so the operator
    does not mistake an autoresponder for a human reply.
    """

    def __init__(
        self,
        *,
        host: str,
        port: int = 587,
        username: str = "",
        password: str = "",
        sender: str = "",
        use_tls: bool = True,
        imap_host: str = "",
        imap_port: int = 993,
        imap_factory: Any = None,
    ) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.sender = sender or username
        self.use_tls = use_tls
        self.imap_host = imap_host
        self.imap_port = imap_port
        self.imap_factory = imap_factory

    def send(
        self,
        *,
        to: str,
        subject: str,
        body: str,
        thread_id: str = "",
        in_reply_to: str = "",
        references: Optional[Iterable[str]] = None,
        message_id: str = "",
        sender: str = "",
        sender_display_name: str = "",
        reply_to: str = "",
        html_body: str = "",
    ) -> dict[str, Any]:
        if not self.host or not self.sender:
            return {"ok": False, "error": "SMTP host and sender are required"}
        import smtplib
        from email.message import EmailMessage
        from email.utils import formataddr

        message_id = message_id or generate_message_id("identityos")
        envelope_from = sender or self.sender
        msg = EmailMessage()
        display = sender_display_name or ""
        msg["From"] = formataddr((display, envelope_from)) if display else envelope_from
        if reply_to:
            msg["Reply-To"] = reply_to
        msg["To"] = to
        msg["Subject"] = subject
        msg["Message-ID"] = message_id
        refs = [r for r in (references or []) if r]
        if in_reply_to and in_reply_to not in refs:
            refs.insert(0, in_reply_to)
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
        if refs:
            msg["References"] = " ".join(refs)
        msg.set_content(body)
        if html_body:
            msg.add_alternative(html_body, subtype="html")

        try:
            with smtplib.SMTP(self.host, self.port, timeout=20) as smtp:
                if self.use_tls:
                    smtp.starttls()
                if self.username:
                    smtp.login(self.username, self.password)
                smtp.send_message(msg)
        except Exception as exc:  # surface the real failure
            raise MailboxError(f"SMTP send failed: {exc}") from exc

        resolved_thread = thread_id or in_reply_to or (refs[0] if refs else message_id)
        return {
            "ok": True,
            "external_id": message_id,
            "thread_id": resolved_thread,
            "evidence": [f"smtp:{self.host}:{self.port}", f"to:{to}", f"subject:{subject}"],
        }

    def fetch_inbox(self) -> list[dict[str, Any]]:
        if not self.imap_host:
            return []
        import email
        import email.policy

        messages: list[dict[str, Any]] = []
        try:
            with self._connect_imap() as imap:
                imap.login(self.username, self.password)
                imap.select("INBOX")
                status, data = imap.search(None, "UNSEEN")
                if status != "OK":
                    return []
                for num in data[0].split():
                    status, fetched = imap.fetch(num, "(RFC822)")
                    if status != "OK" or not fetched:
                        continue
                    raw = _extract_raw(fetched)
                    parsed = email.message_from_bytes(raw, policy=email.policy.default)
                    if self._ignored(parsed):
                        continue
                    full_text = extract_message_text(parsed)
                    in_reply_to = str(parsed.get("In-Reply-To", "") or "").strip()
                    references = parse_message_ids(parsed.get("References", ""))
                    thread_id = str(references[0] if references else in_reply_to)
                    messages.append({
                        "external_id": parsed.get("Message-ID", ""),
                        "thread_id": thread_id,
                        "in_reply_to": in_reply_to,
                        "references": references,
                        "from": email.utils.parseaddr(parsed.get("From", ""))[1],
                        "subject": parsed.get("Subject", ""),
                        "body": strip_quoted_reply(full_text),
                        "raw_body": full_text,
                    })
        except Exception as exc:
            raise MailboxError(f"IMAP fetch failed: {exc}") from exc
        return messages

    def _connect_imap(self):
        import imaplib

        if self.imap_factory is not None:
            return self.imap_factory()
        return imaplib.IMAP4_SSL(self.imap_host, self.imap_port)

    def _mailbox_meta(self, imap: Any) -> tuple[int, int]:
        """Return ``(uid_validity, uidnext)`` for INBOX.

        Real servers (Gmail among them) do not reliably echo ``UIDNEXT`` in the
        SELECT response, so we ask explicitly via STATUS and parse the literal
        reply instead of trusting ``imap.response()`` bookkeeping.
        """
        import re as _re

        uid_validity = 0
        uidnext = 0
        status, data = imap.status("INBOX", "(UIDVALIDITY UIDNEXT)")
        blob = b""
        if status == "OK" and isinstance(data, list) and data:
            blob = data[0] if isinstance(data[0], bytes) else (data[0][0] if isinstance(data[0], (list, tuple)) and data[0] else b"")
        raw = blob.decode("utf-8", errors="replace")
        match = _re.search(r"UIDVALIDITY[ \t]+(\d+)", raw, _re.I)
        if match:
            uid_validity = int(match.group(1))
        match = _re.search(r"UIDNEXT[ \t]+(\d+)", raw, _re.I)
        if match:
            uidnext = int(match.group(1))
        if uidnext == 0:
            # Fallback: highest existing UID + 1 (empty mailbox → 1).
            search_status, search_data = imap.uid("search", None, "ALL")
            if search_status == "OK" and search_data and search_data[0]:
                uids = search_data[0].split()
                uidnext = int(uids[-1]) + 1 if uids else 1
            else:
                uidnext = 1
        return uid_validity, uidnext

    def fetch_inbox_with_cursor(self, *, cursor: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """Fetch only mail newer than a durable high-water mark (IMAP UID).

        ``cursor`` carries ``uid_validity`` / ``last_uid`` / ``seeded`` and is
        persisted by the operator between sessions.  The first run merely
        *establishes* the baseline (no messages are ingested — historical mail
        is never treated as a conversation).  If the mailbox is recreated (its
        ``UIDVALIDITY`` changes) the cursor reseeds and starts over.

        Returns ``{"messages": [...], "cursor": {...}}`` so the caller can
        compactly persist the new high-water mark.
        """
        if not self.imap_host:
            return {"messages": [], "cursor": None}

        import email
        import email.policy
        import time as _time

        stored_validity = int((cursor or {}).get("uid_validity") or 0)
        last_uid = int((cursor or {}).get("last_uid") or 0)
        seeded = bool((cursor or {}).get("seeded"))

        messages: list[dict[str, Any]] = []
        new_validity = 0
        new_last_uid = last_uid
        try:
            with self._connect_imap() as imap:
                imap.login(self.username, self.password)
                select_status, _ = imap.select("INBOX")
                if select_status != "OK":
                    raise MailboxError("IMAP INBOX could not be selected")

                uid_validity, uidnext = self._mailbox_meta(imap)

                reseed = (not seeded) or (stored_validity and stored_validity != uid_validity)
                if reseed:
                    # Baseline only: never ingest mail that predates the operator.
                    new_last_uid = max(uidnext - 1, 0)
                    return {
                        "messages": [],
                        "cursor": {
                            "uid_validity": uid_validity,
                            "last_uid": new_last_uid,
                            "seeded": True,
                            "updated_at": _time.time(),
                        },
                    }

                top_uid = max(uidnext - 1, 0)
                if top_uid <= last_uid:
                    return {
                        "messages": [],
                        "cursor": {
                            "uid_validity": uid_validity,
                            "last_uid": last_uid,
                            "seeded": True,
                            "updated_at": _time.time(),
                        },
                    }

                def _advance(uid_value: Any) -> None:
                    # Only successfully retrieved mail advances the cursor:
                    # failed fetches stay pending for the next tick instead
                    # of being silently skipped forever.
                    nonlocal new_last_uid
                    try:
                        new_last_uid = max(new_last_uid, int(uid_value))
                    except (TypeError, ValueError):
                        pass

                status, data = imap.uid("search", None, f"UID {last_uid + 1}:{top_uid}")
                if status == "OK" and data and data[0]:
                    for num in data[0].split():
                        fetch_status, fetched = imap.uid(
                            "fetch", num, "(BODY.PEEK[] X-GM-MSGID X-GM-THRID)")
                        if fetch_status != "OK" or not fetched:
                            continue
                        raw = _extract_raw(fetched)
                        parsed = email.message_from_bytes(raw, policy=email.policy.default)
                        if self._ignored(parsed):
                            _advance(num)
                            continue
                        full_text = extract_message_text(parsed)
                        in_reply_to = str(parsed.get("In-Reply-To", "") or "").strip()
                        references = parse_message_ids(parsed.get("References", ""))
                        gm_msgid, gm_thrid = gmail_immutable_ids(parsed)
                        rfc_message_id = str(parsed.get("Message-ID", "") or "").strip()
                        # Gmail's immutable thread id is authoritative when
                        # present; the References/In-Reply-To derived value is
                        # only a fallback for non-Gmail servers.
                        thread_id = gm_thrid or str(references[0] if references else in_reply_to)
                        messages.append({
                            "external_id": rfc_message_id or f"uid-{num}",
                            "gmail_message_id": gm_msgid or "",
                            "gmail_thread_id": gm_thrid or "",
                            "rfc_message_id": rfc_message_id,
                            "thread_id": thread_id,
                            "in_reply_to": in_reply_to,
                            "references": references,
                            "from": email.utils.parseaddr(parsed.get("From", ""))[1],
                            "subject": parsed.get("Subject", ""),
                            "body": strip_quoted_reply(full_text),
                            "raw_body": full_text,
                            "received_at": _header_date(parsed),
                        })
                        _advance(num)
        except Exception as exc:
            raise MailboxError(f"IMAP fetch failed: {exc}") from exc
        return {
            "messages": messages,
            "cursor": {
                "uid_validity": uid_validity,
                "last_uid": new_last_uid,
                "seeded": True,
                "updated_at": _time.time(),
            },
        }

    def search_sent(self, in_reply_to: str) -> Optional[str]:
        """Best-effort lookup of an already-accepted reply in Sent Mail.

        Used by crash reconciliation: when a response may have been
        transmitted but its local commit is missing, finding the provider
        message id here prevents a duplicate send. Returns the provider
        message id or None. Never raises; unknown means unknown, not absent.
        """
        if not self.imap_host or not in_reply_to:
            return None
        import email as _email

        candidates = ['"[Gmail]/Sent Mail"', '"[Google Mail]/Sent Mail"', '"Sent"']
        try:
            with self._connect_imap() as imap:
                imap.login(self.username, self.password)
                for folder in candidates:
                    try:
                        status, _ = imap.select(folder)
                    except Exception:
                        continue
                    if status != "OK":
                        continue
                    safe = in_reply_to.replace('"', "")
                    status, data = imap.uid(
                        "search", None, f'HEADER "In-Reply-To" "{safe}"')
                    if status == "OK" and data and data[0]:
                        first = data[0].split()[0]
                        status, fetched = imap.uid("fetch", first, "(RFC822.HEADER)")
                        if status == "OK" and fetched:
                            raw = _extract_raw(fetched)
                            if raw:
                                parsed = _email.message_from_bytes(raw)
                                found = str(parsed.get("Message-ID", "") or "").strip()
                                if found:
                                    return found
        except Exception:
            return None
        return None

    def _ignored(self, parsed: Any) -> bool:
        """True when a message is automation (bounce, autoreply, our own copy)."""
        from_email = parsed.get("From", "")
        from_addr = re.sub(r".*<([^>]+)>", r"\1", from_email).strip().lower()
        if from_addr and from_addr == self.sender.strip().lower():
            return True
        subject = str(parsed.get("Subject", "") or "").lower()
        if any(token in subject for token in (
            "delivery status notification", "undelivered mail", "undeliverable",
            "mail delivery failed", "returned mail", "returned to sender",
            "mail status notification",
        )):
            return True
        content_type = str(parsed.get_content_type() or "").lower()
        if content_type in ("multipart/report", "message/delivery-status", "text/rfc822-headers"):
            return True
        auto_submitted = str(parsed.get("Auto-Submitted", "no") or "no").strip().lower()
        if auto_submitted and auto_submitted != "no":
            return True
        precedence = str(parsed.get("Precedence", "") or "").strip().lower()
        if precedence in ("auto_reply", "bulk", "list"):
            return True
        if parsed.get("X-Autoreply", "") or parsed.get("X-Auto-Response-Suppress", ""):
            return True
        if parsed.get("List-Unsubscribe", "") or parsed.get("List-Id", "") or parsed.get("List-Post", ""):
            return True
        return False

    def validate(self) -> dict[str, Any]:
        """Probe SMTP/IMAP reachability and auth without sending and without
        printing credentials.  Returns per-channel OK/error facts."""
        import smtplib

        smtp: dict[str, Any] = {"host": self.host, "configured": bool(self.host and self.sender), "auth": bool(self.username)}
        if not smtp["configured"]:
            smtp.update({"ok": False, "detail": "missing IDENTITY_SMTP_HOST (and IDENTITY_EMAIL_FROM)"})
        else:
            try:
                with smtplib.SMTP(self.host, self.port, timeout=10) as client:
                    if self.use_tls:
                        client.starttls()
                    if self.username:
                        client.login(self.username, self.password)
                    client.noop()
                smtp.update({"ok": True, "detail": "reachable, authenticated"})
            except Exception as exc:  # pragma: no cover - network dependent
                smtp.update({"ok": False, "detail": f"{type(exc).__name__}: {exc}"})

        imap: dict[str, Any] = {"host": self.imap_host, "configured": bool(self.imap_host), "auth": bool(self.username)}
        if not imap["configured"]:
            imap.update({"ok": False, "detail": "missing IDENTITY_IMAP_HOST"})
        else:
            try:
                import imaplib

                with imaplib.IMAP4_SSL(self.imap_host, self.imap_port) as client:
                    client.login(self.username, self.password)
                    status, _ = client.select("INBOX")
                imap.update({"ok": status == "OK", "detail": "reachable, authenticated, INBOX selectable"})
            except Exception as exc:  # pragma: no cover - network dependent
                imap.update({"ok": False, "detail": f"{type(exc).__name__}: {exc}"})

        return {"smtp": smtp, "imap": imap}


class MailboxTransport:
    """Adapts a mailbox backend to the operations engine transport protocol."""

    def __init__(self, backend: Any) -> None:
        self.backend = backend

    def send(
        self, *, to: str, subject: str, body: str, thread_id: str = "",
        in_reply_to: str = "", references: Optional[Iterable[str]] = None,
        message_id: str = "", sender: str = "", sender_display_name: str = "",
        reply_to: str = "", html_body: str = "",
    ) -> dict[str, Any]:
        return self.backend.send(
            to=to, subject=subject, body=body, thread_id=thread_id,
            in_reply_to=in_reply_to, references=references, message_id=message_id,
            sender=sender, sender_display_name=sender_display_name,
            reply_to=reply_to, html_body=html_body,
        )

    def fetch_inbox_with_cursor(self, *, cursor: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        fn = getattr(self.backend, "fetch_inbox_with_cursor", None)
        if fn is None:
            return {"messages": self.backend.fetch_inbox(), "cursor": None}
        return fn(cursor=cursor)

    def fetch_inbox(self) -> list[dict[str, Any]]:
        return self.backend.fetch_inbox()
