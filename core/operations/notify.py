"""
core/operations/notify.py

IdentityOS NotificationManager: semantic notification events, policy-gated
delivery across channels, deduplication, provenance, and attention state.

Pipeline::

    Aster event
      → NotificationManager.create_event (semantic kind, dedup key, evidence)
      → notification policy (meaningful events only; routine telemetry never)
      → first-party Web Push (preferred once proven)
      → in-app notification center (always)
      → ntfy fallback (only when push unavailable/invalid and warranted)

Transport semantics are precise: a push-service acceptance is recorded as
``submitted``, never as delivered. Only the principal's own confirmation
records visibility. Restart never resends handled notifications: history and
channel attempts persist under ``operations.notify_events``.

Push payloads are minimal by policy (title, short body, deep link, badge):
private detail is fetched inside authenticated Aster Control, never pushed
through the browser vendor infrastructure.
"""

from __future__ import annotations

import enum
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .models import utcnow

logger = logging.getLogger("identityos.notify")

EVENTS_NAMESPACE = "operations.notify_events"
SUBS_NAMESPACE = "operations.push_subscriptions"
RECONCILE_NAMESPACE = "operations.notify_reconcile"

VAPID_SECRET_HANDLE = "webpush/vapid-private"
# VAPID contact claim sent to the push service with every request (transport
# metadata, never notification content). Apple rejected the .local mailto
# form (BadJwtToken with an otherwise valid signature); the Tailnet HTTPS
# origin identifies the operator without exposing any secret. The push
# service already sees request timing and the device endpoint; this adds
# only the operator's private hostname. Overridable per deployment.
VAPID_SUBJECT = os.environ.get(
    "ASTER_VAPID_SUBJECT", "https://idos.taile6cf93.ts.net"
)


class NotifyKind(str, enum.Enum):
    """Semantic notification taxonomy — only kinds with real sources."""

    EXTERNAL_REPLY = "external_reply"
    PRINCIPAL_DECISION_REQUIRED = "principal_decision_required"
    OUTREACH_SUBMITTED = "outreach_submitted"
    JOB_COMPLETED = "job_completed"
    JOB_APPROVAL_REQUIRED = "job_approval_required"
    SECURITY_ALERT = "security_alert"
    SYSTEM_DEGRADED = "system_degraded"
    TEST = "test"

    @classmethod
    def coerce(cls, value: Any) -> "NotifyKind":
        if isinstance(value, cls):
            return value
        try:
            return cls(str(value or "").strip().lower())
        except ValueError:
            raise ValueError(f"Unknown notification kind: {value!r}")


class NotifyImportance(str, enum.Enum):
    NORMAL = "normal"
    HIGH = "high"


@dataclass
class NotifyEvent:
    id: str = ""
    kind: str = NotifyKind.TEST.value
    identity_id: str = "aster"
    title: str = ""
    body: str = ""
    importance: str = NotifyImportance.NORMAL.value
    created_at: str = ""
    object_type: str = ""
    object_id: str = ""
    deep_link: str = "/status#/notifications"
    requires_attention: bool = False
    dedup_key: str = ""
    evidence_ref: str = ""
    channels: list[dict[str, Any]] = field(default_factory=list)
    read: bool = False
    read_at: Optional[str] = None
    resolved: bool = False
    resolved_at: Optional[str] = None
    principal_confirmed_visible: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "identity_id": self.identity_id,
            "title": self.title,
            "body": self.body,
            "importance": self.importance,
            "created_at": self.created_at,
            "object_type": self.object_type,
            "object_id": self.object_id,
            "deep_link": self.deep_link,
            "requires_attention": self.requires_attention,
            "dedup_key": self.dedup_key,
            "evidence_ref": self.evidence_ref,
            "channels": list(self.channels),
            "read": self.read,
            "read_at": self.read_at,
            "resolved": self.resolved,
            "resolved_at": self.resolved_at,
            "principal_confirmed_visible": self.principal_confirmed_visible,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "NotifyEvent":
        return cls(
            id=str(data.get("id", "")),
            kind=str(data.get("kind", NotifyKind.TEST.value)),
            identity_id=str(data.get("identity_id", "aster")),
            title=str(data.get("title", "")),
            body=str(data.get("body", "")),
            importance=str(data.get("importance", NotifyImportance.NORMAL.value)),
            created_at=str(data.get("created_at", "")),
            object_type=str(data.get("object_type", "")),
            object_id=str(data.get("object_id", "")),
            deep_link=str(data.get("deep_link", "/status#/notifications")),
            requires_attention=bool(data.get("requires_attention", False)),
            dedup_key=str(data.get("dedup_key", "")),
            evidence_ref=str(data.get("evidence_ref", "")),
            channels=list(data.get("channels", [])),
            read=bool(data.get("read", False)),
            read_at=data.get("read_at"),
            resolved=bool(data.get("resolved", False)),
            resolved_at=data.get("resolved_at"),
            principal_confirmed_visible=bool(data.get("principal_confirmed_visible", False)),
        )


def _new_id(prefix: str = "ne") -> str:
    import uuid

    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class NotificationManager:
    """Policy-gated semantic notifications for one operator identity."""

    def __init__(self, storage: Any, identity_id: str = "aster") -> None:
        self._storage = storage
        self.identity_id = identity_id

    # ── history ───────────────────────────────────────────────────────

    def _load_events(self) -> list[NotifyEvent]:
        raw = self._storage.load(self.identity_id, EVENTS_NAMESPACE) or {}
        return [NotifyEvent.from_dict(e) for e in raw.get("items", [])]

    def _save_events(self, events: list[NotifyEvent]) -> None:
        self._storage.save(self.identity_id, EVENTS_NAMESPACE, {
            "items": [e.to_dict() for e in events],
        })

    def list_events(self, *, limit: int = 50, unread_only: bool = False) -> list[NotifyEvent]:
        items = self._load_events()
        if unread_only:
            items = [e for e in items if not e.read]
        items.sort(key=lambda e: e.created_at or "", reverse=True)
        return items[: max(1, limit)]

    def get_event(self, event_id: str) -> Optional[NotifyEvent]:
        return next((e for e in self._load_events() if e.id == event_id), None)

    def find_unresolved(self, dedup_key: str) -> Optional[NotifyEvent]:
        """Return the unresolved event for a dedup key, if any."""
        if not dedup_key:
            return None
        return next(
            (e for e in self._load_events()
             if e.dedup_key == dedup_key and not e.resolved),
            None,
        )

    def create_event(
        self,
        kind: Any,
        *,
        title: str,
        body: str = "",
        importance: Any = NotifyImportance.NORMAL,
        object_type: str = "",
        object_id: str = "",
        deep_link: str = "/status#/notifications",
        requires_attention: bool = False,
        dedup_key: str = "",
        evidence_ref: str = "",
    ) -> NotifyEvent:
        """Create a semantic event, deduplicated by key.

        An unresolved event with the same non-empty dedup key is returned
        instead of creating a duplicate: one persistent blocker never yields
        200 notifications, and restarts never resend handled ones.
        """
        kind = NotifyKind.coerce(kind).value
        importance = (
            importance.value if isinstance(importance, NotifyImportance)
            else str(importance or "normal")
        )
        events = self._load_events()
        if dedup_key:
            for existing in events:
                if existing.dedup_key == dedup_key and not existing.resolved:
                    return existing
        event = NotifyEvent(
            id=_new_id(),
            kind=kind,
            identity_id=self.identity_id,
            title=title[:120],
            body=body[:500],
            importance=importance,
            created_at=utcnow().isoformat(),
            object_type=object_type[:60],
            object_id=object_id[:120],
            deep_link=deep_link[:200] if deep_link.startswith("/") else "/status#/notifications",
            requires_attention=bool(requires_attention),
            dedup_key=dedup_key[:200],
            evidence_ref=evidence_ref[:200],
        )
        events.append(event)
        self._save_events(events)
        return event

    def record_channel(
        self, event_id: str, channel: str, result: str, *, detail: str = ""
    ) -> Optional[NotifyEvent]:
        events = self._load_events()
        for event in events:
            if event.id == event_id:
                event.channels.append({
                    "channel": channel,
                    "result": result,
                    "detail": detail[:200],
                    "at": utcnow().isoformat(),
                })
                self._save_events(events)
                return event
        return None

    def mark_read(self, event_id: str) -> bool:
        events = self._load_events()
        for event in events:
            if event.id == event_id and not event.read:
                event.read = True
                event.read_at = utcnow().isoformat()
                self._save_events(events)
                return True
        return False

    def mark_viewed(self) -> dict[str, int]:
        """Clear notifications once viewed (principal opened the center).

        Every unread event becomes read. TEST-kind events additionally
        resolve: their entire purpose is being seen, so a viewed test is a
        completed test. All other kinds stay unresolved until their
        underlying matter is genuinely handled — viewing is not handling.
        Returns counts for observability.
        """
        events = self._load_events()
        read = resolved = 0
        dirty = False
        for event in events:
            if not event.read:
                event.read = True
                event.read_at = utcnow().isoformat()
                read += 1
                dirty = True
            if event.kind == NotifyKind.TEST.value and not event.resolved:
                event.resolved = True
                event.resolved_at = utcnow().isoformat()
                resolved += 1
                dirty = True
        if dirty:
            self._save_events(events)
        return {"read": read, "resolved": resolved}

    def resolve(self, event_id: str, resolution: str = "") -> bool:
        events = self._load_events()
        for event in events:
            if event.id == event_id and not event.resolved:
                event.resolved = True
                event.resolved_at = utcnow().isoformat()
                if resolution:
                    event.channels.append({
                        "channel": "resolution",
                        "result": resolution[:200],
                        "detail": "",
                        "at": utcnow().isoformat(),
                    })
                self._save_events(events)
                return True
        return False

    def confirm_visible(self, event_id: str) -> bool:
        """Record the principal's own confirmation of visibility.

        This is the principal's word, not cryptographic delivery evidence,
        and it is stored as exactly that.
        """
        events = self._load_events()
        for event in events:
            if event.id == event_id:
                event.principal_confirmed_visible = True
                if not event.read:
                    event.read = True
                    event.read_at = utcnow().isoformat()
                self._save_events(events)
                return True
        return False

    # ── attention + badge (derived from real state, restart-safe) ─────

    def attention_count(self, store: Any = None) -> int:
        """Items genuinely requiring the principal's attention right now.

        Only UNREAD, unresolved attention events count: once Arsène has
        viewed the notification center, seen items stop badging. Derived
        live blockers (authorizations, approvals) clear only when their
        underlying state clears — viewing cannot wish away a real blocker.
        """
        return len(self.attention_items(store, unread_only=True))

    def attention_items(self, store: Any = None, *, unread_only: bool = False) -> list[dict[str, Any]]:
        """Attention list: unresolved attention events + live blockers.

        Live blockers (pending authorizations, permission-required principal
        messages) clear automatically when the underlying state clears — the
        badge never sticks on stale telemetry.
        """
        items: list[dict[str, Any]] = []
        for event in self._load_events():
            if event.requires_attention and not event.resolved:
                if unread_only and event.read:
                    continue
                items.append({
                    "id": event.id,
                    "kind": event.kind,
                    "title": event.title,
                    "at": event.created_at,
                    "deep_link": event.deep_link,
                    "source": "event",
                })
        if store is not None:
            try:
                from .models import MessageStatus

                pending_auth = sum(
                    1 for m in store.list_messages()
                    if m.status is MessageStatus.AWAITING_AUTHORIZATION
                )
                if pending_auth:
                    items.append({
                        "id": "derived:pending-authorizations",
                        "kind": NotifyKind.PRINCIPAL_DECISION_REQUIRED.value,
                        "title": f"{pending_auth} outreach awaiting your authorization",
                        "at": "",
                        "deep_link": "/status#/messages",
                        "source": "derived",
                    })
                perm_required = sum(
                    1 for m in store.list_messages()
                    if m.status is MessageStatus.PERMISSION_REQUIRED
                )
                if perm_required:
                    items.append({
                        "id": "derived:permission-required",
                        "kind": NotifyKind.PRINCIPAL_DECISION_REQUIRED.value,
                        "title": f"{perm_required} instruction(s) need your approval",
                        "at": "",
                        "deep_link": "/status#/messages",
                        "source": "derived",
                    })
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("attention derivation failed: %s", exc)
        return items

    # ── policy ────────────────────────────────────────────────────────

    #: Telemetry that must NEVER become a notification.
    SILENT_CATEGORIES = frozenset({
        "heartbeat", "tick", "observation", "polling", "waiting",
        "no_change", "commons_poll", "would_send",
    })

    def policy_allows(self, kind: Any, *, importance: Any = NotifyImportance.NORMAL) -> bool:
        """True when this kind may notify. Routine telemetry never may."""
        try:
            NotifyKind.coerce(kind)
        except ValueError:
            return False
        return True

    def check_silent(self, category: str) -> bool:
        """True when the category must stay silent (heartbeat/ticks/etc.)."""
        return str(category or "").strip().lower() in self.SILENT_CATEGORIES

    # ── subscriptions ─────────────────────────────────────────────────

    def _load_subs(self) -> list[dict[str, Any]]:
        raw = self._storage.load(self.identity_id, SUBS_NAMESPACE) or {}
        items = raw.get("items", [])
        return [dict(s) for s in items if isinstance(s, dict)]

    def _save_subs(self, subs: list[dict[str, Any]]) -> None:
        self._storage.save(self.identity_id, SUBS_NAMESPACE, {"items": subs})

    def register_subscription(
        self, subscription: dict[str, Any], *, principal_login: str, device_label: str = ""
    ) -> dict[str, Any]:
        """Create or replace a push subscription. Validates shape strictly."""
        endpoint = str(subscription.get("endpoint", "") or "")
        keys = subscription.get("keys") or {}
        p256dh = str(keys.get("p256dh", "") or "")
        auth = str(keys.get("auth", "") or "")
        if not endpoint.startswith("https://") or len(endpoint) > 2048:
            raise ValueError("invalid subscription endpoint")
        if not p256dh or not auth or len(p256dh) > 256 or len(auth) > 128:
            raise ValueError("invalid subscription keys")
        subs = [s for s in self._load_subs() if s.get("endpoint") != endpoint]
        record = {
            "endpoint": endpoint,
            "keys": {"p256dh": p256dh, "auth": auth},
            "principal_login": principal_login,
            "device_label": device_label[:120],
            "created_at": utcnow().isoformat(),
            "last_verified": utcnow().isoformat(),
            "failures": 0,
            "stale": False,
        }
        subs.append(record)
        self._save_subs(subs)
        return {"endpoint": endpoint, "verified": True, "stale": False}

    def remove_subscription(self, endpoint: str) -> bool:
        subs = self._load_subs()
        kept = [s for s in subs if s.get("endpoint") != endpoint]
        if len(kept) == len(subs):
            return False
        self._save_subs(kept)
        return True

    def valid_subscriptions(self) -> list[dict[str, Any]]:
        now = time.time()
        live: list[dict[str, Any]] = []
        for sub in self._load_subs():
            if sub.get("stale"):
                continue
            if int(sub.get("failures", 0) or 0) >= 3:
                continue
            try:
                verified_age = now - _parse_ts(sub.get("last_verified", ""))
            except (ValueError, TypeError):
                verified_age = 0
            if verified_age > 90 * 86400:
                continue
            live.append(sub)
        return live

    def mark_push_result(
        self, endpoint: str, ok: bool, *, expired: bool = False, detail: str = ""
    ) -> None:
        subs = self._load_subs()
        for sub in subs:
            if sub.get("endpoint") != endpoint:
                continue
            if ok:
                sub["failures"] = 0
                sub["stale"] = False
                sub["last_verified"] = utcnow().isoformat()
            elif expired:
                sub["stale"] = True
            else:
                sub["failures"] = int(sub.get("failures", 0) or 0) + 1
            sub["last_result"] = detail[:200]
        self._save_subs(subs)

    # ── VAPID ─────────────────────────────────────────────────────────

    def ensure_vapid(self, secret_store: Any) -> tuple[str, str]:
        """Return (public_app_key_b64url, private_pem). Generates once.

        The private key lives ONLY in the secret store (gitignored private
        state). The public key is protocol-required browser material.
        Generation is file-lock guarded: two concurrent first calls must not
        produce two keypairs (the loser would orphan whichever subscription
        was created under its key).
        """
        import fcntl

        lock_path = None
        lock_fd = None
        try:
            root = getattr(secret_store, "root", None)
            lock_path = (root() if callable(root) else root)
        except Exception:
            lock_path = None
        if lock_path is not None:
            try:
                from pathlib import Path

                lock_dir = Path(str(lock_path))
                lock_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
                lock_fd = open(lock_dir / ".webpush.lock", "w")
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
            except Exception:
                lock_fd = None
        try:
            existing = secret_store.get(VAPID_SECRET_HANDLE)
            if existing:
                pem = existing
            else:
                from py_vapid import Vapid

                vapid = Vapid()
                vapid.generate_keys()
                pem = vapid.private_pem() if isinstance(vapid.private_pem(), str) else vapid.private_pem().decode()
                secret_store.put(VAPID_SECRET_HANDLE, pem)
                # Re-read: a concurrent generator may have won the race; the
                # stored key is authoritative, never our in-memory copy.
                pem = secret_store.get(VAPID_SECRET_HANDLE) or pem
            return _public_app_key(pem), pem
        finally:
            try:
                if lock_fd is not None:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                    lock_fd.close()
            except Exception:
                pass

    # ── sending ───────────────────────────────────────────────────────

    #: Secret-shaped content must never cross the vendor push path.
    _SECRET_HINTS = (
        "password", "api_key", "apikey", "api-key", "bearer ", "secret",
        "credential", "private key", "ntfy.sh/",
    )

    def _voice_checked_payload(self, event: NotifyEvent, *, badge: int = 0) -> Optional[dict[str, Any]]:
        """Build the push payload, refusing (recorded) rather than sending
        anything containing U+2014. Never raises: notification delivery must
        not crash the operator tick."""
        from .voice import repair_outbound, validate_outbound

        payload = self.push_payload(event, badge=badge)
        if validate_outbound(payload["title"], payload["body"]).ok:
            return payload
        repaired_title, title_clean = repair_outbound(payload["title"])
        repaired_body, body_clean = repair_outbound(payload["body"])
        if title_clean and body_clean:
            payload["title"], payload["body"] = repaired_title, repaired_body
            return payload
        self.record_channel(event.id, "webpush", "rejected",
                            detail="style_violation: no-em-dash invariant")
        return None

    def push_payload(self, event: NotifyEvent, *, badge: int = 0) -> dict[str, Any]:
        """Minimal push payload: title, short body, deep link, badge.

        Private detail stays behind authenticated Aster Control. Bodies are
        capped and scanned: anything secret-shaped is replaced with a generic
        attention line — the detail is fetched inside the app instead.
        """
        body = (event.body or "")[:180]
        lowered = body.lower()
        if any(hint in lowered for hint in self._SECRET_HINTS):
            body = "Aster needs your attention. Open Aster Control for details."
        return {
            "title": event.title[:80] or "Aster",
            "body": body,
            "tag": event.id,
            "deep_link": event.deep_link,
            "kind": event.kind,
            "badge": max(0, int(badge)),
        }

    def send_first_party(
        self,
        event: NotifyEvent,
        *,
        secret_store: Any,
        badge: int = 0,
        sender: Callable[..., Any] | None = None,
    ) -> dict[str, Any]:
        """Attempt first-party Web Push to all valid subscriptions.

        Returns transport-semantics outcome: accepted / rejected / expired /
        no_subscription. Never claims delivery. Stale subscriptions are
        marked, never hammered.
        """
        from pywebpush import WebPushException, webpush

        sender = sender or webpush
        subs = self.valid_subscriptions()
        if not subs:
            return {"channel": "webpush", "result": "no_subscription", "attempted": 0}
        _, private_pem = self.ensure_vapid(secret_store)
        # Pass a Vapid instance: pywebpush honors Vapid01 subclasses directly
        # (py_vapid.Vapid subclasses Vapid01). Passing the PEM string instead
        # routes through from_string, which cannot parse PEM-with-headers.
        vapid = _vapid_from_pem(private_pem)
        import json as _json

        payload = self._voice_checked_payload(event, badge=badge)
        if payload is None:
            return {"channel": "webpush", "result": "rejected",
                    "detail": "style_violation: no-em-dash invariant"}
        payload = _json.dumps(payload)
        accepted = rejected = expired = 0
        last_detail = ""
        for sub in subs:
            try:
                sender(
                    {"endpoint": sub["endpoint"], "keys": dict(sub.get("keys") or {})},
                    payload,
                    vapid_private_key=vapid,
                    vapid_claims={"sub": VAPID_SUBJECT},
                    timeout=15,
                )
                self.mark_push_result(sub["endpoint"], True)
                self.record_channel(event.id, "webpush", "submitted",
                                    detail=sub["endpoint"][:80])
                accepted += 1
            except Exception as exc:
                status = getattr(exc, "response", None) is not None and getattr(exc.response, "status_code", None)
                is_expired = status in (404, 410)
                self.mark_push_result(sub["endpoint"], False, expired=is_expired,
                                      detail=str(exc)[:200])
                self.record_channel(
                    event.id, "webpush", "expired" if is_expired else "rejected",
                    detail=str(exc)[:200],
                )
                if is_expired:
                    expired += 1
                else:
                    rejected += 1
                last_detail = str(exc)[:200]
        if accepted:
            result = "submitted"
        elif expired and not rejected:
            result = "expired"
        else:
            result = "rejected"
        return {"channel": "webpush", "result": result, "attempted": len(subs),
                "accepted": accepted, "rejected": rejected, "expired": expired,
                "detail": last_detail}

    def send_ntfy_fallback(
        self, event: NotifyEvent, registry: Any, *, sender: Callable[..., Any] | None = None
    ) -> dict[str, Any]:
        """Fallback through the installed notification capability.

        The topic stays inside the capability: only success/failure and the
        returned message id are observed here — never the topic itself.
        """
        from .voice import repair_outbound, validate_outbound

        title = event.title[:120] or "Aster"
        message = (event.body or "")[:500]
        if not validate_outbound(title, message).ok:
            repaired_title, title_clean = repair_outbound(title)
            repaired_message, message_clean = repair_outbound(message)
            if title_clean and message_clean:
                title, message = repaired_title, repaired_message
            else:
                self.record_channel(event.id, "ntfy", "rejected",
                                    detail="style_violation: no-em-dash invariant")
                return {"channel": "ntfy", "result": "rejected",
                        "detail": "style_violation: no-em-dash invariant"}
        params = {
            "title": title,
            "message": message,
            "priority": 5 if event.importance == NotifyImportance.HIGH.value else 4,
            "tags": ["identityos", "aster", event.kind],
        }
        try:
            if sender is not None:
                result = sender("aster", "notification.send", **params)
            else:
                result = registry.call("aster", "notification.send", **params)
        except Exception as exc:
            self.record_channel(event.id, "ntfy", "failed", detail=str(exc)[:200])
            return {"channel": "ntfy", "result": "failed", "detail": str(exc)[:200]}
        ok = bool(getattr(result, "success", False))
        data = getattr(result, "data", None) or {}
        self.record_channel(
            event.id, "ntfy", "submitted" if ok else "rejected",
            detail=str((getattr(result, "error", None) or data.get("ntfy_id", "")))[:200],
        )
        return {"channel": "ntfy", "result": "submitted" if ok else "rejected"}


def reconcile(
    engine: Any,
    *,
    vapid_sender: Callable[..., Any] | None = None,
    ntfy_sender: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Create semantic events from real operator state, then send new ones.

    Called at the end of an operator tick (guarded: needs a secret store).
    Watermarks + dedup keys make it restart-safe and spam-free: one persistent
    blocker yields one event, never 200. TEST-kind events are never auto-sent;
    routine telemetry never becomes an event at all.
    """
    store = engine.store
    manager = NotificationManager(store._storage, store.identity_id)
    state = store._storage.load(store.identity_id, RECONCILE_NAMESPACE) or {}
    created: list[str] = []

    def _note(kind: NotifyKind, **kwargs: Any) -> None:
        existing = manager.find_unresolved(str(kwargs.get("dedup_key", "") or ""))
        if existing is not None:
            return
        event = manager.create_event(kind, **kwargs)
        created.append(event.id)

    state.setdefault("started_at", utcnow().isoformat())

    # Escalations awaiting the principal (ledger → attention event).
    for note in store.list_notifications(unread_only=True):
        if note.kind not in ("escalation", "principal_instruction"):
            continue
        ref = (note.refs or {}).get("message_id", "")
        _note(
            NotifyKind.PRINCIPAL_DECISION_REQUIRED,
            title="Aster needs your approval",
            body=str(note.summary or "")[:200],
            importance=NotifyImportance.HIGH,
            object_type="notification",
            object_id=str(ref or note.kind),
            deep_link="/status#/messages",
            requires_attention=True,
            dedup_key=f"ledger:{note.kind}:{ref or note.summary}",
            evidence_ref="operations.notifications",
        )

    # Substantive inbound replies since last reconcile.
    last_reply = state.get("last_reply_seen", "")
    newest_reply = last_reply
    for rel in store.list_relationships():
        if getattr(rel, "opted_out", False):
            continue
        inbound_at = getattr(rel, "last_inbound_at", None) or ""
        if inbound_at and inbound_at > last_reply:
            newest_reply = max(newest_reply, inbound_at)
            _note(
                NotifyKind.EXTERNAL_REPLY,
                title=f"Reply from {rel.display_name or 'a contact'}",
                body=f"{rel.display_name or 'A contact'} replied — open Messages to read it.",
                object_type="relationship",
                object_id=rel.id,
                deep_link="/status#/messages",
                requires_attention=True,
                dedup_key=f"reply:{rel.id}:{inbound_at}",
                evidence_ref=f"relationship:{rel.id}",
            )
    state["last_reply_seen"] = newest_reply

    # Presence degradation transitions (material only).
    try:
        presence_raw = store._storage.load(store.identity_id, "operations.presence") or {}
        current = str(presence_raw.get("status", "") or "")
    except Exception:
        current = ""
    previous = str(state.get("last_presence_status", "") or "")
    if current != previous:
        state["last_presence_status"] = current
        if current == "degraded":
            _note(
                NotifyKind.SYSTEM_DEGRADED,
                title="Aster is degraded",
                body=str(presence_raw.get("activity", "") or "A subsystem needs attention.")[:200],
                importance=NotifyImportance.HIGH,
                deep_link="/status#/notifications",
                requires_attention=True,
                dedup_key=f"health:degraded:{utcnow().isoformat()[:13]}",
                evidence_ref="operations.presence",
            )

    # Delegated services jobs (read-only; never mutates services state).
    _reconcile_services_jobs(manager, store, state, _note)

    store._storage.save(store.identity_id, RECONCILE_NAMESPACE, state)

    # Send pass: new attention events go first-party; ntfy only as fallback.
    sent: list[dict[str, Any]] = []
    secret_store = getattr(engine, "_secret_store", None)
    registry = getattr(engine, "_capability_registry", None)
    if secret_store is not None:
        for event_id in created:
            event = manager.get_event(event_id)
            if event is None or event.kind == NotifyKind.TEST.value:
                continue
            if not event.requires_attention:
                continue
            if any(c.get("channel") == "webpush" and c.get("result") == "submitted"
                   for c in event.channels):
                continue
            outcome = manager.send_first_party(
                event, secret_store=secret_store,
                badge=manager.attention_count(store), sender=vapid_sender,
            )
            sent.append({"event": event_id, **outcome})
            if outcome["result"] in ("no_subscription", "expired", "rejected"):
                if event.importance == NotifyImportance.HIGH.value and registry is not None:
                    fallback = manager.send_ntfy_fallback(event, registry, sender=ntfy_sender)
                    sent.append({"event": event_id, **fallback})
    return {"created": created, "sent": sent}


def _reconcile_services_jobs(
    manager: NotificationManager, store: Any, state: dict, note: Callable[..., None]
) -> None:
    try:
        from core.services.integration import existing_store
    except ImportError:
        return
    try:
        services = existing_store(store._storage)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("services reconcile unavailable: %s", exc)
        return
    if services is None:
        return
    try:
        identity = store.identity_id
        seen_seq = int(state.get("services_seq", 0) or 0)
        newest = seen_seq
        for row in services.rows(
            "SELECT seq,kind,job,created FROM events WHERE identity=? AND seq>? ORDER BY seq ASC LIMIT 100",
            (identity, seen_seq),
        ):
            newest = max(newest, int(row["seq"]))
            kind = str(row["kind"] or "")
            job = str(row["job"] or "")
            if kind == "quote_issued" and job:
                note(
                    NotifyKind.JOB_APPROVAL_REQUIRED,
                    title="Engineer work needs your approval",
                    body=f"A quote is awaiting your decision (job {job[:8]}…).",
                    importance=NotifyImportance.HIGH,
                    object_type="job",
                    object_id=job,
                    deep_link="/status#/work",
                    requires_attention=True,
                    dedup_key=f"jobquote:{job}",
                    evidence_ref=f"services:events:{row['seq']}",
                )
            elif kind in ("artifact_delivered", "settled") and job:
                note(
                    NotifyKind.JOB_COMPLETED,
                    title="Engineer work completed",
                    body=f"Job {job[:8]}… completed.",
                    object_type="job",
                    object_id=job,
                    deep_link="/status#/work",
                    requires_attention=False,
                    dedup_key=f"jobdone:{job}",
                    evidence_ref=f"services:events:{row['seq']}",
                )
            elif kind in ("worker_blocked",) and job:
                note(
                    NotifyKind.SYSTEM_DEGRADED,
                    title="Delegated work is blocked",
                    body=f"Job {job[:8]}… is blocked and may need intervention.",
                    importance=NotifyImportance.HIGH,
                    object_type="job",
                    object_id=job,
                    deep_link="/status#/work",
                    requires_attention=True,
                    dedup_key=f"jobblocked:{job}",
                    evidence_ref=f"services:events:{row['seq']}",
                )
        state["services_seq"] = newest
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("services job scan failed: %s", exc)


def _parse_ts(value: Any) -> float:
    from datetime import datetime, timezone

    parsed = datetime.fromisoformat(str(value or ""))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _vapid_from_pem(private_pem: str) -> Any:
    from py_vapid import Vapid

    raw_pem = private_pem.encode("utf-8") if isinstance(private_pem, str) else private_pem
    return Vapid.from_pem(raw_pem)


def _public_app_key(private_pem: str) -> str:
    from cryptography.hazmat.primitives import serialization
    from py_vapid.utils import b64urlencode

    vapid = _vapid_from_pem(private_pem)
    raw = vapid.public_key.public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    encoded = b64urlencode(raw)
    return encoded.decode() if isinstance(encoded, bytes) else encoded
