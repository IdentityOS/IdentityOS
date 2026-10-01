"""
core/operations/email_jobs.py

Durable email response jobs: one inbound Gmail message owns at most one
normal outbound response, across ticks, restarts, retries, and crashes.

Lifecycle (all transitions persisted through :class:`OperationsStore`)::

    DISCOVERED → CLAIMED → GENERATING → READY_TO_SEND → SENT → COMPLETED
         ↘ FAILED (retryable, bounded) → back to CLAIMED on retry

Invariants enforced here:

- Idempotency key = inbound Gmail message ID (never subject/sender/time).
- One worker owns a job: claims carry ``run_id:pid``; a fresh claim by a
  live owner is refused; stale claims (dead owner or aged out) are
  reclaimable. In-process threads serialize on the store lock.
- Crash-after-send: a restart finding READY_TO_SEND/SENT first looks for an
  existing outbound response (persisted linkage, then Gmail Sent search);
  only a provably-unsent job regenerates. At-least-once across the
  transport boundary is unavoidable without a provider idempotency token;
  the reconciliation window is narrowed to that boundary.
- No silent drops: every terminal state records outcome, attempts, and
  failure reasons; retryable failures stay retryable with bounded attempts.

Structured transition logs carry IDs, hashes, and counts only — never
bodies, subjects, credentials, or secret contents.
"""

from __future__ import annotations

import hashlib
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from .models import EmailJob, EmailJobStatus, utcnow

logger = logging.getLogger("identityos.email_jobs")

STALE_CLAIM_SECONDS = 900.0
MAX_SEND_ATTEMPTS = 3


def new_run_id() -> str:
    """Unique owner id for this worker process run."""
    return f"{uuid.uuid4().hex[:12]}:{os.getpid()}"


def _now_iso() -> str:
    return utcnow().isoformat()


def _parse_ts(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _age_seconds(value: Any, now: Optional[datetime] = None) -> Optional[float]:
    parsed = _parse_ts(value)
    if parsed is None:
        return None
    now = now or datetime.now(timezone.utc)
    return (now - parsed).total_seconds()


def _owner_pid(owner: str) -> Optional[int]:
    """Extract the pid from a claim owner id.

    Format is ``<run>:<pid>[:<worker>]``. The worker suffix distinguishes two
    threads in one process; without it both look like the same owner and
    ``claim_job`` treats the second as reentrant, letting two threads generate
    and send a reply to the same message. The pid stays in the second position
    so records written before the suffix existed still parse.
    """
    parts = str(owner or "").split(":")
    # Prefer the documented pid position; fall back to the final segment so a
    # bare or unexpected shape still resolves rather than silently reporting
    # "unknown", which would make every claim look alive.
    candidates = parts[1:2] or parts[-1:]
    for part in candidates:
        try:
            pid = int(part)
        except (TypeError, ValueError):
            continue
        return pid if pid > 0 else None
    return None


def _owner_alive(owner: str) -> Optional[bool]:
    """Best-effort liveness of a claim owner (None when unknowable)."""
    pid = _owner_pid(owner)
    if pid is None or pid <= 0:
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True


def log_transition(job: EmailJob, event: str, **fields: Any) -> None:
    """Structured, secret-free transition log for post-incident reconstruction."""
    safe = {
        "job": job.id,
        "inbound": (job.inbound_message_id or "")[:40],
        "thread": (job.thread_id or "")[:40],
        "status": job.status.value,
        "event": event,
        "attempt": job.attempt_count,
        "outbound": (job.outbound_message_id or "")[:40],
    }
    for key, value in fields.items():
        if key in ("body", "subject", "credentials", "secret", "token", "password"):
            continue
        safe[key] = value
    logger.info("email_job %s", " ".join(f"{k}={v}" for k, v in safe.items()))


def context_version(
    *,
    body_sha256: str = "",
    profile_fetched_at: str = "",
    project_fingerprint: str = "",
    model_id: str = "",
) -> str:
    """Fingerprint of the inputs a response was generated from.

    Recorded on every job so two executions producing different answers can
    be traced to different inputs instead of mistaken for flakiness.
    """
    digest = hashlib.sha256()
    for part in (body_sha256, profile_fetched_at, project_fingerprint, model_id):
        digest.update(str(part or "").encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()[:16]


def response_hash(body: str) -> str:
    """Stable hash of a generated response body (change detection)."""
    return hashlib.sha256((body or "").encode("utf-8")).hexdigest()[:16]


def ensure_job(
    store: Any,
    *,
    inbound_message_id: str,
    thread_id: str = "",
    sender: str = "",
    received_at: str = "",
    rfc_message_id: str = "",
    gmail_message_id: str = "",
    gmail_thread_id: str = "",
    subject: str = "",
    gmail_received_at: str = "",
) -> EmailJob:
    """Get-or-create the DISCOVERED job for an inbound message.

    The idempotency key is the provider's immutable id when available
    (``gmail_message_id``), else the RFC ``Message-ID``. Discovery is
    timestamped on first sight so poll wait is measurable separately from
    model and send time.
    """
    key = (gmail_message_id or inbound_message_id or rfc_message_id or "").strip()
    existing = store.find_email_job_by_inbound(key)
    if existing is not None:
        if gmail_message_id and not existing.gmail_message_id:
            existing.gmail_message_id = gmail_message_id
        if gmail_thread_id and not existing.gmail_thread_id:
            existing.gmail_thread_id = gmail_thread_id
        if rfc_message_id and not existing.rfc_message_id:
            existing.rfc_message_id = rfc_message_id
        if subject and not existing.subject:
            existing.subject = subject[:300]
        if gmail_received_at and not existing.gmail_received_at:
            existing.gmail_received_at = gmail_received_at
            store.save_email_job(existing)
        return existing
    job = EmailJob(
        inbound_message_id=key,
        rfc_message_id=rfc_message_id,
        gmail_message_id=gmail_message_id,
        gmail_thread_id=gmail_thread_id,
        thread_id=thread_id or gmail_thread_id,
        sender=sender,
        subject=(subject or "")[:300],
        received_at=received_at,
        gmail_received_at=gmail_received_at,
        discovered_at=utcnow().isoformat(),
    )
    store.save_email_job(job)
    log_transition(job, "discovered")
    return job


def claim_job(
    store: Any,
    job: EmailJob,
    owner: str,
    *,
    stale_after_seconds: float = STALE_CLAIM_SECONDS,
) -> tuple[bool, str]:
    """Attempt exclusive ownership. Returns (granted, reason).

    Terminal jobs are never re-claimed. A fresh claim by a live owner is
    refused so two workers cannot generate twice. Stale claims (dead owner
    or aged out) are reclaimable for crash recovery.
    """
    fresh = store.get_email_job(job.id) or job
    if fresh.is_terminal():
        return False, f"terminal:{fresh.status.value}"
    if fresh.status in (
        EmailJobStatus.CLAIMED,
        EmailJobStatus.GENERATING,
        EmailJobStatus.READY_TO_SEND,
        EmailJobStatus.SENT,
    ):
        if fresh.claimed_by and fresh.claimed_by != owner:
            alive = _owner_alive(fresh.claimed_by)
            age = _age_seconds(fresh.claimed_at)
            if alive is not False and (age is None or age < stale_after_seconds):
                return False, "claimed-by-live-owner"
        # Reclaim (stale/crashed owner) or reentrant (same owner).
    fresh.status = EmailJobStatus.CLAIMED
    fresh.claimed_by = owner
    fresh.claimed_at = _now_iso()
    fresh.mark("claimed_at", fresh.claimed_at)
    fresh.attempt_count = int(fresh.attempt_count or 0) + 1
    fresh.failure_reason = ""
    store.save_email_job(fresh)
    job.__dict__.update(fresh.__dict__)
    log_transition(fresh, "claimed", owner=owner)
    return True, "claimed"


def advance_job(
    store: Any,
    job: EmailJob,
    status: EmailJobStatus,
    *,
    context_version_value: str = "",
    generated_response_hash: str = "",
    outbound_message_id: str = "",
    failure_reason: str = "",
    retryable: bool = True,
) -> EmailJob:
    """Persist a lifecycle transition with its evidence."""
    fresh = store.get_email_job(job.id) or job
    fresh.status = status
    if context_version_value:
        fresh.context_version = context_version_value
    if generated_response_hash:
        fresh.generated_response_hash = generated_response_hash
    if outbound_message_id:
        fresh.outbound_message_id = outbound_message_id
    if failure_reason:
        fresh.failure_reason = failure_reason[:300]
    if status is EmailJobStatus.GENERATING:
        fresh.mark("generation_started_at")
    elif status is EmailJobStatus.READY_TO_SEND:
        fresh.mark("generation_finished_at")
    if status is EmailJobStatus.FAILED:
        fresh.retryable = bool(retryable)
        if not fresh.retryable or fresh.attempt_count >= fresh.max_attempts:
            log_transition(fresh, "failed-terminal", reason=fresh.failure_reason)
        else:
            log_transition(fresh, "failed-retryable", reason=fresh.failure_reason)
    elif status is EmailJobStatus.HELD:
        # A reply is still owed; policy, not failure, is blocking it.
        fresh.retryable = True
        log_transition(fresh, "held-awaiting-approval", reason=fresh.failure_reason)
    elif status is EmailJobStatus.SENT:
        fresh.mark("smtp_accepted_at")
        log_transition(fresh, "sent")
    elif status is EmailJobStatus.COMPLETED:
        fresh.mark("completed_at")
        log_transition(fresh, "completed")
    else:
        log_transition(fresh, f"advance:{status.value}")
    store.save_email_job(fresh)
    job.__dict__.update(fresh.__dict__)
    return fresh


def fail_job(store: Any, job: EmailJob, reason: str, *, retryable: bool = True) -> EmailJob:
    """Record a failure; terminal unless retryable with attempts left."""
    retryable = bool(retryable) and int(job.attempt_count or 0) < int(job.max_attempts or MAX_SEND_ATTEMPTS)
    return advance_job(store, job, EmailJobStatus.FAILED,
                       failure_reason=reason, retryable=retryable)


def stamp_job(store: Any, job: Optional[EmailJob], stage: str, *,
              provider: str = "", model: str = "") -> Optional[EmailJob]:
    """Persist one pipeline timestamp (and provider identity) on a job.

    Kept separate from :func:`advance_job` because the send path records
    transport timestamps around work that does not change job status, and
    those timestamps are what separate "waiting to be picked up" from
    "the model was slow" in the reported latency.
    """
    if job is None:
        return None
    fresh = store.get_email_job(job.id) or job
    fresh.mark(stage)
    if provider:
        fresh.provider = provider[:80]
    if model:
        fresh.model = model[:120]
    store.save_email_job(fresh)
    job.__dict__.update(fresh.__dict__)
    return fresh


def find_sent_reply(
    search_sent: Optional[Callable[[str], Optional[str]]],
    in_reply_to: str,
) -> Optional[str]:
    """Best-effort Gmail Sent-folder lookup for an already-accepted reply.

    Returns the provider message id when found, else None. Any failure
    (offline, unsupported) means 'unknown' — callers must treat that as
    *not found* only alongside their durable records, never as proof of
    absence on its own.
    """
    if not search_sent or not in_reply_to:
        return None
    try:
        found = search_sent(in_reply_to)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("sent-search failed: %s", exc)
        return None
    return str(found) if found else None


def reconcile_job(
    store: Any,
    job: EmailJob,
    owner: str,
    *,
    search_sent: Optional[Callable[[str], Optional[str]]] = None,
    stale_after_seconds: float = STALE_CLAIM_SECONDS,
) -> dict[str, Any]:
    """Reconcile one non-terminal job after restart or before a risky send.

    - COMPLETED/terminal: untouched.
    - SENT: adopt (transport accepted; complete bookkeeping now).
    - READY_TO_SEND/GENERATING/CLAIMED with live owner: untouched.
    - Stale or FAILED-retryable: look for an existing outbound response
      (persisted linkage, then Sent search); adopt when found, else reset
      to DISCOVERED for one more honest attempt.
    Returns a small action report; never sends anything itself.
    """
    fresh = store.get_email_job(job.id) or job
    if fresh.is_terminal():
        return {"job": fresh.id, "action": "none-terminal"}
    if fresh.status is EmailJobStatus.SENT:
        advance_job(store, fresh, EmailJobStatus.COMPLETED)
        return {"job": fresh.id, "action": "adopted-sent"}
    if fresh.status is EmailJobStatus.HELD:
        # Blocked by policy, not by failure. Release the claim so the message
        # is retried once the operator is in a mode that can send it, instead
        # of stranding it behind a dead claim.
        if fresh.claimed_by and fresh.claimed_by != owner:
            alive = _owner_alive(fresh.claimed_by)
            age = _age_seconds(fresh.claimed_at)
            if alive is not False and (age is None or age < stale_after_seconds):
                return {"job": fresh.id, "action": "none-held-live"}
        advance_job(store, fresh, EmailJobStatus.DISCOVERED,
                    failure_reason=fresh.failure_reason)
        return {"job": fresh.id, "action": "released-held"}
    if fresh.claimed_by and fresh.claimed_by != owner:
        alive = _owner_alive(fresh.claimed_by)
        age = _age_seconds(fresh.claimed_at)
        if alive is not False and (age is None or age < stale_after_seconds):
            return {"job": fresh.id, "action": "none-claimed-live"}
    if fresh.status in (EmailJobStatus.READY_TO_SEND, EmailJobStatus.GENERATING,
                        EmailJobStatus.CLAIMED, EmailJobStatus.DISCOVERED,
                        EmailJobStatus.FAILED, EmailJobStatus.HELD):
        if fresh.outbound_message_id:
            advance_job(store, fresh, EmailJobStatus.COMPLETED)
            return {"job": fresh.id, "action": "adopted-recorded-send"}
        found = None
        if fresh.status is EmailJobStatus.READY_TO_SEND:
            found = find_sent_reply(search_sent, fresh.inbound_message_id)
        if found:
            fresh.outbound_message_id = found
            advance_job(store, fresh, EmailJobStatus.COMPLETED)
            return {"job": fresh.id, "action": "adopted-gmail-sent", "outbound": found}
        if fresh.status is EmailJobStatus.FAILED and not fresh.retryable:
            return {"job": fresh.id, "action": "none-failed-terminal"}
        if fresh.status is EmailJobStatus.FAILED and fresh.attempt_count >= fresh.max_attempts:
            fresh.retryable = False
            advance_job(store, fresh, EmailJobStatus.FAILED, failure_reason=fresh.failure_reason,
                        retryable=False)
            return {"job": fresh.id, "action": "none-attempts-exhausted"}
        advance_job(store, fresh, EmailJobStatus.DISCOVERED)
        return {"job": fresh.id, "action": "reset-discovered"}
    return {"job": fresh.id, "action": "none"}
