"""Evidence-backed self-diagnosis and non-destructive maintenance.

Maintenance is deliberately separate from communication. It may inspect and
repair durable bookkeeping, but it never composes or sends a message. The
normal operator phases decide whether owed work may proceed after maintenance
has made that work visible again.

"Cleanup" here means reconciliation, cancellation, and bounded derived
telemetry. Messages, email jobs, relationships, provenance, notifications,
memory, and identity state are evidence and are never deleted.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from .models import (
    EmailJobStatus,
    FollowUpStatus,
    MessageDirection,
    MessageStatus,
    NotificationEntry,
    RelationshipStatus,
)
from .monitor import message_answered


MAINTENANCE_NAMESPACE = "operations.maintenance"
MAINTENANCE_HISTORY_LIMIT = 30
STALE_WORK_SECONDS = 900.0


def _parse(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _age(value: Any, now: datetime) -> Optional[float]:
    parsed = _parse(value)
    return None if parsed is None else max(0.0, (now - parsed).total_seconds())


class SelfMaintenance:
    """Diagnose invariants and repair only bookkeeping that is safe to change."""

    def __init__(self, store: Any) -> None:
        self.store = store

    def run(
        self,
        *,
        now: Optional[datetime] = None,
        reconcile_jobs: Optional[Callable[[], list[dict[str, Any]]]] = None,
    ) -> dict[str, Any]:
        now = now or datetime.now(timezone.utc)
        actions: list[dict[str, Any]] = []

        # Observe writes made by the push receiver/control process before
        # diagnosing. A stale cache is not evidence that the state is healthy.
        for refresh in (
            self.store.refresh_messages,
            self.store.refresh_relationships,
            self.store.refresh_email_jobs,
        ):
            try:
                refresh()
            except Exception as exc:  # pragma: no cover - backend dependent
                actions.append({"action": "refresh_failed", "error": str(exc)[:200]})

        actions.extend(self._clean_follow_ups(now))
        actions.extend(self._relink_orphaned_inbound())
        actions.extend(self._clear_answered_deferred())
        if reconcile_jobs is not None:
            try:
                for result in reconcile_jobs() or []:
                    if result.get("action") not in (None, "none", "none-terminal",
                                                     "none-held-live", "none-claimed-live"):
                        actions.append({"action": "email_job_reconciled", **result})
            except Exception as exc:  # pragma: no cover - defensive boundary
                actions.append({"action": "job_reconcile_failed", "error": str(exc)[:200]})

        findings = self._diagnose(now)
        # "info" findings are honest notes for inspection, not failures; they
        # must not make an identity look broken.
        problems = [f for f in findings
                    if f.get("severity") in ("warning", "error")]
        status = "degraded" if problems else "healthy"
        checked_at = now.isoformat()
        report = {
            "status": status,
            "checked_at": checked_at,
            "findings": findings[:100],
            "actions": actions[:100],
            "summary": {
                "findings": len(findings),
                "problems": len(problems),
                "actions": len(actions),
                "stale_work": sum(1 for item in findings if item["code"] == "stale_email_job"),
                "owed_replies": sum(
                    1 for item in findings
                    if item["code"] in ("owed_email_reply", "deferred_inbound",
                                        "deferred_inbound_unlinked")
                ),
            },
            "safety": {
                "durable_records_deleted": 0,
                "policy": "reconcile and cancel; never delete evidence",
            },
        }
        self._persist(report)
        return report

    def _clean_follow_ups(self, now: datetime) -> list[dict[str, Any]]:
        actions: list[dict[str, Any]] = []
        scheduled = self.store.list_follow_ups(status=FollowUpStatus.SCHEDULED)
        by_relationship: dict[str, list[Any]] = {}
        for follow_up in scheduled:
            relationship = self.store.get_relationship(follow_up.relationship_id)
            reason = ""
            if relationship is None:
                reason = "cancelled by self-maintenance: relationship missing"
            elif relationship.opted_out or relationship.status in (
                RelationshipStatus.DECLINED,
                RelationshipStatus.OPTED_OUT,
            ):
                reason = "cancelled by self-maintenance: relationship closed"
            elif _parse(follow_up.due_at) is None:
                reason = "cancelled by self-maintenance: invalid due_at"
            if reason:
                self._cancel_follow_up(follow_up, reason, now)
                actions.append({
                    "action": "follow_up_cancelled",
                    "follow_up_id": follow_up.id,
                    "relationship_id": follow_up.relationship_id,
                    "reason": reason,
                })
                continue
            by_relationship.setdefault(follow_up.relationship_id, []).append(follow_up)

        # A relationship may own only one scheduled nudge. Keep the oldest
        # schedule and cancel extras; preserving the rows keeps the audit trail.
        for relationship_id, items in by_relationship.items():
            if len(items) < 2:
                continue
            items.sort(key=lambda item: (_parse(item.created_at) or now, item.id))
            for duplicate in items[1:]:
                reason = "cancelled by self-maintenance: duplicate scheduled follow-up"
                self._cancel_follow_up(duplicate, reason, now)
                actions.append({
                    "action": "duplicate_follow_up_cancelled",
                    "follow_up_id": duplicate.id,
                    "relationship_id": relationship_id,
                    "kept_follow_up_id": items[0].id,
                })
        return actions

    def _cancel_follow_up(self, follow_up: Any, reason: str, now: datetime) -> None:
        follow_up.status = FollowUpStatus.CANCELLED
        follow_up.reason = reason
        follow_up.completed_at = now.isoformat()
        self.store.update_follow_up(follow_up)
        relationship = self.store.get_relationship(follow_up.relationship_id)
        if relationship is not None:
            remaining = [
                item for item in self.store.list_follow_ups(status=FollowUpStatus.SCHEDULED)
                if item.relationship_id == relationship.id
            ]
            relationship.follow_up_due_at = remaining[0].due_at if remaining else None
            self.store.update_relationship(relationship)

    def _clear_answered_deferred(self) -> list[dict[str, Any]]:
        actions: list[dict[str, Any]] = []
        for message in self.store.list_messages():
            if (message.direction is not MessageDirection.INBOUND
                    or message.status is not MessageStatus.RECEIVED
                    or not str(message.authorization or "").startswith("deferred:")):
                continue
            if not message_answered(self.store, message):
                continue
            message.authorization = ""
            self.store.update_message(message)
            actions.append({"action": "answered_deferred_marker_cleared",
                            "message_id": message.id})
        return actions

    def _relink_orphaned_inbound(self) -> list[dict[str, Any]]:
        """Repair a missing relationship only from an unambiguous thread link.

        Older pre-recorded inbox rows may have been persisted before monitor
        classification attached their relationship. The sender address is not
        stored on those rows, so guessing would be fabrication. A thread id
        present on exactly one relationship is independent durable evidence
        and is safe to adopt; zero or multiple matches are left untouched.
        """
        actions: list[dict[str, Any]] = []
        relationships = self.store.list_relationships()
        for message in self.store.list_messages():
            if (message.direction is not MessageDirection.INBOUND
                    or message.channel != "email"
                    or message.relationship_id
                    or not message.thread_id):
                continue
            matches = [
                relationship for relationship in relationships
                if message.thread_id in (relationship.thread_ids or [])
            ]
            if len(matches) != 1:
                continue
            relationship = matches[0]
            message.relationship_id = relationship.id
            self.store.update_message(message)
            if message.id not in relationship.message_ids:
                relationship.message_ids.append(message.id)
                self.store.update_relationship(relationship)
            actions.append({
                "action": "inbound_relationship_relinked",
                "message_id": message.id,
                "relationship_id": relationship.id,
                "evidence": "unique durable thread_id match",
            })
        return actions

    def _diagnose(self, now: datetime) -> list[dict[str, Any]]:
        findings: list[dict[str, Any]] = []
        messages = self.store.list_messages()
        inbound_keys = {
            value
            for message in messages
            if message.direction is MessageDirection.INBOUND
            for value in (message.id, message.external_id)
            if value
        }

        for job in self.store.list_email_jobs():
            if job.is_terminal():
                continue
            keys = {job.inbound_message_id, job.rfc_message_id, job.gmail_message_id}
            if not any(key and key in inbound_keys for key in keys):
                findings.append({
                    "code": "email_job_without_message",
                    "severity": "warning",
                    "detail": "non-terminal email job has no durable inbound message",
                    "refs": {"job_id": job.id, "status": job.status.value},
                })
            last = job.updated_at or job.claimed_at or job.created_at
            age = _age(last, now)
            if age is not None and age >= STALE_WORK_SECONDS:
                findings.append({
                    "code": "stale_email_job",
                    "severity": "warning",
                    "detail": f"email job has not progressed for {int(age)}s",
                    "refs": {"job_id": job.id, "status": job.status.value,
                             "attempts": job.attempt_count},
                })
            if job.status is EmailJobStatus.FAILED and job.retryable:
                findings.append({
                    "code": "owed_email_reply",
                    "severity": "warning",
                    "detail": "retryable email job still owes a response",
                    "refs": {"job_id": job.id, "reason": job.failure_reason},
                })
            elif job.status in (
                EmailJobStatus.DISCOVERED,
                EmailJobStatus.CLAIMED,
                EmailJobStatus.GENERATING,
                EmailJobStatus.READY_TO_SEND,
                EmailJobStatus.HELD,
            ):
                findings.append({
                    "code": "owed_email_reply",
                    "severity": "warning",
                    "detail": "non-terminal email job still owes a response",
                    "refs": {"job_id": job.id, "status": job.status.value,
                             "reason": job.failure_reason},
                })

        for message in messages:
            if (message.direction is MessageDirection.INBOUND
                    and message.status is MessageStatus.RECEIVED
                    and str(message.authorization or "").startswith("deferred:")
                    and not message_answered(self.store, message)):
                code = ("deferred_inbound" if message.relationship_id
                        else "deferred_inbound_unlinked")
                findings.append({
                    "code": code,
                    "severity": "warning",
                    "detail": ("recorded inbound remains deferred and unanswered"
                               if message.relationship_id else
                               "deferred inbound has no provable relationship link"),
                    "refs": {"message_id": message.id,
                             "reason": message.authorization.removeprefix("deferred:")},
                })

        # Thread-COVERED but individually unanswered inbounds. Newest-wins
        # batching answers a conversation on its latest message, which is the
        # correct behaviour, but an earlier unanswered message in the same
        # thread stays invisible to both retry and diagnosis without this.
        # Informational only: maintenance never marks it answered, since a
        # later thread reply is not proof the earlier question was addressed.
        for message in messages:
            if (message.direction is MessageDirection.INBOUND
                    and message.channel == "email"
                    and not message_answered(self.store, message)
                    and not str(message.authorization or "").startswith("deferred:")
                    and self._thread_answered_later(messages, message)):
                findings.append({
                    "code": "inbound_covered_by_thread_answer",
                    "severity": "info",
                    "detail": ("inbound was never answered individually; a later "
                               "message in the same thread was answered"),
                    "refs": {"message_id": message.id, "thread_id": message.thread_id},
                })
        return findings

    def _thread_answered_later(self, messages: list[Any], message: Any) -> bool:
        if not message.thread_id:
            return False
        message_time = _parse(message.created_at or message.received_at)
        for other in messages:
            if (other.direction is MessageDirection.INBOUND
                    and other.id != message.id
                    and other.thread_id == message.thread_id
                    and message_answered(self.store, other)):
                other_time = _parse(other.created_at or other.received_at)
                if message_time is None or other_time is None or other_time >= message_time:
                    return True
        return False

    def _persist(self, report: dict[str, Any]) -> None:
        prior = self.store._storage.load(self.store.identity_id, MAINTENANCE_NAMESPACE) or {}
        fingerprint_input = [
            {"code": item.get("code"), "refs": item.get("refs", {})}
            for item in report["findings"]
        ]
        fingerprint = hashlib.sha256(
            json.dumps(fingerprint_input, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()[:20]
        prior_fingerprint = str(prior.get("fingerprint") or "")
        prior_status = str((prior.get("latest") or {}).get("status") or "")

        history = list(prior.get("history") or [])
        history.append(report)
        payload = {
            "latest": report,
            "history": history[-MAINTENANCE_HISTORY_LIMIT:],
            "fingerprint": fingerprint,
            "updated_at": report["checked_at"],
        }
        self.store._storage.save(self.store.identity_id, MAINTENANCE_NAMESPACE, payload)

        # Notify only on a changed diagnosis, plus one recovery event. A
        # repeated tick must not create an unbounded notification storm.
        if report["status"] == "degraded" and fingerprint != prior_fingerprint:
            self.store.append_notification(NotificationEntry(
                kind="self_diagnosis",
                summary=(f"Self-diagnosis found {len(report['findings'])} "
                         "maintenance issue(s)."),
                refs={"evidence": MAINTENANCE_NAMESPACE,
                      "fingerprint": fingerprint},
            ))
        elif report["status"] == "healthy" and prior_status == "degraded":
            self.store.append_notification(NotificationEntry(
                kind="self_recovery",
                summary="Self-maintenance cleared the previously diagnosed issues.",
                refs={"evidence": MAINTENANCE_NAMESPACE},
            ))


def load_maintenance_report(storage: Any, identity_id: str) -> Optional[dict[str, Any]]:
    """Return the latest persisted report, or None when no cycle has run."""
    try:
        data = storage.load(identity_id, MAINTENANCE_NAMESPACE) or {}
    except Exception:
        return None
    latest = data.get("latest")
    return latest if isinstance(latest, dict) else None
