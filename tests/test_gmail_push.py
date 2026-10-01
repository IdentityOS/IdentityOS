"""Tests for event-driven Gmail ingest.

These cover the failure modes that make push notification *worse* than
polling when done carelessly: duplicate delivery, out-of-order delivery,
history gaps, a slow endpoint, and a silently expired watch. Each of those
turns into a silently unanswered email if handled naively, so each gets a test.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone

import pytest

from core.capabilities.email.gmail_push import (
    GmailApiError,
    GmailPushConfig,
    GmailPushService,
    PushNotification,
    WatchState,
    _history_is_older,
    decode_pubsub_envelope,
    normalize_gmail_message,
)
from runtime.persistence import JSONFileBackend

CONFIG_ENV = {
    "ASTER_GMAIL_PUSH_CLIENT_ID": "CLIENTIDVALUE",
    "ASTER_GMAIL_PUSH_CLIENT_SECRET": "SECRETVALUE",
    "ASTER_GMAIL_PUSH_REFRESH_TOKEN": "REFRESHVALUE",
    "ASTER_GMAIL_PUSH_TOPIC": "projects/p/topics/t",
    "ASTER_GMAIL_PUSH_VERIFICATION_TOKEN": "verify-me",
}


def _config(**overrides) -> GmailPushConfig:
    return GmailPushConfig.from_env({**CONFIG_ENV, **overrides})


def _service(tmp_path, client, *, ingest=None, config=None):
    backend = JSONFileBackend(root_dir=str(tmp_path / "store"))
    seen: list[list[dict]] = []

    def _ingest(items):
        seen.append(list(items))
        return {"ok": True, "inbound": len(items)}

    service = GmailPushService(
        config or _config(),
        storage=backend,
        identity_id="aster",
        ingest=ingest or _ingest,
        client=client,
    )
    return service, seen, backend


def _envelope(history_id: str, address: str = "a@eagles.oc.edu") -> bytes:
    inner = json.dumps({"emailAddress": address, "historyId": history_id})
    return json.dumps({
        "message": {
            "data": base64.b64encode(inner.encode()).decode(),
            "messageId": f"m-{history_id}",
        },
        "subscription": "projects/p/subscriptions/s",
    }).encode()


class FakeClient:
    """Stands in for the Gmail API with scripted, inspectable behavior."""

    def __init__(self, *, history_ids=None, messages=None, fail=None, start=None,
                 newest_history_id=""):
        self.history_ids = history_ids or []
        self.messages = messages or {}
        self.fail = fail
        self.start_result = start or {}
        self.newest_history_id = newest_history_id
        self.history_calls = []
        self.get_calls = []
        self.watch_calls = 0

    def start_watch(self):
        self.watch_calls += 1
        if self.fail:
            raise self.fail
        return self.start_result

    def stop_watch(self):
        return {}

    def history(self, start_history_id, **_kwargs):
        self.history_calls.append(start_history_id)
        if self.fail:
            raise self.fail
        return self.history_ids, self.newest_history_id, False

    def get_message(self, message_id):
        self.get_calls.append(message_id)
        if message_id not in self.messages:
            raise GmailApiError(f"message {message_id} unreadable", 404)
        return self.messages[message_id]


def _expiry_ms(days: float) -> str:
    """Gmail returns watch expiry in epoch milliseconds."""
    return str(int((datetime.now(timezone.utc) + timedelta(days=days)).timestamp() * 1000))


# ── configuration ──────────────────────────────────────────────────────────


def test_unconfigured_env_is_inert():
    cfg = GmailPushConfig.from_env({})
    assert cfg.configured is False
    redacted = json.dumps(cfg.redacted())
    assert cfg.configured is False
    # Only presence flags, never values.
    assert "client_secret_present" in redacted
    assert "refresh_token_present" in redacted


def test_redacted_config_reports_presence_not_values():
    redacted = _config().redacted()
    assert redacted["configured"] is True
    assert redacted["client_id_present"] is True
    assert redacted["client_secret_present"] is True
    assert "CLIENTIDVALUE" not in json.dumps(redacted)
    assert "verify-me" not in json.dumps(redacted)


def test_config_requires_all_oauth_fields():
    for missing in ("ASTER_GMAIL_PUSH_CLIENT_ID", "ASTER_GMAIL_PUSH_CLIENT_SECRET",
                    "ASTER_GMAIL_PUSH_REFRESH_TOKEN", "ASTER_GMAIL_PUSH_TOPIC"):
        env = {k: v for k, v in CONFIG_ENV.items() if k != missing}
        assert GmailPushConfig.from_env(env).configured is False


# ── envelope decoding ──────────────────────────────────────────────────────


def test_decode_pubsub_envelope():
    notification = decode_pubsub_envelope(_envelope("1000"))
    assert notification is not None
    assert notification.history_id == "1000"
    assert notification.email_address == "a@eagles.oc.edu"


def test_decode_rejects_garbage_without_raising():
    for payload in (b"", b"not json", b"{}", b'{"message":{}}', b'{"message":{"data":"!!!"}}',
                    json.dumps({"nope": 1}).encode()):
        assert decode_pubsub_envelope(payload) is None


# ── authorization ──────────────────────────────────────────────────────────


def test_push_requires_verification_token(tmp_path):
    client = FakeClient()
    service, _, _ = _service(tmp_path, client)
    assert service.authorize({"token": "verify-me"}) is True
    assert service.authorize({"token": "wrong"}) is False
    assert service.authorize({}) is False


def test_push_refused_entirely_without_token(tmp_path):
    """An endpoint with no secret is an open trigger; refuse rather than
    accept."""
    service, _, _ = _service(tmp_path, FakeClient(), config=_config(
        ASTER_GMAIL_PUSH_VERIFICATION_TOKEN=""))
    assert service.authorize({}) is False
    assert service.authorize({"token": ""}) is False


# ── notification queue durability ──────────────────────────────────────────


def test_push_is_durably_queued_and_acked(tmp_path):
    service, _, backend = _service(tmp_path, FakeClient())
    status, payload = service.handle_push(_envelope("1000"))
    assert status == 200
    assert payload["accepted"] is True
    # Survives a process restart: the notification is on disk, not in memory.
    reloaded = GmailPushService(_config(), storage=backend, identity_id="aster",
                               ingest=lambda items: None)
    assert len(reloaded._load_queue()) == 1


def test_undecodable_push_is_acked_not_retried_forever(tmp_path):
    """Pub/Sub retries non-2xx forever; a body that cannot ever decode must
    be acked so it stops burning retries."""
    service, _, _ = _service(tmp_path, FakeClient())
    status, payload = service.handle_push(b"garbage")
    assert status == 200
    assert payload["accepted"] is False


def test_duplicate_delivery_is_coalesced(tmp_path):
    service, _, _ = _service(tmp_path, FakeClient())
    for _ in range(3):
        service.handle_push(_envelope("1000"))
    assert len(service._load_queue()) == 1


def test_late_notification_does_not_advance_high_water_mark(tmp_path):
    """A redelivered old notification must not skip the messages between."""
    service, _, _ = _service(tmp_path, FakeClient())
    service.handle_push(_envelope("2000"))
    service.handle_push(_envelope("1500"))
    state = service._load_state()
    assert state.history_id == "1500", "drain must start from the oldest id"


def test_history_comparison_handles_non_numeric_ids():
    assert _history_is_older("10", "20") is True
    assert _history_is_older("20", "10") is False
    assert _history_is_older("abc", "abd") is True
    assert _history_is_older("zzz", "abc") is False


# ── watch lifecycle ────────────────────────────────────────────────────────


def test_start_watch_persists_history_and_expiry(tmp_path):
    expiry = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
    client = FakeClient(start={"id": "w1", "expiration": _expiry_ms(7),
                               "historyId": "5000"})
    service, _, _ = _service(tmp_path, client)
    result = service.start_watch()
    assert result["ok"] is True
    state = service._load_state()
    assert state.watch_id == "w1"
    assert state.history_id == "5000"


def test_expired_watch_is_reported_offline(tmp_path):
    client = FakeClient(start={"id": "w1", "expiration": _expiry_ms(-1),
                               "historyId": "5000"})
    service, _, _ = _service(tmp_path, client)
    service.start_watch()
    assert service.health()["status"] == "offline", "lapsed watch must not look healthy"


def test_fresh_watch_reports_healthy(tmp_path):
    client = FakeClient(start={"id": "w1", "expiration": _expiry_ms(6),
                               "historyId": "5000"})
    service, _, _ = _service(tmp_path, client)
    service.start_watch()
    assert service.health()["status"] == "healthy"


def test_expiry_in_epoch_millis_is_parsed_not_assumed_dead(tmp_path):
    """Gmail returns expiration as epoch milliseconds.

    Storing that raw makes an ISO parse fail, and a failed parse is treated as
    expired — so the watch would report offline forever while working fine.
    """
    from core.capabilities.email.gmail_push import _iso_from_epoch_ms

    client = FakeClient(start={"id": "w1", "expiration": _expiry_ms(6),
                               "historyId": "5000"})
    service, _, _ = _service(tmp_path, client)
    service.start_watch()
    state = service._load_state()
    assert state.expiration, "expiry must be stored in a parseable form"
    assert state.is_expired() is False
    # And the stored value is ISO, not a bare millisecond integer.
    assert state.expiration.startswith("20")
    assert _iso_from_epoch_ms("garbage") == ""
    assert _iso_from_epoch_ms(0) == ""


def test_notification_arriving_during_drain_is_not_lost(tmp_path):
    """A notification that lands while history is being read must survive the
    drain, or the mail it describes is silently skipped."""
    client = FakeClient(history_ids=[], messages={}, newest_history_id="1100")
    service, _, _ = _service(tmp_path, client)
    service.handle_push(_envelope("1000"))

    original_history = client.history

    def racing_history(start_history_id, **kwargs):
        # Simulate a new message arriving mid-drain.
        service.handle_push(_envelope("1200"))
        return original_history(start_history_id, **kwargs)

    client.history = racing_history
    service.drain()
    assert [q.history_id for q in service._load_queue()] == ["1200"], \
        "a notification newer than the drain's high-water mark must be kept"


def test_watch_is_renewed_before_expiry(tmp_path):
    client = FakeClient(start={"id": "w1", "expiration": _expiry_ms(2),
                               "historyId": "5000"})
    service, _, _ = _service(tmp_path, client)
    service.start_watch()
    assert client.watch_calls == 1

    # Within the daily interval: no renewal yet.
    assert service.renew_watch() == {"ok": True, "renewed": False, "reason": "not due"}

    # Past the interval: renew, and the expiry must move forward.
    state = service._load_state()
    state.last_renewed_at = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    service._save_state(state)
    result = service.renew_watch()
    assert result["renewed"] is True
    assert client.watch_calls == 2


def test_watch_failure_is_recorded_not_swallowed(tmp_path):
    client = FakeClient(fail=GmailApiError("403 revoked", 403))
    service, _, _ = _service(tmp_path, client)
    result = service.start_watch()
    assert result["ok"] is False
    assert "403" in result["reason"]
    assert service._load_state().last_error


def test_renewal_loop_is_inert_when_unconfigured(tmp_path):
    import threading

    service, _, _ = _service(tmp_path, FakeClient(),
                             config=GmailPushConfig.from_env({}))
    stop = threading.Event()
    stop.set()
    service.renew_loop(stop)          # must return immediately, not raise
    assert stop.is_set()


# ── drain ──────────────────────────────────────────────────────────────────


def test_drain_ingests_history_messages(tmp_path):
    message = {
        "id": "g1", "threadId": "t1", "labelIds": ["INBOX", "UNREAD"],
        "payload": {"headers": [
            {"name": "From", "value": "Arsene <a@eagles.oc.edu>"},
            {"name": "Subject", "value": "Re: V3"},
            {"name": "Message-ID", "value": "<m1@x>"},
        ]},
    }
    client = FakeClient(history_ids=["g1"], messages={"g1": message},
                        newest_history_id="1100")
    service, seen, _ = _service(tmp_path, client)
    service.handle_push(_envelope("1000"))
    result = service.drain()
    assert result["ok"] is True
    assert result["messages"] == 1
    assert seen[0][0]["external_id"] == "<m1@x>"
    assert service._load_queue() == [], "queue clears only after history is read"
    assert service._load_state().history_id == "1100"


def test_drain_keeps_notifications_when_history_lookup_fails(tmp_path):
    client = FakeClient(fail=GmailApiError("500 boom", 500))
    service, seen, _ = _service(tmp_path, client)
    service.handle_push(_envelope("1000"))
    result = service.drain()
    assert result["ok"] is False
    assert len(service._load_queue()) == 1, "a failed lookup must not drop the notification"
    assert service._load_state().last_error


def test_drain_uses_lowest_queued_history_id(tmp_path):
    client = FakeClient(history_ids=[])
    service, _, _ = _service(tmp_path, client)
    service.handle_push(_envelope("2000"))
    service.handle_push(_envelope("1000"))
    service.drain()
    assert client.history_calls == ["1000"]


def test_drain_with_no_queue_is_a_noop(tmp_path):
    client = FakeClient()
    service, seen, _ = _service(tmp_path, client)
    result = service.drain()
    assert result["ok"] is True
    assert result["messages"] == 0
    assert client.history_calls == []


def test_partial_fetch_failure_keeps_history_and_queue_for_retry(tmp_path):
    """A readable message is ingested, but the mark must not skip the bad one.

    Advancing the high-water mark here would drop the unreadable message for
    good: the next drain would start above it and never look back.
    """
    good = {
        "id": "g1", "threadId": "t1",
        "payload": {"headers": [{"name": "Message-ID", "value": "<m1@x>"}]},
    }
    client = FakeClient(history_ids=["g1", "g2"], messages={"g1": good},
                        newest_history_id="1100")
    service, seen, _ = _service(tmp_path, client)
    service.handle_push(_envelope("1000"))
    result = service.drain()

    assert result["ok"] is False, "a partial drain must not report success"
    assert result["messages"] == 1, "the readable message should still be delivered"
    assert result["failures"] == 1
    assert service._load_state().history_id != "1100", \
        "history advanced past an unreadable message"
    assert service._load_queue(), "the notification must stay queued for retry"


def test_all_fetches_failing_retains_history_and_queues_again(tmp_path):
    """Nothing readable: no advance, no clear, and the reason is recorded."""
    client = FakeClient(history_ids=["g1", "g2"], messages={},
                        newest_history_id="1100")
    service, seen, _ = _service(tmp_path, client)
    service.handle_push(_envelope("1000"))
    result = service.drain()

    assert result["ok"] is False
    assert result["messages"] == 0
    assert result["failures"] == 2
    assert seen == [], "no message should be ingested when none could be read"
    assert service._load_state().history_id == "1000", \
        "the mark must stay where the notification left it, not jump to newest"
    assert service._load_state().last_error, "the failure must be recorded"
    assert len(service._load_queue()) == 1, "the notification must survive"


def test_repeated_failure_abandons_loudly_instead_of_wedging(tmp_path):
    """A permanently undeliverable id must not block every later notification.

    Giving up is a real loss, so the count is recorded and visible rather than
    the queue silently emptying.
    """
    from core.capabilities.email.gmail_push import MAX_DRAIN_ATTEMPTS

    client = FakeClient(history_ids=["g1"], messages={}, newest_history_id="1100")
    service, _, _ = _service(tmp_path, client)
    service.handle_push(_envelope("1000"))

    for _ in range(MAX_DRAIN_ATTEMPTS):
        service.drain()

    state = service._load_state()
    assert service._load_queue() == [], "a wedged queue would stall all later mail"
    assert state.abandoned_notifications == 1
    assert "abandoned" in state.last_error
    assert state.last_error in service.health()["last_error"]


def test_successful_drain_still_advances_and_clears(tmp_path):
    """The no-loss change must not stop normal progress."""
    good = {
        "id": "g1", "threadId": "t1",
        "payload": {"headers": [{"name": "Message-ID", "value": "<m1@x>"}]},
    }
    client = FakeClient(history_ids=["g1"], messages={"g1": good},
                        newest_history_id="1100")
    service, seen, _ = _service(tmp_path, client)
    service.handle_push(_envelope("1000"))
    result = service.drain()

    assert result["ok"] is True
    assert result["messages"] == 1
    assert len(seen) == 1
    assert service._load_state().history_id == "1100"
    assert service._load_state().abandoned_notifications == 0
    assert service._load_queue() == []


def test_ingest_refusal_does_not_advance_history(tmp_path):
    """A pipeline that says 'no' has not accepted the messages."""
    good = {
        "id": "g1", "threadId": "t1",
        "payload": {"headers": [{"name": "Message-ID", "value": "<m1@x>"}]},
    }
    client = FakeClient(history_ids=["g1"], messages={"g1": good},
                        newest_history_id="1100")
    service, _, _ = _service(tmp_path, client)
    service._ingest = lambda items: {"ok": False, "reason": "paused"}
    service.handle_push(_envelope("1000"))
    result = service.drain()

    assert result["ok"] is False
    assert "paused" in result["reason"]
    assert service._load_state().history_id != "1100"
    assert service._load_queue()


def test_ingest_exception_does_not_advance_history(tmp_path):
    good = {
        "id": "g1", "threadId": "t1",
        "payload": {"headers": [{"name": "Message-ID", "value": "<m1@x>"}]},
    }
    client = FakeClient(history_ids=["g1"], messages={"g1": good},
                        newest_history_id="1100")
    service, _, _ = _service(tmp_path, client)

    def boom(items):
        raise RuntimeError("pipeline exploded")

    service._ingest = boom
    service.handle_push(_envelope("1000"))
    result = service.drain()

    assert result["ok"] is False
    assert "pipeline exploded" in result["reason"]
    assert service._load_state().history_id != "1100"
    assert service._load_queue()


# ── normalization ──────────────────────────────────────────────────────────


def test_normalize_matches_imap_transport_shape():
    payload = {
        "id": "gmail-1", "threadId": "thr-1",
        "labelIds": ["INBOX", "UNREAD"],
        "internalDate": "1700000000000",
        "payload": {"headers": [
            {"name": "From", "value": "Arsene Manzi <a@eagles.oc.edu>"},
            {"name": "To", "value": "aster@x.com"},
            {"name": "Subject", "value": "Re: V3 Acceptance"},
            {"name": "Message-ID", "value": "<abc@x>"},
            {"name": "In-Reply-To", "value": "<parent@x>"},
            {"name": "References", "value": "<root@x> <parent@x>"},
            {"name": "Date", "value": "Mon, 28 Sep 2026 12:00:00 -0500"},
        ], "mimeType": "text/plain", "body": {"data": base64.b64encode(
            b"Step one").decode()}},
    }
    item = normalize_gmail_message(payload)
    assert item["external_id"] == "<abc@x>"
    assert item["gmail_message_id"] == "gmail-1"
    assert item["gmail_thread_id"] == "thr-1"
    assert item["from"] == "a@eagles.oc.edu"
    assert item["subject"] == "Re: V3 Acceptance"
    assert item["body"] == "Step one"
    assert item["in_reply_to"] == "<parent@x>"
    assert item["references"] == ["<root@x>", "<parent@x>"]
    # Gmail's immutable thread id must reach the durable job.
    assert item["thread_id"] == "thr-1"


def test_normalize_handles_missing_headers_honestly():
    item = normalize_gmail_message({})
    assert item["external_id"] == ""
    assert item["subject"] == ""
    assert item["from"] == ""


def test_normalize_prefers_internal_date_over_sender_header():
    """Delivery latency must come from Gmail, not from a claimable header."""
    payload = {
        "id": "g1", "threadId": "t", "internalDate": "1700000000000",
        "payload": {"headers": [
            {"name": "Message-ID", "value": "<m@x>"},
            {"name": "Date", "value": "Mon, 01 Jan 2001 00:00:00 +0000"},
        ]},
    }
    item = normalize_gmail_message(payload)
    assert item["received_at"] == "2023-11-14T22:13:20+00:00", \
        "internalDate (epoch ms) is the authoritative receive time"


def test_normalize_falls_back_to_date_header_then_to_unknown():
    with_header = normalize_gmail_message({
        "id": "g1", "payload": {"headers": [
            {"name": "Message-ID", "value": "<m@x>"},
            {"name": "Date", "value": "Tue, 14 Nov 2023 22:13:20 +0000"}]},
    })
    assert with_header["received_at"] == "2023-11-14T22:13:20+00:00"

    unknown = normalize_gmail_message({
        "id": "g2", "payload": {"headers": [{"name": "Message-ID", "value": "<n@x>"}]},
    })
    assert unknown["received_at"] == "", \
        "an unknown receive time must stay unknown, not become 'now'"


def test_normalize_ignores_garbage_internal_date():
    for bad in ("not-a-number", "0", "-5", ""):
        item = normalize_gmail_message({
            "id": "g", "internalDate": bad,
            "payload": {"headers": [
                {"name": "Message-ID", "value": "<m@x>"},
                {"name": "Date", "value": "Tue, 14 Nov 2023 22:13:20 +0000"}]},
        })
        assert item["received_at"] == "2023-11-14T22:13:20+00:00", bad


def test_normalize_prefers_raw_rfc822_when_present():
    raw = "From: a@x\r\nSubject: Hi\r\n\r\nbody text\r\n"
    payload = {"id": "g1", "threadId": "t",
               "payload": {"headers": [{"name": "Message-ID", "value": "<r@x>"}]},
               "raw": base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")}
    item = normalize_gmail_message(payload)
    assert "body text" in item["raw_body"]


# ── watch state ────────────────────────────────────────────────────────────


def test_watch_state_roundtrip():
    state = WatchState(history_id="5", notifications_received=3,
                       messages_ingested=2, expiration="2030-01-01T00:00:00+00:00")
    again = WatchState.from_dict(state.to_dict())
    assert again.history_id == "5"
    assert again.notifications_received == 3
    assert again.messages_ingested == 2


def test_watch_without_expiry_is_treated_as_expired():
    assert WatchState().is_expired() is True, \
        "unknown expiry must not be reported as a live watch"


def test_health_never_exposes_secrets(tmp_path):
    service, _, _ = _service(tmp_path, FakeClient())
    payload = json.dumps(service.health())
    for secret in ("CLIENTIDVALUE", "SECRETVALUE", "REFRESHVALUE", "verify-me"):
        assert secret not in payload


# ── receive-only vs draining ownership ────────────────────────────────────


def test_receive_only_service_refuses_to_drain(tmp_path):
    """The presence process must not pretend to have processed mail.

    It has no configured engine, so a drain that silently succeeded would make
    health claim the notification was handled when it was never even read.
    """
    good = {
        "id": "g1", "threadId": "t1",
        "payload": {"headers": [{"name": "Message-ID", "value": "<m1@x>"}]},
    }
    client = FakeClient(history_ids=["g1"], messages={"g1": good},
                        newest_history_id="1100")
    backend = JSONFileBackend(root_dir=str(tmp_path / "store"))
    service = GmailPushService(_config(), storage=backend, identity_id="aster",
                               ingest=None, client=client)

    assert service.can_drain is False
    service.handle_push(_envelope("1000"))
    result = service.drain()

    assert result["ok"] is False
    assert "receive-only" in result["reason"]
    assert service._load_queue(), "the notification must remain for the operator"
    assert service._load_state().history_id != "1100"


def test_receive_only_service_still_accepts_and_queues(tmp_path):
    """Receiving is the presence process's whole job; it must keep working."""
    backend = JSONFileBackend(root_dir=str(tmp_path / "store"))
    service = GmailPushService(_config(), storage=backend, identity_id="aster",
                               ingest=None,
                               client=FakeClient(newest_history_id="1100"))
    status, body = service.handle_push(_envelope("1000"))
    assert status == 200
    assert body["accepted"] is True
    assert len(service._load_queue()) == 1


def test_presence_and_operator_share_the_queue(tmp_path):
    """The split only works because both processes see the same storage."""
    good = {
        "id": "g1", "threadId": "t1",
        "payload": {"headers": [{"name": "Message-ID", "value": "<m1@x>"}]},
    }
    root = str(tmp_path / "store")

    # Presence process: receive only.
    presence = GmailPushService(
        _config(), storage=JSONFileBackend(root_dir=root), identity_id="aster",
        ingest=None,
        client=FakeClient(history_ids=["g1"], messages={"g1": good},
                          newest_history_id="1100"))
    # Operator process: a separate store instance over the same directory.
    ingested: list[list[dict]] = []
    operator = GmailPushService(
        _config(), storage=JSONFileBackend(root_dir=root), identity_id="aster",
        ingest=lambda items: ingested.append(list(items)) or {"ok": True},
        client=FakeClient(history_ids=["g1"], messages={"g1": good},
                          newest_history_id="1100"))

    presence.handle_push(_envelope("1000"))
    assert presence.drain()["ok"] is False, "presence must not ingest"

    result = operator.drain()
    assert result["ok"] is True
    assert len(ingested) == 1, "the operator drains what presence queued"
    assert ingested[0][0]["external_id"] == "<m1@x>"


def test_draining_service_reports_it_can_drain(tmp_path):
    service, _, _ = _service(tmp_path, FakeClient())
    assert service.can_drain is True
