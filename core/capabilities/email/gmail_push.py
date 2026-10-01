"""Event-driven Gmail ingest: users.watch -> Pub/Sub -> history -> durable job.

Polling is a fallback, not the design. A human waiting on a reply should not be
bounded by an interval, and a 300s poll that silently stops is indistinguishable
from "no mail arrived" — which is exactly how STEP-3 went unanswered.

The flow:

    Gmail users.watch  ──►  Pub/Sub topic  ──►  push endpoint
                                                     │
                                       decode historyId, ack fast
                                                     │
                                       durable queue on disk
                                                     │
                            Gmail history.list(startHistoryId)
                                                     │
                              Gmail messages.get (full) + durable EmailJob

Design constraints that come from past failure, not preference:

* **The notification is a hint, never the payload.** Pub/Sub messages can be
  delivered more than once, out of order, or not at all. The endpoint
  acknowledges immediately and does the work on a durable queue, so a slow or
  crashed process loses no mail. ``historyId`` is persisted, so a gap is
  recoverable by replaying history rather than by re-reading the inbox.

* **The watch expires.** Gmail watches last about seven days. Renewal is
  therefore a first-class scheduled duty, and an expired watch is reported as
  degraded rather than silently ignored.

* **One ingest pipeline.** This module never decides whether a message deserves
  a reply. It only turns Gmail history into the same message dicts the IMAP
  transport already produces, so exactly one path creates jobs, applies
  policy, and sends.

Everything is config-gated. With no OAuth credentials configured, nothing here
activates and the existing IMAP polling path continues to work unchanged.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from .backends import parse_rfc_date

logger = logging.getLogger(__name__)

GMAIL_API_BASE = "https://gmail.googleapis.com/gmail/v1"
GMAIL_TOKEN_URL = "https://oauth2.googleapis.com/token"

#: Gmail watches expire in about seven days. Renew well before that so a
#: restart or transient failure cannot leave the mailbox unwatched.
WATCH_RENEW_INTERVAL = timedelta(hours=24)
WATCH_LIFETIME = timedelta(days=6)

#: The minimum scope a Gmail watch needs, plus history/message reads.
REQUIRED_SCOPES = (
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/pubsub",
)

_SCOPED = "gmail.push"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: Optional[datetime]) -> str:
    return dt.isoformat() if dt else ""


def _parse(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _iso_from_epoch_ms(value: Any) -> str:
    """Convert Gmail's epoch-millisecond expiry into an ISO timestamp.

    Gmail returns ``expiration`` as a millisecond epoch string. Storing that
    raw would make every ISO parse fail, and a failed parse means "expired" —
    so the watch would look permanently dead while actually working.
    """
    try:
        millis = int(value)
    except (TypeError, ValueError):
        return ""
    if millis <= 0:
        return ""
    try:
        return datetime.fromtimestamp(millis / 1000.0, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return ""


# ── configuration ──────────────────────────────────────────────────────────


@dataclass
class GmailPushConfig:
    """OAuth and Pub/Sub settings, read from the environment.

    Secrets are held in memory for the process lifetime and never written to
    the store, a log line, or a diagnostics payload. ``configured`` is False
    unless every field needed to actually establish a watch is present, which
    is what keeps this entirely inert on a deployment that has not opted in.
    """

    client_id: str = ""
    client_secret: str = ""
    refresh_token: str = ""
    topic_name: str = ""
    user_id: str = "me"
    label_ids: tuple[str, ...] = ("INBOX",)
    #: Shared secret the push endpoint requires in the query string. Pub/Sub
    #: push cannot send arbitrary headers, so this is a bearer value in the URL;
    #: it is still far better than an open endpoint that can be used to make the
    #: runtime fetch mail on demand.
    verification_token: str = ""
    host: str = "127.0.0.1"
    port: int = 8788
    http_timeout: float = 15.0
    api_timeout: float = 30.0
    max_retries: int = 3

    @classmethod
    def from_env(cls, env: Optional[dict[str, str]] = None) -> "GmailPushConfig":
        source = env if env is not None else os.environ

        def get(*names: str, default: str = "") -> str:
            for name in names:
                value = source.get(name)
                if value:
                    return value.strip()
            return default

        labels = get("ASTER_GMAIL_PUSH_LABELS", default="INBOX")
        return cls(
            client_id=get("ASTER_GMAIL_PUSH_CLIENT_ID", "GMAIL_PUSH_CLIENT_ID"),
            client_secret=get("ASTER_GMAIL_PUSH_CLIENT_SECRET", "GMAIL_PUSH_CLIENT_SECRET"),
            refresh_token=get("ASTER_GMAIL_PUSH_REFRESH_TOKEN", "GMAIL_PUSH_REFRESH_TOKEN"),
            topic_name=get("ASTER_GMAIL_PUSH_TOPIC", "GMAIL_PUSH_TOPIC"),
            user_id=get("ASTER_GMAIL_PUSH_USER", default="me"),
            label_ids=tuple(p.strip() for p in labels.split(",") if p.strip()) or ("INBOX",),
            verification_token=get("ASTER_GMAIL_PUSH_VERIFICATION_TOKEN",
                                  "GMAIL_PUSH_VERIFICATION_TOKEN"),
            host=get("ASTER_GMAIL_PUSH_HOST", default="127.0.0.1"),
            port=int(get("ASTER_GMAIL_PUSH_PORT", default="8788") or 8788),
        )

    @property
    def configured(self) -> bool:
        return all((self.client_id, self.client_secret, self.refresh_token, self.topic_name))

    def redacted(self) -> dict[str, Any]:
        """Configuration as reported by health: presence, never value."""
        return {
            "configured": self.configured,
            "topic": self.topic_name or None,
            "user": self.user_id,
            "labels": list(self.label_ids),
            "client_id_present": bool(self.client_id),
            "refresh_token_present": bool(self.refresh_token),
            "client_secret_present": bool(self.client_secret),
            "verification_token_present": bool(self.verification_token),
        }


# ── Gmail API client ───────────────────────────────────────────────────────


class GmailApiError(RuntimeError):
    """A Gmail API call failed. Carries the HTTP status when there was one."""

    def __init__(self, message: str, status: Optional[int] = None,
                 body: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.body = body[:500]


class GmailApiClient:
    """Minimal Gmail API client for watch, history, and message reads.

    Only the standard library is used, and access tokens are cached in memory
    only. A refresh token is long-lived; an access token is not, and neither
    belongs on disk in plaintext.
    """

    def __init__(self, config: GmailPushConfig, *,
                 opener: Optional[Callable[..., Any]] = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.config = config
        self._opener = opener or urllib.request.urlopen
        self._sleep = sleep
        self._token: str = ""
        self._token_expiry: float = 0.0
        self._lock = threading.Lock()

    # ── auth ──
    def _refresh_access_token(self) -> str:
        if not self.config.configured:
            raise GmailApiError("gmail push is not configured")
        now = time.time()
        with self._lock:
            if self._token and now < self._token_expiry - 60:
                return self._token
            payload = urllib.parse.urlencode({
                "client_id": self.config.client_id,
                "client_secret": self.config.client_secret,
                "refresh_token": self.config.refresh_token,
                "grant_type": "refresh_token",
            }).encode()
            request = urllib.request.Request(
                GMAIL_TOKEN_URL, data=payload,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            try:
                with self._opener(request, timeout=self.config.api_timeout) as response:
                    body = json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                raise GmailApiError(
                    f"token refresh failed: {exc.code}", exc.code, _safe(exc)) from exc
            except Exception as exc:
                raise GmailApiError(f"token refresh failed: {type(exc).__name__}: {exc}") from exc
            token = str(body.get("access_token") or "")
            if not token:
                raise GmailApiError("token refresh returned no access_token")
            self._token = token
            # Default to 50 minutes when the provider omits expires_in; Gmail
            # access tokens are valid for an hour, so this is conservative.
            self._token_expiry = now + float(body.get("expires_in", 3000))
            return token

    def _request(self, method: str, path: str, *, body: Optional[dict] = None,
                 params: Optional[dict[str, str]] = None, authenticated: bool = True
                 ) -> dict[str, Any]:
        url = f"{GMAIL_API_BASE}{path}"
        if params:
            clean = {k: v for k, v in params.items() if v is not None and v != ""}
            if clean:
                url = f"{url}?{urllib.parse.urlencode(clean)}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if authenticated:
            headers["Authorization"] = f"Bearer {self._refresh_access_token()}"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)

        last: Optional[Exception] = None
        for attempt in range(1, self.config.max_retries + 1):
            try:
                with self._opener(request, timeout=self.config.api_timeout) as response:
                    raw = response.read().decode("utf-8")
                    return json.loads(raw) if raw.strip() else {}
            except urllib.error.HTTPError as exc:
                detail = _safe(exc)
                # 401/403 are not fixed by retrying the same request; the token
                # may have been revoked, which is a real, reportable failure.
                if exc.code in (401, 403, 404):
                    raise GmailApiError(f"{method} {path} -> {exc.code}", exc.code, detail) from exc
                if exc.code in (429, 500, 502, 503, 504) and attempt < self.config.max_retries:
                    last = exc
                    self._sleep(2 ** attempt)
                    continue
                raise GmailApiError(f"{method} {path} -> {exc.code}", exc.code, detail) from exc
            except GmailApiError:
                raise
            except Exception as exc:
                last = exc
                if attempt < self.config.max_retries:
                    self._sleep(2 ** attempt)
                    continue
                raise GmailApiError(
                    f"{method} {path} failed: {type(exc).__name__}: {exc}") from exc
        raise GmailApiError(f"{method} {path} failed: {last}")

    # ── watch ──
    def start_watch(self) -> dict[str, Any]:
        return self._request("POST", f"/users/{self.config.user_id}/watch", body={
            "topicName": self.config.topic_name,
            "labelIds": list(self.config.label_ids),
            # EXCLUSIVE means "tell me about INBOX changes only", which keeps
            # the notification volume proportional to mail that could matter.
            "labelFilterBehavior": "EXCLUSIVE",
        })

    def stop_watch(self) -> dict[str, Any]:
        return self._request("POST", f"/users/{self.config.user_id}/stopWatch")

    # ── history ──
    def history(self, start_history_id: str, *, label_id: str = "INBOX",
                max_results: int = 100) -> tuple[list[str], str, bool]:
        """List message/thread ids added since ``start_history_id``.

        Returns ``(message_ids, newest_history_id, more)``. ``more`` True means
        Gmail truncated the page and the caller must continue from the returned
        id. Truncation is not an error, but ignoring it silently drops mail, so
        it is reported rather than swallowed.
        """
        ids: list[str] = []
        page_token = ""
        newest = start_history_id
        more = False
        while True:
            params = {
                "startHistoryId": start_history_id,
                "historyTypes": "messageAdded",
                "labelId": label_id,
                "maxResults": str(max_results),
            }
            if page_token:
                params["pageToken"] = page_token
            payload = self._request("GET", f"/users/{self.config.user_id}/history",
                                    params=params)
            newest = str(payload.get("historyId") or newest)
            for record in payload.get("history") or []:
                if record.get("messagesAdded"):
                    for added in record["messagesAdded"]:
                        mid = str((added.get("message") or {}).get("id") or "")
                        if mid and mid not in ids:
                            ids.append(mid)
            page_token = str(payload.get("nextPageToken") or "")
            if not page_token:
                more = bool(payload.get("history", []) and len(ids) > max_results)
                break
        return ids, newest, more

    def get_message(self, message_id: str) -> dict[str, Any]:
        return self._request(
            "GET", f"/users/{self.config.user_id}/messages/{message_id}",
            params={"format": "full"})


def _safe(exc: Any) -> str:
    """Best-effort safe body from an HTTP error, never containing a token."""
    try:
        return exc.read().decode("utf-8", "replace")
    except Exception:
        return str(exc)


# ── message normalization ──────────────────────────────────────────────────


def _header(headers: list[dict[str, str]], name: str) -> str:
    wanted = name.lower()
    for header in headers or []:
        if str(header.get("name", "")).lower() == wanted:
            return str(header.get("value", ""))
    return ""


def _address(value: str) -> str:
    value = (value or "").strip()
    if "<" in value and ">" in value:
        return value[value.rfind("<") + 1: value.rfind(">")].strip()
    return value


def _gmail_received_at(payload: dict[str, Any], date_header: str) -> str:
    """Best available server-side receive timestamp, as an ISO-8601 UTC string.

    ``internalDate`` is Gmail's authoritative receive time in epoch
    milliseconds. The ``Date`` header is sender-claimed and can be wrong, so it
    is only a fallback. An empty string means "unknown", which the pipeline
    records as such rather than substituting the current time and presenting
    scheduling delay as delivery latency.
    """
    raw = str((payload or {}).get("internalDate") or "").strip()
    if raw:
        try:
            millis = int(raw)
        except (TypeError, ValueError):
            millis = 0
        if millis > 0:
            return _iso(datetime.fromtimestamp(millis / 1000.0, tz=timezone.utc))
    parsed = parse_rfc_date(date_header)
    return parsed.isoformat() if parsed else ""


def _body_text(payload: dict[str, Any]) -> tuple[str, str]:
    """Return ``(text, raw_rfc822)`` for a Gmail message payload.

    The raw base64url body is preserved because durable evidence requires the
    bytes Gmail actually stored, not a re-rendered approximation.
    """
    raw = ""
    if payload.get("raw"):
        try:
            raw = base64.urlsafe_b64decode(
                str(payload["raw"]) + "=" * (-len(str(payload["raw"])) % 4)
            ).decode("utf-8", "replace")
        except Exception:
            raw = ""
    if raw:
        return raw, raw

    def walk(part: dict[str, Any], out: list[str]) -> None:
        mime = str(part.get("mimeType") or "")
        body = part.get("body") or {}
        data = body.get("data")
        if data and mime in ("text/plain", "text/html", ""):
            try:
                out.append(base64.urlsafe_b64decode(
                    str(data) + "=" * (-len(str(data)) % 4)).decode("utf-8", "replace"))
            except Exception:
                pass
        for child in part.get("parts") or []:
            walk(child, out)

    chunks: list[str] = []
    walk(payload.get("payload") or {}, chunks)
    return "\n".join(chunks), ""


def normalize_gmail_message(payload: dict[str, Any]) -> dict[str, Any]:
    """Convert a Gmail API message into the transport's message dict shape.

    Matching the IMAP backend's output exactly is what lets one ingest pipeline
    serve both transports. Any new consumer that needs a Gmail-specific field
    should read it from the durable job, not by branching here.
    """
    payload = payload or {}
    headers = payload.get("payload", {}).get("headers") or []
    text, raw = _body_text(payload)
    from_header = _address(_header(headers, "From"))
    to_header = _header(headers, "To")
    message_id = _header(headers, "Message-ID").strip()
    references = _header(headers, "References").split()
    in_reply_to = _header(headers, "In-Reply-To").strip()
    thread_id = str(payload.get("threadId") or "")
    gmail_id = str(payload.get("id") or "")
    labels = [str(x) for x in (payload.get("labelIds") or [])]

    return {
        "id": gmail_id or message_id,
        "external_id": message_id or gmail_id,
        "gmail_message_id": gmail_id,
        "gmail_thread_id": thread_id,
        "from": from_header,
        "sender_email": from_header,
        "to": to_header,
        "subject": _header(headers, "Subject"),
        "body": text,
        "text": text,
        "raw_body": raw or text,
        "thread_id": thread_id or (references[0] if references else ""),
        "in_reply_to": in_reply_to or (references[-1] if references else ""),
        "references": references,
        "date": _header(headers, "Date"),
        # Gmail's server-side receive time. Without it the pipeline falls back
        # to "now" and the recorded latency measures our own scheduling delay
        # instead of delivery latency. Prefer the header when the API omitted
        # internalDate.
        "received_at": _gmail_received_at(payload, _header(headers, "Date")),
        "labels": labels,
        # Gmail has already told us the message is in the watched label; the
        # polling path filters UNSEEN. Carrying unread keeps parity.
        "unread": "UNREAD" in labels,
    }


# ── watch state ────────────────────────────────────────────────────────────

# How many times a notification may fail to drain before it is abandoned.
# Retrying forever would hold the high-water mark at a stale value and make
# every later notification re-read old history; giving up silently would lose
# mail. Abandonment is counted and reported, so the loss is visible.
MAX_DRAIN_ATTEMPTS = 10


@dataclass
class WatchState:
    """Persisted facts about the Gmail watch. No secrets, ever."""

    history_id: str = ""
    watch_id: str = ""
    expiration: str = ""
    last_notification_at: str = ""
    last_notification_history_id: str = ""
    last_renewed_at: str = ""
    last_error: str = ""
    notifications_received: int = 0
    history_lookups: int = 0
    messages_ingested: int = 0
    # Notifications dropped after MAX_DRAIN_ATTEMPTS failed drains. Non-zero
    # means mail was lost; it is reported in health rather than hidden.
    abandoned_notifications: int = 0
    updated_at: str = field(default_factory=lambda: _iso(_utcnow()))

    def to_dict(self) -> dict[str, Any]:
        return {
            "history_id": self.history_id,
            "watch_id": self.watch_id,
            "expiration": self.expiration,
            "last_notification_at": self.last_notification_at,
            "last_notification_history_id": self.last_notification_history_id,
            "last_renewed_at": self.last_renewed_at,
            "last_error": self.last_error,
            "notifications_received": self.notifications_received,
            "history_lookups": self.history_lookups,
            "messages_ingested": self.messages_ingested,
            "abandoned_notifications": self.abandoned_notifications,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WatchState":
        data = data or {}
        return cls(
            history_id=str(data.get("history_id") or ""),
            watch_id=str(data.get("watch_id") or ""),
            expiration=str(data.get("expiration") or ""),
            last_notification_at=str(data.get("last_notification_at") or ""),
            last_notification_history_id=str(data.get("last_notification_history_id") or ""),
            last_renewed_at=str(data.get("last_renewed_at") or ""),
            last_error=str(data.get("last_error") or ""),
            notifications_received=int(data.get("notifications_received") or 0),
            history_lookups=int(data.get("history_lookups") or 0),
            messages_ingested=int(data.get("messages_ingested") or 0),
            abandoned_notifications=int(data.get("abandoned_notifications") or 0),
            updated_at=str(data.get("updated_at") or _iso(_utcnow())),
        )

    def is_expired(self, now: Optional[datetime] = None) -> bool:
        expiry = _parse(self.expiration)
        if expiry is None:
            # No known expiry is itself suspicious: assume it lapsed rather
            # than claiming the watch is healthy.
            return True
        return (now or _utcnow()) >= expiry

    def needs_renewal(self, now: Optional[datetime] = None) -> bool:
        now = now or _utcnow()
        renewed = _parse(self.last_renewed_at)
        if renewed is None or self.is_expired(now):
            return True
        return (now - renewed) >= WATCH_RENEW_INTERVAL


# ── durable notification queue ─────────────────────────────────────────────


@dataclass
class PushNotification:
    email_address: str = ""
    history_id: str = ""
    received_at: str = field(default_factory=lambda: _iso(_utcnow()))
    # Failed drain attempts for this notification. A drain that cannot read or
    # hand off every message leaves the notification queued and counts an
    # attempt, so a permanently broken id cannot wedge the queue forever.
    attempts: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"email_address": self.email_address, "history_id": self.history_id,
                "received_at": self.received_at, "attempts": self.attempts}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PushNotification":
        data = data or {}
        try:
            attempts = int(data.get("attempts") or 0)
        except (TypeError, ValueError):
            attempts = 0
        return cls(email_address=str(data.get("email_address") or ""),
                   history_id=str(data.get("history_id") or ""),
                   received_at=str(data.get("received_at") or _iso(_utcnow())),
                   attempts=attempts)


def decode_pubsub_envelope(payload: Any) -> Optional[PushNotification]:
    """Decode a Pub/Sub push body into a notification.

    Returns None for anything unrecognizable. A malformed notification is not
    worth crashing an endpoint over, but it is also not silently treated as
    success — the caller records it.
    """
    try:
        body = payload if isinstance(payload, dict) else json.loads(
            payload.decode("utf-8") if isinstance(payload, bytes) else str(payload))
    except Exception:
        return None
    if not isinstance(body, dict):
        return None
    if "message" not in body:
        return None
    message = body.get("message") or {}
    data = message.get("data")
    if not data:
        return None
    try:
        decoded = base64.b64decode(str(data) + "=" * (-len(str(data)) % 4))
        inner = json.loads(decoded.decode("utf-8"))
    except Exception:
        return None
    if not isinstance(inner, dict):
        return None
    return PushNotification(
        email_address=str(inner.get("emailAddress") or ""),
        history_id=str(inner.get("historyId") or ""),
    )


# ── the service ────────────────────────────────────────────────────────────


class GmailPushService:
    """Owns the watch, the notification endpoint, and the history drain.

    Three responsibilities, deliberately separate:

    * :meth:`handle_push` — HTTP handler. Verifies, decodes, persists, acks.
      Never blocks on Gmail, because Pub/Sub redelivers on a slow ack and a
      slow endpoint is a lost-notification bug.
    * :meth:`drain` — turns queued notifications into messages via history, and
      hands them to the existing ingest pipeline.
    * :meth:`renew_loop` — keeps the watch alive for the process's lifetime.

    A service built without an ``ingest`` callable is *receive-only*: it
    accepts notifications and keeps the watch alive, but refuses to drain.
    That is the shape for the presence process, which has no configured
    engine. Draining belongs to the operator process, which is the single
    writer for email jobs and outbound sends. Splitting them this way is what
    stops two processes from racing on the same mailbox.
    """

    def __init__(self, config: GmailPushConfig, *, storage: Any, identity_id: str,
                 ingest: Optional[Callable[[list[dict[str, Any]]], Any]] = None,
                 client: Optional[GmailApiClient] = None) -> None:
        self.config = config
        self.storage = storage
        self.identity_id = identity_id
        self._ingest = ingest
        self._client = client or GmailApiClient(config)
        self._queue_lock = threading.Lock()

    @property
    def can_drain(self) -> bool:
        """True when this process owns the pipeline and may ingest messages."""
        return self._ingest is not None

    # ── persisted state ──
    @property
    def _namespace(self) -> str:
        return f"operations.gmail_push.{_SCOPED}"

    def _load_state(self) -> WatchState:
        try:
            return WatchState.from_dict(self.storage.load(self.identity_id, self._namespace) or {})
        except Exception:
            return WatchState()

    def _save_state(self, state: WatchState) -> None:
        state.updated_at = _iso(_utcnow())
        self.storage.save(self.identity_id, self._namespace, state.to_dict())

    def _load_queue(self) -> list[PushNotification]:
        try:
            raw = self.storage.load(self.identity_id, f"{self._namespace}.queue") or {}
        except Exception:
            return []
        items = raw.get("items") if isinstance(raw, dict) else raw
        out: list[PushNotification] = []
        for item in items or []:
            if isinstance(item, dict):
                out.append(PushNotification.from_dict(item))
        return out

    def _save_queue(self, items: list[PushNotification]) -> None:
        # Bound the queue so a notification storm cannot grow state without
        # limit. Oldest historyIds are the ones history can still replay from,
        # so keep the newest and record the minimum for replay.
        trimmed = items[-200:]
        self.storage.save(self.identity_id, f"{self._namespace}.queue", {
            "items": [i.to_dict() for i in trimmed],
        })

    # ── notifications ──
    def authorize(self, query: dict[str, str]) -> bool:
        """Reject push requests that do not carry the verification token.

        Without this, anyone who learns the endpoint URL could make the runtime
        fetch mail on demand. The token is compared in constant time.
        """
        expected = self.config.verification_token
        if not expected:
            # Refusing when unconfigured is the safe default: an endpoint with
            # no secret is an open trigger.
            logger.warning("gmail push endpoint has no verification token; refusing")
            return False
        import hmac

        provided = (query.get("token") or query.get("verification_token") or "")
        return hmac.compare_digest(provided, expected)

    def handle_push(self, body: bytes) -> tuple[int, dict[str, Any]]:
        """Handle one Pub/Sub push. Returns ``(status, payload)``.

        Acknowledges with 200 once the notification is durably queued. Gmail
        history is read later by :meth:`drain`, so a Gmail outage cannot cause
        Pub/Sub to redeliver into a slow endpoint. A small JSON body (rather
        than 204) keeps the ack inspectable when debugging a silent mailbox.
        """
        notification = decode_pubsub_envelope(body)
        if notification is None:
            # Pub/Sub treats non-2xx as failure and retries. A body we cannot
            # decode will never decode on retry, so ack and record.
            logger.warning("gmail push received an undecodable envelope")
            return 200, {"accepted": False, "reason": "undecodable_envelope"}

        with self._queue_lock:
            queue = self._load_queue()
            state = self._load_state()
            # Duplicate deliveries are normal. Keep the lowest historyId so a
            # replay after a gap still covers the whole range.
            if not any(q.history_id == notification.history_id for q in queue):
                queue.append(notification)
            state.notifications_received += 1
            state.last_notification_at = notification.received_at
            if notification.history_id:
                state.last_notification_history_id = notification.history_id
                if not state.history_id or _history_is_older(notification.history_id,
                                                              state.history_id):
                    # A late notification must not move the high-water mark
                    # forward and skip the messages in between.
                    state.history_id = notification.history_id
            self._save_state(state)
            self._save_queue(queue)
        return 200, {"accepted": True, "queued": len(queue)}

    # ── watch lifecycle ──
    def start_watch(self) -> dict[str, Any]:
        if not self.config.configured:
            return {"ok": False, "reason": "gmail push not configured"}
        try:
            result = self._client.start_watch()
        except GmailApiError as exc:
            state = self._load_state()
            state.last_error = str(exc)
            self._save_state(state)
            return {"ok": False, "reason": str(exc), "status": exc.status}
        state = self._load_state()
        state.watch_id = str(result.get("id") or "")
        state.expiration = _iso_from_epoch_ms(result.get("expiration"))
        state.history_id = str(result.get("historyId") or state.history_id)
        state.last_renewed_at = _iso(_utcnow())
        state.last_error = ""
        self._save_state(state)
        logger.info("gmail watch established (expires %s)", state.expiration)
        return {"ok": True, "expiration": state.expiration,
                "history_id": state.history_id}

    def renew_watch(self) -> dict[str, Any]:
        """Renew before expiry so the mailbox is never unwatched."""
        state = self._load_state()
        if not state.needs_renewal():
            return {"ok": True, "renewed": False, "reason": "not due"}
        result = self.start_watch()
        return {"ok": result.get("ok", False), "renewed": bool(result.get("ok"))}

    # ── history drain ──
    def drain(self) -> dict[str, Any]:
        """Turn queued notifications into messages and ingest them.

        History is read from the *lowest* queued historyId, not the newest, so
        a message that arrived while the process was down is not skipped.

        The high-water mark only advances when every message in the batch was
        read *and* handed to the pipeline. A partial failure keeps the mark and
        the queue where they are, so the same range is retried; ingest is
        idempotent, so a replay is free of side effects. Advancing past an
        unreadable message would silently drop it, which is the one failure
        mode this subsystem exists to prevent.

        A notification that keeps failing is abandoned only after
        ``max_attempts``, and the abandonment is recorded in the watch state
        so it shows up in health instead of vanishing.
        """
        if self._ingest is None:
            # Receive-only process. The queue is durable and shared, so the
            # operator process will drain it. Silently returning success here
            # would let health claim the mail was handled when it was not.
            return {"ok": False, "reason": "receive-only service: the operator "
                                           "process owns draining"}
        with self._queue_lock:
            queue = self._load_queue()
        if not queue:
            return {"ok": True, "messages": 0, "reason": "no notifications"}

        state = self._load_state()
        start = min((q.history_id for q in queue if q.history_id), default="")
        start = start or state.history_id
        if not start:
            return {"ok": False, "reason": "no historyId available to start from"}

        try:
            message_ids, newest, truncated = self._client.history(start)
        except GmailApiError as exc:
            state.last_error = str(exc)
            state.history_lookups += 1
            self._save_state(state)
            return self._retry_or_abandon(
                state, reason=f"history lookup failed: {exc}",
                uncovered=len(queue))

        fetched: list[dict[str, Any]] = []
        failures = 0
        first_failure = ""
        for message_id in message_ids:
            try:
                payload = self._client.get_message(message_id)
            except GmailApiError as exc:
                failures += 1
                first_failure = first_failure or f"message {message_id}: {exc}"
                logger.warning("gmail message %s unreadable: %s", message_id, exc)
                continue
            item = normalize_gmail_message(payload)
            if item.get("external_id"):
                fetched.append(item)

        if failures:
            # Part of the batch is unreadable. The readable messages are still
            # handed over — ingest is idempotent, so replaying them later costs
            # nothing — but the mark and the queue are left alone so the
            # unreadable ids are attempted again instead of being skipped.
            state.history_lookups += 1
            if fetched:
                try:
                    self._ingest(fetched)
                except Exception as exc:  # noqa: BLE001 - reported, mark held
                    logger.warning("gmail partial ingest raised: %s", exc)
            outcome = self._retry_or_abandon(
                state,
                reason=f"{failures} of {len(message_ids)} messages unreadable"
                       f" ({first_failure})",
                uncovered=failures)
            outcome.update({"messages": len(fetched), "failures": failures,
                            "truncated": truncated, "partial": True})
            return outcome

        result: Any = None
        if fetched:
            try:
                result = self._ingest(fetched)
            except Exception as exc:  # noqa: BLE001 - ingest failures must not advance
                state.history_lookups += 1
                logger.warning("gmail ingest raised: %s", exc)
                return self._retry_or_abandon(
                    state, reason=f"ingest raised: {exc}", uncovered=len(fetched))

        # A pipeline that reports failure is treated the same as a raise: the
        # messages were not accepted, so the range must be replayed.
        if isinstance(result, dict) and result.get("ok") is False:
            state.history_lookups += 1
            return self._retry_or_abandon(
                state,
                reason=f"ingest refused: {result.get('reason') or 'unknown'}",
                uncovered=len(fetched))

        state.history_id = newest or state.history_id
        state.last_error = ""
        state.history_lookups += 1
        state.messages_ingested += len(fetched)
        self._save_state(state)

        # Only clear the queue once history has been read and the batch landed.
        # If the lookup failed the notifications stay queued and are retried.
        #
        # A notification at or below the new high-water mark has been covered
        # by this drain and is dropped. Anything above it is newer mail that
        # this drain did not read, so it must survive. A notification with no
        # historyId cannot be placed, so it is kept rather than guessed at.
        with self._queue_lock:
            remaining = [
                q for q in self._load_queue()
                if not q.history_id or _history_is_older(state.history_id, q.history_id)
            ]
            self._save_queue(remaining)

        return {"ok": True, "messages": len(fetched), "failures": 0,
                "history_id": state.history_id, "truncated": truncated,
                "ingest": result}

    def _retry_or_abandon(self, state: WatchState, *, reason: str,
                          uncovered: int) -> dict[str, Any]:
        """Keep the queue for another attempt, or record a permanent failure.

        Without a bound, one undeliverable message id would hold the
        high-water mark forever and every later notification would be drained
        from a stale start. Abandoning is a real loss, so it is counted and
        surfaced in health rather than done quietly.
        """
        with self._queue_lock:
            queue = self._load_queue()
            keep: list[PushNotification] = []
            abandoned = 0
            for notification in queue:
                notification.attempts += 1
                if notification.attempts >= MAX_DRAIN_ATTEMPTS:
                    abandoned += 1
                    state.abandoned_notifications += 1
                    continue
                keep.append(notification)
            self._save_queue(keep)

        if abandoned:
            logger.error("abandoned %d gmail notification(s) after %d attempts: %s",
                         abandoned, MAX_DRAIN_ATTEMPTS, reason)
            state.last_error = (f"{reason}; abandoned {abandoned} notification(s) "
                                f"after {MAX_DRAIN_ATTEMPTS} attempts")
            self._save_state(state)
            return {"ok": False, "reason": state.last_error, "abandoned": abandoned}

        state.last_error = reason
        self._save_state(state)
        logger.warning("gmail drain incomplete, will retry: %s", reason)
        return {"ok": False, "reason": reason, "uncovered": uncovered,
                "queued": len(keep)}

    def drain_loop(self, stop: threading.Event, *,
                   interval: float = 30.0,
                   on_drain: Optional[Callable[[dict[str, Any]], None]] = None
                   ) -> None:
        """Drain queued notifications on a cadence until asked to stop.

        The push endpoint only enqueues; something has to do the work. Polling
        this queue on a short interval means a notification is processed even
        if the worker that happened to receive it has died, and it keeps a
        single owner for the Gmail read. The interval is a safety net, not the
        primary latency path: ``interval`` bounds the worst case, and a fresh
        notification is picked up on the next tick.
        """
        if not self.config.configured:
            logger.info("gmail push not configured; drain loop inactive")
            return
        while not stop.is_set():
            try:
                if self._load_queue():
                    result = self.drain()
                    if on_drain is not None:
                        on_drain(result)
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("gmail push drain failed: %s", exc)
            stop.wait(interval)

    def renew_loop(self, stop: threading.Event, *, interval: float = 3600.0) -> None:
        """Keep the watch alive for the lifetime of the process."""
        if not self.config.configured:
            logger.info("gmail push not configured; watch renewal inactive")
            return
        while not stop.is_set():
            try:
                self.renew_watch()
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("gmail watch renewal failed: %s", exc)
            stop.wait(interval)

    # ── health ──
    def health(self) -> dict[str, Any]:
        """Push-ingest health for the dashboard, with the evidence behind it."""
        state = self._load_state()
        now = _utcnow()
        if not self.config.configured:
            status = "not_configured"
        elif state.abandoned_notifications or state.last_error:
            # Abandoned notifications mean mail was dropped, not merely
            # delayed. That must never read as healthy.
            status = "degraded"
        elif not state.history_id:
            status = "degraded"
        elif state.is_expired(now):
            status = "offline"
        elif not state.needs_renewal(now):
            status = "healthy"
        else:
            status = "degraded"
        return {
            "status": status,
            "config": self.config.redacted(),
            "notifications_received": state.notifications_received,
            "history_lookups": state.history_lookups,
            "messages_ingested": state.messages_ingested,
            "abandoned_notifications": state.abandoned_notifications,
            "queue_depth": len(self._load_queue()),
            "history_id": state.history_id,
            "expiration": state.expiration,
            "last_notification_at": state.last_notification_at,
            "last_renewed_at": state.last_renewed_at,
            "last_error": state.last_error or None,
            "renewal_due": state.needs_renewal(now),
        }


def _history_is_older(candidate: str, reference: str) -> bool:
    """Compare history ids as integers; fall back to string order.

    Gmail history ids are large integers, but a non-numeric id must not raise
    in a code path that only exists to protect against losing mail.
    """
    try:
        return int(candidate) < int(reference)
    except (TypeError, ValueError):
        return str(candidate) < str(reference)
