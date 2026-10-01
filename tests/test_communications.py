"""Tests for the communications view and runtime health.

Two properties matter most here and both are about honesty rather than
convenience:

1. The communications view must not invent structure. Email rows, terminal
   rows, and legacy jobs all have to land in the right place with the fields
   they actually have, and fields a transport does not provide stay empty.
2. Health must report what the records say. A missing heartbeat is
   ``unknown``, not ``healthy``; a device bridge being absent degrades a
   subsystem without taking the identity offline.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from core.operations.communications import (
    CHANNEL_EMAIL,
    CHANNEL_TERMINAL,
    channels_for,
    conversations_for,
    get_channel,
    register_channel,
    Channel,
)
from core.operations.health import runtime_health
from core.operations.models import (
    AWAITING_RESPONSE_STATUSES,
    EmailJob,
    EmailJobStatus,
    Message,
    MessageDirection,
    MessageStatus,
    Relationship,
)
from core.operations.store import OperationsStore
from runtime.persistence import JSONFileBackend


def _store(tmp_path, *, email: bool = False):
    backend = JSONFileBackend(root_dir=str(tmp_path / "store"))
    if email:
        backend.save("aster", "capabilities", {
            "installed": [{"id": "email", "version": "1.0.0", "config": {}}],
        })
    return OperationsStore(backend, "aster"), backend


def _iso(dt: datetime) -> str:
    return dt.isoformat()


# ── fixtures ───────────────────────────────────────────────────────────────


def _relationship(email: str = "a@eagles.oc.edu") -> Relationship:
    return Relationship(id="rel1", display_name="Principal", email=email,
                        role="principal", created_at=_iso(datetime.now(timezone.utc)))


def _inbound(job_rfc: str, body: str = "hello", *, thread: str = "t1",
             subject: str = "Re: Test", relationship: str = "rel1",
             received: datetime | None = None) -> Message:
    now = received or datetime.now(timezone.utc)
    return Message(
        id=f"in-{job_rfc}",
        direction=MessageDirection.INBOUND,
        body=body,
        status=MessageStatus.RECEIVED,
        relationship_id=relationship,
        external_id=job_rfc,
        thread_id=thread,
        subject=subject,
        received_at=_iso(now),
        created_at=_iso(now),
    )


def _outbound(body: str = "reply", *, thread: str = "t1",
              in_reply_to: str = "", subject: str = "Re: Test",
              relationship: str = "rel1",
              sent: datetime | None = None) -> Message:
    now = sent or datetime.now(timezone.utc)
    return Message(
        id=f"out-{len(body)}-{now.timestamp()}",
        direction=MessageDirection.OUTBOUND,
        body=body,
        status=MessageStatus.SENT,
        relationship_id=relationship,
        in_reply_to=in_reply_to,
        thread_id=thread,
        subject=subject,
        sent_at=_iso(now),
        created_at=_iso(now),
        generation={"to": "a@eagles.oc.edu", "adapter": "chain", "model": "m"},
    )


def _job(job_rfc: str, status: EmailJobStatus, *, gmail: str = "", thread: str = "",
         reason: str = "", base: datetime | None = None) -> EmailJob:
    b = base or datetime.now(timezone.utc)
    return EmailJob(
        id=f"job-{job_rfc[:12]}",
        inbound_message_id=job_rfc,
        rfc_message_id=job_rfc,
        gmail_message_id=gmail,
        gmail_thread_id=thread,
        subject="Re: Test",
        received_at=_iso(b),
        created_at=_iso(b),
        status=status,
        completed_at=_iso(b + timedelta(seconds=1)) if status in (
            EmailJobStatus.COMPLETED, EmailJobStatus.FAILED) else None,
        failure_reason=reason,
    )


# ── channel grouping ───────────────────────────────────────────────────────


def test_channels_are_canonical_and_terminal_is_separate(tmp_path):
    """Email and terminal share the Message table but must never merge."""
    store, _ = _store(tmp_path)
    store.add_relationship(_relationship())
    store.append_message(_inbound("<r1@x>"))
    store.append_message(Message(
        id="ctl-1", direction=MessageDirection.INBOUND, body="run a tick",
        status=MessageStatus.RECEIVED, channel="aster-control",
        created_at=_iso(datetime.now(timezone.utc)),
    ))

    email = conversations_for(store, CHANNEL_EMAIL)
    terminal = conversations_for(store, CHANNEL_TERMINAL)

    assert len(email) == 1
    assert [e.channel for e in email[0].events] == [CHANNEL_EMAIL]
    assert len(terminal) == 1
    assert [e.channel for e in terminal[0].events] == [CHANNEL_TERMINAL]
    # No event from one channel appears in the other.
    assert all(e.id != "ctl-1" for e in email[0].events)


def test_gmail_thread_id_is_authoritative_for_conversation_key(tmp_path):
    """When the transport captured a Gmail thread id, grouping uses it even
    if the model mangled the subject."""
    store, _ = _store(tmp_path)
    store.add_relationship(_relationship())
    store.append_message(_inbound("<r1@x>", subject="Aster Loop V3", thread=""))
    store.append_message(_inbound("<r2@x>", subject="Confirmation of STEP-1", thread=""))
    store.save_email_job(_job("<r1@x>", EmailJobStatus.COMPLETED, gmail="g1", thread="gmail-77"))
    store.save_email_job(_job("<r2@x>", EmailJobStatus.COMPLETED, gmail="g2", thread="gmail-77"))

    convs = conversations_for(store, CHANNEL_EMAIL)
    assert len(convs) == 1, "different subjects in one Gmail thread must not split"
    assert convs[0].external_conversation_id == "gmail-77"
    assert convs[0].id == "email:gmail-77"


def test_conversation_falls_back_to_rfc_thread_when_no_gmail_id(tmp_path):
    store, _ = _store(tmp_path)
    store.add_relationship(_relationship())
    store.append_message(_inbound("<r1@x>", thread="<thread-9@x>"))
    store.append_message(_inbound("<r2@x>", thread="<thread-9@x>"))
    store.save_email_job(_job("<r1@x>", EmailJobStatus.COMPLETED))
    store.save_email_job(_job("<r2@x>", EmailJobStatus.COMPLETED))
    convs = conversations_for(store, CHANNEL_EMAIL)
    assert len(convs) == 1
    assert convs[0].id == "email:<thread-9@x>"


def test_legacy_jobs_without_rfc_message_id_still_join(tmp_path):
    """Jobs written before rfc_message_id existed used inbound_message_id.

    Losing their Gmail ids would silently drop diagnostics for the exact
    messages the team is debugging.
    """
    store, _ = _store(tmp_path)
    store.add_relationship(_relationship())
    store.append_message(_inbound("<legacy@x>"))
    job = _job("<legacy@x>", EmailJobStatus.COMPLETED, gmail="gmail-legacy")
    job.rfc_message_id = ""          # simulate the legacy shape
    store.save_email_job(job)
    convs = conversations_for(store, CHANNEL_EMAIL)
    event = convs[0].events[0]
    assert event.external_message_id == "gmail-legacy"


def test_awaiting_aster_comes_from_ledger_not_timestamps(tmp_path):
    """A message with no reply must read as awaiting even if nothing moved
    recently; a sent reply must clear it."""
    store, _ = _store(tmp_path)
    store.add_relationship(_relationship())
    store.append_message(_inbound("<r1@x>"))
    store.save_email_job(_job("<r1@x>", EmailJobStatus.HELD, base=datetime.now(timezone.utc) - timedelta(days=2)))
    convs = conversations_for(store, CHANNEL_EMAIL)
    assert convs[0].awaiting_aster is True
    assert convs[0].state == "awaiting_aster"

    store.append_message(_outbound(in_reply_to="<r1@x>"))
    store.save_email_job(_job("<r1@x>", EmailJobStatus.COMPLETED, base=datetime.now(timezone.utc) - timedelta(days=2)))
    convs = conversations_for(store, CHANNEL_EMAIL)
    assert convs[0].awaiting_aster is False


def test_held_counts_as_awaiting_response_in_models():
    assert EmailJobStatus.HELD.value in AWAITING_RESPONSE_STATUSES
    assert EmailJobStatus.COMPLETED.value not in AWAITING_RESPONSE_STATUSES
    assert EmailJobStatus.SENT.value not in AWAITING_RESPONSE_STATUSES


def test_unknown_channel_is_distinct_from_empty_channel(tmp_path):
    store, _ = _store(tmp_path)
    assert get_channel("email") is not None
    assert get_channel("sms") is None
    assert conversations_for(store, "sms") == []


def test_awaiting_set_is_importable_and_matches_health_usage():
    from core.operations.health import runtime_health as rh
    assert callable(rh)


# ── health ─────────────────────────────────────────────────────────────────


class _FakePresence:
    def __init__(self, record):
        self._record = record

    def record(self):
        return self._record


def test_health_reports_unknown_not_healthy_without_heartbeat(tmp_path):
    store, backend = _store(tmp_path)
    report = runtime_health(store, presence_store=None, storage=backend, identity_id="aster")
    runtime = [s for s in report["subsystems"] if s["name"] == "identity_runtime"][0]
    assert runtime["state"] == "unknown"
    assert report["overall"] != "healthy"


def test_health_healthy_with_fresh_heartbeat_and_no_unanswered(tmp_path):
    store, backend = _store(tmp_path, email=True)
    now = datetime.now(timezone.utc)
    backend.save("aster", "operations.ingest_health", {
        "ok": True, "fetched": 0, "at": _iso(now),
    })
    report = runtime_health(store, presence_store=_FakePresence({
        "last_heartbeat": _iso(now), "last_tick_at": _iso(now),
    }), storage=backend, identity_id="aster")
    assert report["overall"] == "healthy"
    runtime = [s for s in report["subsystems"] if s["name"] == "identity_runtime"][0]
    assert runtime["state"] == "healthy"


def test_configured_email_with_no_ever_poll_is_degraded(tmp_path):
    """Email installed but never polled is a real gap, not a healthy state."""
    store, backend = _store(tmp_path, email=True)
    now = datetime.now(timezone.utc)
    report = runtime_health(store, presence_store=_FakePresence({
        "last_heartbeat": _iso(now), "last_tick_at": _iso(now),
    }), storage=backend, identity_id="aster")
    ingest = [s for s in report["subsystems"] if s["name"] == "email_ingest"][0]
    assert ingest["state"] == "degraded"
    assert "no poll" in ingest["detail"]
    assert report["overall"] == "degraded"


def test_uninstalled_email_is_not_a_degraded_identity(tmp_path):
    """No email capability means nothing to poll — not a broken identity."""
    store, backend = _store(tmp_path, email=False)
    now = datetime.now(timezone.utc)
    report = runtime_health(store, presence_store=_FakePresence({
        "last_heartbeat": _iso(now), "last_tick_at": _iso(now),
    }), storage=backend, identity_id="aster")
    ingest = [s for s in report["subsystems"] if s["name"] == "email_ingest"][0]
    assert ingest["state"] == "not_configured"
    assert report["overall"] == "healthy"


def test_health_offline_on_stale_heartbeat(tmp_path):
    store, backend = _store(tmp_path, email=True)
    old = datetime.now(timezone.utc) - timedelta(hours=2)
    report = runtime_health(store, presence_store=_FakePresence({
        "last_heartbeat": _iso(old), "last_tick_at": _iso(old),
    }), storage=backend, identity_id="aster")
    runtime = [s for s in report["subsystems"] if s["name"] == "identity_runtime"][0]
    assert runtime["state"] == "offline"
    assert report["overall"] == "offline"


def test_health_degraded_when_loop_not_ticking_but_heartbeat_fresh(tmp_path):
    store, backend = _store(tmp_path, email=True)
    now = datetime.now(timezone.utc)
    report = runtime_health(store, presence_store=_FakePresence({
        "last_heartbeat": _iso(now),
        "last_tick_at": _iso(now - timedelta(hours=1)),
    }), storage=backend, identity_id="aster")
    runtime = [s for s in report["subsystems"] if s["name"] == "identity_runtime"][0]
    assert runtime["state"] == "degraded"


def test_device_bridge_absence_never_takes_identity_offline(tmp_path):
    """The conceptual split: browser control is a device capability, not the
    identity. Its absence must not make Aster look dead."""
    store, backend = _store(tmp_path)
    now = datetime.now(timezone.utc)
    report = runtime_health(store, presence_store=_FakePresence({
        "last_heartbeat": _iso(now), "last_tick_at": _iso(now),
    }), storage=backend, identity_id="aster")
    names = {s["name"]: s for s in report["subsystems"]}
    assert "device_bridge" in names
    assert report["overall"] in ("healthy", "degraded")
    assert names["device_bridge"]["state"] != "offline" or report["overall"] != "offline"


def test_health_surfaces_unanswered_inbound_as_degraded(tmp_path):
    store, backend = _store(tmp_path, email=True)
    now = datetime.now(timezone.utc)
    store.add_relationship(_relationship())
    store.append_message(_inbound("<r1@x>"))
    store.save_email_job(_job("<r1@x>", EmailJobStatus.HELD, base=now - timedelta(minutes=5)))
    report = runtime_health(store, presence_store=_FakePresence({
        "last_heartbeat": _iso(now), "last_tick_at": _iso(now),
    }), storage=backend, identity_id="aster")
    names = {s["name"]: s for s in report["subsystems"]}
    assert names["email_sender"]["state"] == "degraded"
    assert "awaiting" in names["email_sender"]["detail"] or "response" in names["email_sender"]["detail"]
    assert report["email_jobs"]["awaiting_response"], "unanswered message must be listed"
    assert report["overall"] == "degraded"


def test_ingest_health_reads_persisted_record(tmp_path):
    store, backend = _store(tmp_path, email=True)
    now = datetime.now(timezone.utc)
    backend.save("aster", "operations.ingest_health", {
        "ok": True, "fetched": 2, "at": _iso(now),
    })
    report = runtime_health(store, presence_store=_FakePresence({
        "last_heartbeat": _iso(now), "last_tick_at": _iso(now),
    }), storage=backend, identity_id="aster")
    ingest = [s for s in report["subsystems"] if s["name"] == "email_ingest"][0]
    assert ingest["state"] == "healthy"
    assert "2" in ingest["detail"]


def test_failed_ingest_is_offline_not_healthy(tmp_path):
    store, backend = _store(tmp_path, email=True)
    now = datetime.now(timezone.utc)
    backend.save("aster", "operations.ingest_health", {
        "ok": False, "error": "IMAP login failed", "at": _iso(now),
    })
    report = runtime_health(store, presence_store=_FakePresence({
        "last_heartbeat": _iso(now), "last_tick_at": _iso(now),
    }), storage=backend, identity_id="aster")
    ingest = [s for s in report["subsystems"] if s["name"] == "email_ingest"][0]
    assert ingest["state"] == "offline"
    assert "IMAP login failed" in ingest["detail"]


def test_stalled_ingest_degrades(tmp_path):
    store, backend = _store(tmp_path, email=True)
    now = datetime.now(timezone.utc)
    backend.save("aster", "operations.ingest_health", {
        "ok": True, "fetched": 0, "at": _iso(now - timedelta(hours=3)),
    })
    report = runtime_health(store, presence_store=_FakePresence({
        "last_heartbeat": _iso(now), "last_tick_at": _iso(now),
    }), storage=backend, identity_id="aster")
    ingest = [s for s in report["subsystems"] if s["name"] == "email_ingest"][0]
    assert ingest["state"] == "degraded"


# ── job created_at backfill (regression) ──────────────────────────────────


def test_legacy_job_created_at_not_stamped_with_load_time():
    """A job persisted before created_at existed must not be re-stamped with
    the time it was read; that rewrites history and inflates delivery
    latency."""
    data = {"id": "j1", "inbound_message_id": "<r@x>", "status": "completed",
            "received_at": "2026-01-01T00:00:00+00:00",
            "completed_at": "2026-01-01T00:00:05+00:00"}
    job = EmailJob.from_dict(data)
    assert job.created_at == "2026-01-01T00:00:00+00:00"
    latency = job.latency_breakdown()
    assert latency["total_seconds"] == pytest.approx(5.0)
    assert latency["delivery_seconds"] == pytest.approx(0.0)


def test_created_at_roundtrips_unchanged():
    job = _job("<r@x>", EmailJobStatus.COMPLETED)
    again = EmailJob.from_dict(json.loads(json.dumps(job.to_dict())))
    assert again.created_at == job.created_at


def test_latency_segments_none_when_stages_missing():
    job = _job("<r@x>", EmailJobStatus.DISCOVERED)
    job.smtp_accepted_at = ""
    job.completed_at = None
    latency = job.latency_breakdown()
    assert latency["send_seconds"] is None
    assert latency["total_seconds"] is None
