"""Self-diagnosis and non-destructive cleanup regressions."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from core.operations.health import runtime_health
from core.operations.monitor import message_answered
from core.operations.maintenance import (
    MAINTENANCE_HISTORY_LIMIT,
    MAINTENANCE_NAMESPACE,
    SelfMaintenance,
)
from core.operations.models import (
    EmailJob,
    EmailJobStatus,
    FollowUp,
    FollowUpStatus,
    Message,
    MessageDirection,
    MessageStatus,
    Relationship,
    RelationshipStatus,
)
from core.operations.store import OperationsStore
from runtime.persistence import InMemoryBackend


def _store():
    backend = InMemoryBackend()
    return OperationsStore(backend, "aster"), backend


def test_cleanup_cancels_invalid_followups_without_deleting_evidence():
    store, _ = _store()
    open_rel = Relationship(id="open", email="open@example.org",
                            status=RelationshipStatus.ENGAGED)
    closed_rel = Relationship(id="closed", email="closed@example.org",
                              status=RelationshipStatus.OPTED_OUT, opted_out=True)
    store.add_relationship(open_rel)
    store.add_relationship(closed_rel)
    followups = [
        FollowUp(id="keep", relationship_id="open",
                 due_at="2026-09-30T00:00:00+00:00",
                 created_at="2026-09-20T00:00:00+00:00"),
        FollowUp(id="duplicate", relationship_id="open",
                 due_at="2026-10-01T00:00:00+00:00",
                 created_at="2026-09-21T00:00:00+00:00"),
        FollowUp(id="missing", relationship_id="gone",
                 due_at="2026-09-30T00:00:00+00:00"),
        FollowUp(id="closed-fu", relationship_id="closed",
                 due_at="2026-09-30T00:00:00+00:00"),
        FollowUp(id="bad-date", relationship_id="open", due_at="not-a-date"),
    ]
    for item in followups:
        store.add_follow_up(item)

    report = SelfMaintenance(store).run(
        now=datetime(2026, 9, 29, tzinfo=timezone.utc))

    assert report["status"] == "healthy"
    assert report["safety"]["durable_records_deleted"] == 0
    assert len(store.list_follow_ups()) == 5, "cleanup must preserve the audit rows"
    assert [f.id for f in store.list_follow_ups(status=FollowUpStatus.SCHEDULED)] == ["keep"]
    cancelled = store.list_follow_ups(status=FollowUpStatus.CANCELLED)
    assert {f.id for f in cancelled} == {"duplicate", "missing", "closed-fu", "bad-date"}
    assert all(f.completed_at for f in cancelled)


def test_diagnosis_is_deduplicated_and_recovery_is_recorded():
    store, _ = _store()
    message = Message(
        id="in1", direction=MessageDirection.INBOUND, channel="email",
        external_id="<m1@x>", status=MessageStatus.RECEIVED,
        relationship_id="rel1", body="hello",
    )
    store.append_message(message)
    store.add_relationship(Relationship(id="rel1", email="friend@example.org"))
    job = EmailJob(
        id="job1", inbound_message_id="<m1@x>", rfc_message_id="<m1@x>",
        status=EmailJobStatus.FAILED, retryable=True, attempt_count=1,
        failure_reason="model unavailable",
    )
    store.save_email_job(job)
    maintenance = SelfMaintenance(store)
    later = datetime.now(timezone.utc) + timedelta(hours=1)

    first = maintenance.run(now=later)
    second = maintenance.run(now=later + timedelta(minutes=1))

    assert first["status"] == "degraded"
    assert any(f["code"] == "owed_email_reply" for f in first["findings"])
    assert any(f["code"] == "stale_email_job" for f in first["findings"])
    diagnoses = [n for n in store.list_notifications() if n.kind == "self_diagnosis"]
    assert len(diagnoses) == 1, "the same finding must not notify every tick"
    assert second["status"] == "degraded"

    job.status = EmailJobStatus.COMPLETED
    job.retryable = False
    store.save_email_job(job)
    healthy = maintenance.run(now=later + timedelta(minutes=2))
    assert healthy["status"] == "healthy"
    assert len([n for n in store.list_notifications() if n.kind == "self_recovery"]) == 1
    assert store.get_email_job("job1") is not None, "recovery must not erase evidence"


def test_cleanup_clears_only_answered_deferred_markers():
    store, _ = _store()
    answered = Message(
        id="answered", direction=MessageDirection.INBOUND, channel="email",
        external_id="<answered@x>", status=MessageStatus.RECEIVED,
        authorization="deferred:model_unavailable",
    )
    still_owed = Message(
        id="owed", direction=MessageDirection.INBOUND, channel="email",
        external_id="<owed@x>", status=MessageStatus.RECEIVED,
        authorization="deferred:model_unavailable",
    )
    store.append_message(answered)
    store.append_message(still_owed)
    store.append_message(Message(
        direction=MessageDirection.OUTBOUND, channel="email",
        status=MessageStatus.SENT, in_reply_to="<answered@x>",
    ))

    report = SelfMaintenance(store).run()

    assert store.get_message("answered").authorization == ""
    assert store.get_message("owed").authorization == "deferred:model_unavailable"
    # An unlinked deferred inbound has a distinct, more specific finding.
    assert any(f["code"] == "deferred_inbound_unlinked" for f in report["findings"])
    still_owed2 = store.get_message("owed")
    still_owed2.relationship_id = "rel1"
    store.update_message(still_owed2)
    linked = SelfMaintenance(store).run()
    assert any(f["code"] == "deferred_inbound" for f in linked["findings"])


def test_relinks_orphaned_inbound_only_on_unique_thread_evidence():
    store, _ = _store()
    rel = Relationship(id="rel1", email="friend@example.org",
                       thread_ids=["thread-known"])
    store.add_relationship(rel)
    message = Message(
        id="orphan", direction=MessageDirection.INBOUND, channel="email",
        external_id="<recover@x>", thread_id="thread-known",
        status=MessageStatus.RECEIVED,
        authorization="deferred:reply_generation_unavailable",
    )
    store.append_message(message)

    report = SelfMaintenance(store).run()

    linked = store.get_message("orphan")
    assert linked.relationship_id == "rel1"
    assert "orphan" in store.get_relationship("rel1").message_ids
    assert any(a["action"] == "inbound_relationship_relinked" for a in report["actions"])
    assert not any(f["code"] == "deferred_inbound_unlinked" for f in report["findings"])
    assert any(f["code"] == "deferred_inbound" for f in report["findings"])


def test_relink_refuses_ambiguous_thread_evidence():
    store, _ = _store()
    store.add_relationship(Relationship(id="rel-a", email="a@example.org",
                                        thread_ids=["thread-shared"]))
    store.add_relationship(Relationship(id="rel-b", email="b@example.org",
                                        thread_ids=["thread-shared"]))
    store.append_message(Message(
        id="ambiguous", direction=MessageDirection.INBOUND, channel="email",
        external_id="<amb@x>", thread_id="thread-shared",
        status=MessageStatus.RECEIVED,
        authorization="deferred:reply_generation_unavailable",
    ))

    report = SelfMaintenance(store).run()

    assert store.get_message("ambiguous").relationship_id == ""
    assert not any(a.get("action") == "inbound_relationship_relinked"
                   for a in report["actions"])
    assert any(f["code"] == "deferred_inbound_unlinked" for f in report["findings"])


def test_maintenance_history_is_bounded_derived_telemetry():
    store, backend = _store()
    maintenance = SelfMaintenance(store)
    start = datetime(2026, 9, 1, tzinfo=timezone.utc)
    for index in range(MAINTENANCE_HISTORY_LIMIT + 7):
        maintenance.run(now=start + timedelta(minutes=index))

    data = backend.load("aster", MAINTENANCE_NAMESPACE)
    assert len(data["history"]) == MAINTENANCE_HISTORY_LIMIT
    assert data["latest"]["safety"]["durable_records_deleted"] == 0


def test_runtime_health_consumes_persisted_self_diagnosis():
    store, backend = _store()
    now = datetime.now(timezone.utc)
    SelfMaintenance(store).run(now=now)

    payload = runtime_health(store, storage=backend, identity_id="aster", now=now)

    subsystem = next(s for s in payload["subsystems"] if s["name"] == "self_maintenance")
    assert subsystem["state"] == "healthy"
    assert payload["maintenance"]["safety"]["durable_records_deleted"] == 0


def test_thread_covered_inbound_is_reported_without_degrading_health():
    store, _ = _store()
    rel = Relationship(id="rel1", email="friend@example.org")
    store.add_relationship(rel)
    earlier = Message(
        id="earlier", direction=MessageDirection.INBOUND, channel="email",
        external_id="<e@x>", thread_id="t", status=MessageStatus.RECEIVED,
        created_at="2026-09-28T17:00:00+00:00",
    )
    later = Message(
        id="later", direction=MessageDirection.INBOUND, channel="email",
        external_id="<l@x>", thread_id="t", status=MessageStatus.RECEIVED,
        created_at="2026-09-28T18:00:00+00:00",
    )
    store.append_message(earlier)
    store.append_message(later)
    store.append_message(Message(
        direction=MessageDirection.OUTBOUND, channel="email",
        status=MessageStatus.SENT, in_reply_to="<l@x>",
        created_at="2026-09-29T00:00:00+00:00",
    ))

    report = SelfMaintenance(store).run()

    # The earlier message was never individually answered, and maintenance
    # must not pretend it was — but a thread-level answer is informational.
    assert not message_answered(store, store.get_message("earlier"))
    assert store.get_message("earlier").authorization == ""
    assert report["status"] == "healthy"
    info = next(f for f in report["findings"]
                if f["code"] == "inbound_covered_by_thread_answer")
    assert info["severity"] == "info"
    assert report["summary"]["problems"] == 0


def test_runtime_health_counts_retryable_failure_as_owed():
    store, backend = _store()
    backend.save("aster", "capabilities", {"installed": [{"id": "email"}]})
    store.save_email_job(EmailJob(
        id="retryable", inbound_message_id="<retry@x>",
        status=EmailJobStatus.FAILED, retryable=True,
        attempt_count=1, max_attempts=3, failure_reason="transport timeout",
    ))
    now = datetime.now(timezone.utc)
    SelfMaintenance(store).run(now=now)

    payload = runtime_health(store, storage=backend, identity_id="aster", now=now)

    assert payload["email_jobs"]["awaiting_response"][0]["job_id"] == "retryable"
    sender = next(s for s in payload["subsystems"] if s["name"] == "email_sender")
    assert sender["state"] == "degraded"
