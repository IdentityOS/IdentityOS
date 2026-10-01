"""Runtime health, computed from liveness evidence rather than optimism.

The distinction this exists to make: "Aster is alive but has nothing to do"
versus "Aster is dead and nobody noticed". A laptop-specific capability going
away (a local Firefox bridge, a desktop app) is a *subsystem* degradation and
must never make the identity look offline. Conversely, an email poller that
stopped polling while the process still runs is exactly the failure that
produced an unexplained unanswered message, so it has to be visible as its own
signal.

Every field is derived from a persisted record with a timestamp: a presence
heartbeat, the ingest heartbeat, the durable email job ledger, and adapter
health. Nothing is reported healthy because it is expected to be.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

#: A presence heartbeat older than this means the process is almost certainly
#: gone. Matches the presence module's own staleness threshold.
HEARTBEAT_STALE_SECONDS = 900.0

#: An operator loop that has not completed a tick in this long is degraded even
#: though the process is alive and heartbeating.
LOOP_STALE_SECONDS = 1800.0

#: A message sitting unprocessed this long is a real backlog, not a slow model.
STALLED_JOB_SECONDS = 900.0

_HEALTHY = "healthy"
_DEGRADED = "degraded"
_OFFLINE = "offline"
_UNKNOWN = "unknown"


def _parse(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _age(value: Any, now: datetime) -> Optional[float]:
    moment = _parse(value)
    if moment is None:
        return None
    return round((now - moment).total_seconds(), 3)


def _subsystem(name: str, state: str, detail: str = "",
               last_ok: Any = None, now: Optional[datetime] = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    return {
        "name": name,
        "state": state,
        "detail": detail,
        "last_ok_at": last_ok or None,
        "age_seconds": _age(last_ok, now),
    }


def _email_capability_installed(storage: Any, identity_id: str) -> bool:
    """Whether the email transport is actually installed for this identity."""
    if storage is None:
        return False
    try:
        record = storage.load(identity_id, "capabilities") or {}
    except Exception:
        return False
    installed = record.get("installed") or []
    for entry in installed:
        if isinstance(entry, dict) and entry.get("id") == "email":
            return True
        if entry == "email":
            return True
    return False


def _device_bridge_status() -> dict[str, str]:
    """Whether this host can drive a real desktop browser.

    This is the capability that makes the conceptual distinction concrete: the
    identity runtime is portable, but *driving a human's logged-in browser* is
    tied to that human's machine. Aster remains alive without it. When the
    bridge is unavailable the answer is "this host has no desktop browser", not
    "Aster is offline" — and the status must never be inferred from the
    identity's health.
    """
    try:
        from core.capabilities.browser.firefox_profiles import list_firefox_profiles
    except Exception as exc:
        return {"state": _UNKNOWN, "detail": f"browser capability unavailable: {exc}"[:200]}
    try:
        profiles = list(list_firefox_profiles())
    except Exception as exc:
        return {"state": _UNKNOWN, "detail": f"profile scan failed: {exc}"[:200]}
    if profiles:
        return {
            "state": _HEALTHY,
            "detail": f"{len(profiles)} browser profile(s) reachable on this host",
        }
    return {
        "state": _DEGRADED,
        "detail": "no desktop browser profile on this host; identity runtime unaffected",
    }


def _adapter_health(storage: Any, identity_id: str) -> list[dict[str, Any]]:
    """Per-provider health, so one dead provider is visible without implying
    the identity is dead."""
    out: list[dict[str, Any]] = []
    try:
        from core.adapters.registry import AdapterRegistry

        registry = AdapterRegistry()
        adapters = registry.available() if hasattr(registry, "available") else []
    except Exception:
        return out
    for adapter in adapters or []:
        name = getattr(adapter, "name", "") or type(adapter).__name__
        model = str(getattr(adapter, "model", "") or "")
        state = _UNKNOWN
        detail = ""
        probe = getattr(adapter, "health_check", None)
        if callable(probe):
            try:
                result = probe()
                ok = result if isinstance(result, bool) else bool(
                    getattr(result, "ok", getattr(result, "healthy", False)))
                state = _HEALTHY if ok else _OFFLINE
                detail = "" if ok else str(
                    getattr(result, "error", "") or getattr(result, "detail", ""))[:200]
            except Exception as exc:
                state = _OFFLINE
                detail = f"{type(exc).__name__}: {exc}"[:200]
        out.append({"name": name, "model": model, "state": state, "detail": detail})
    return out


def _is_policy_hold(reason: Any) -> bool:
    """HELD jobs blocked on a human authorization decision are not stalled work.

    A policy hold means Aster noticed something consequential and correctly
    refused to act alone. Counting it with the stuck set would mark the
    subsystem broken for doing exactly what the rules require.
    """
    marker = str(reason or "").lower()
    return (
        "consequential commitments" in marker
        or "requires human authorization" in marker
        or "approval" in marker
    )


def runtime_health(store: Any, presence_store: Any = None, *,
                   storage: Any = None, identity_id: str = "aster",
                   now: Optional[datetime] = None) -> dict[str, Any]:
    """Full health payload: overall state plus every subsystem's evidence.

    ``overall`` is the worst of the *identity-critical* subsystems only. Device
    bridges and optional adapters are reported but never allowed to make the
    identity look offline, because an identity that stops existing when a
    laptop sleeps is not a persistent identity.
    """
    now = now or datetime.now(timezone.utc)
    subsystems: list[dict[str, Any]] = []

    # ── identity runtime ────────────────────────────────────────────────
    presence_record = None
    heartbeat_age = None
    loop_age = None
    if presence_store is not None:
        try:
            presence_record = presence_store.record()
        except Exception:
            presence_record = None
    if presence_record:
        heartbeat_age = _age(presence_record.get("last_heartbeat"), now)
        loop_age = _age(presence_record.get("last_tick_at"), now)
        if heartbeat_age is None:
            runtime_state = _OFFLINE
            runtime_detail = "no heartbeat recorded"
        elif heartbeat_age > HEARTBEAT_STALE_SECONDS:
            runtime_state = _OFFLINE
            runtime_detail = f"heartbeat stale by {int(heartbeat_age)}s"
        elif loop_age is not None and loop_age > LOOP_STALE_SECONDS:
            runtime_state = _DEGRADED
            runtime_detail = f"operator loop has not ticked for {int(loop_age)}s"
        else:
            runtime_state = _HEALTHY
            runtime_detail = ""
    else:
        runtime_state = _UNKNOWN
        runtime_detail = "presence record unavailable"
    subsystems.append(_subsystem(
        "identity_runtime", runtime_state, runtime_detail,
        (presence_record or {}).get("last_heartbeat"), now))

    # ── persistent store ────────────────────────────────────────────────
    store_state, store_detail = _HEALTHY, ""
    try:
        store.refresh_controls()
    except Exception as exc:
        store_state, store_detail = _OFFLINE, f"{type(exc).__name__}: {exc}"[:200]
    subsystems.append(_subsystem("persistent_store", store_state, store_detail, None, now))

    # ── email ingest ────────────────────────────────────────────────────
    # Only a *configured* transport can be expected to report ingest health.
    # If email is not installed, "no heartbeat" means "nothing to poll", which
    # is not a degraded identity.
    email_configured = _email_capability_installed(storage, identity_id)
    ingest_state, ingest_detail = _UNKNOWN, "no ingest heartbeat recorded"
    ingest_at = None
    if not email_configured:
        ingest_state, ingest_detail = "not_configured", "email capability not installed"
    elif storage is not None:
        try:
            record = storage.load(identity_id, "operations.ingest_health")
        except Exception:
            record = None
        if record:
            ingest_at = record.get("at")
            age = _age(ingest_at, now)
            if record.get("ok"):
                ingest_state = _HEALTHY
                ingest_detail = f"last poll fetched {record.get('fetched', 0)} message(s)"
            else:
                ingest_state = _OFFLINE
                ingest_detail = str(record.get("error", ""))[:200] or "last poll failed"
            if age is not None and age > STALLED_JOB_SECONDS:
                ingest_state = _DEGRADED
                ingest_detail = f"no successful poll for {int(age)}s"
        else:
            ingest_state = _DEGRADED
            ingest_detail = "email configured but no poll has ever been recorded"
    subsystems.append(_subsystem("email_ingest", ingest_state, ingest_detail, ingest_at, now))

    # ── email jobs: the honest "still owed an answer" signal ────────────
    jobs_summary: dict[str, Any] = {"total": 0, "by_status": {}, "awaiting_response": []}
    try:
        jobs = list(store.list_email_jobs())
        jobs_summary["total"] = len(jobs)
        by_status: dict[str, int] = {}
        for job in jobs:
            by_status[job.status.value] = by_status.get(job.status.value, 0) + 1
            # A retryable FAILED job still owes a response. Restricting this to
            # named in-flight statuses made health claim "no unanswered
            # inbound" precisely when a model/transport outage left work to
            # retry.
            # Policy-held jobs await a human approval decision, not a fix from
            # the runtime. Surface them separately so "awaiting your decision"
            # is not counted with the genuinely stuck ones.
            if not job.is_terminal():
                entry = {
                    "job_id": job.id,
                    "inbound_message_id": job.inbound_message_id,
                    "gmail_message_id": job.gmail_message_id or None,
                    "subject": job.subject or None,
                    "sender": job.sender or None,
                    "status": job.status.value,
                    "attempts": job.attempt_count,
                    "reason": job.failure_reason or None,
                    "last_activity": job.updated_at,
                    "age_seconds": _age(job.updated_at, now),
                }
                if job.status.value == "held" and _is_policy_hold(job.failure_reason):
                    jobs_summary.setdefault("awaiting_authorization", []).append(entry)
                else:
                    jobs_summary["awaiting_response"].append(entry)
        jobs_summary["by_status"] = by_status
    except Exception as exc:
        jobs_summary["error"] = f"{type(exc).__name__}: {exc}"[:200]

    stalled = [item for item in jobs_summary["awaiting_response"]
               if (item.get("age_seconds") or 0) > STALLED_JOB_SECONDS]
    jobs_summary["stalled"] = len(stalled)

    if not email_configured:
        jobs_state, jobs_detail = "not_configured", "email capability not installed"
    elif jobs_summary["awaiting_response"]:
        jobs_state = _DEGRADED
        jobs_detail = (f"{len(jobs_summary['awaiting_response'])} message(s) awaiting "
                       f"a response, {len(stalled)} stalled")
    elif jobs_summary.get("awaiting_authorization"):
        jobs_state = _HEALTHY
        jobs_detail = (f"{len(jobs_summary['awaiting_authorization'])} message(s) "
                       "awaiting your authorization")
    else:
        jobs_state = _HEALTHY
        jobs_detail = "no unanswered inbound"
    subsystems.append(_subsystem("email_sender", jobs_state, jobs_detail, None, now))

    # ── autonomous self-maintenance ────────────────────────────────────
    try:
        from .maintenance import load_maintenance_report

        maintenance = load_maintenance_report(storage, identity_id) if storage is not None else None
    except Exception as exc:
        maintenance = {"status": _UNKNOWN,
                       "findings": [{"detail": f"{type(exc).__name__}: {exc}"[:200]}]}
    if maintenance is None:
        maintenance_state = _UNKNOWN
        maintenance_detail = "no self-maintenance cycle recorded"
        maintenance_at = None
    else:
        maintenance_state = str(maintenance.get("status") or _UNKNOWN)
        maintenance_at = maintenance.get("checked_at")
        count = int((maintenance.get("summary") or {}).get("findings") or
                    len(maintenance.get("findings") or []))
        maintenance_detail = ("no diagnosed maintenance issues" if count == 0
                              else f"{count} diagnosed maintenance issue(s)")
        age = _age(maintenance_at, now)
        if age is not None and age > LOOP_STALE_SECONDS:
            maintenance_state = _DEGRADED
            maintenance_detail = f"self-maintenance stale by {int(age)}s"
    subsystems.append(_subsystem(
        "self_maintenance", maintenance_state, maintenance_detail,
        maintenance_at, now))

    # ── scheduler ───────────────────────────────────────────────────────
    if presence_record is not None:
        next_check = presence_record.get("next_check_at")
        scheduler_state = _HEALTHY if (loop_age is not None and loop_age <= LOOP_STALE_SECONDS) else _DEGRADED
        subsystems.append(_subsystem(
            "scheduler", scheduler_state, f"next check {next_check}" if next_check else "",
            presence_record.get("last_tick_at"), now))

    # ── event-driven Gmail ingest ───────────────────────────────────────
    # Reported separately from the IMAP poller: push is the primary path and
    # polling is the fallback, so "push is broken" and "mail is not arriving"
    # are different faults with different fixes.
    try:
        from core.capabilities.email.gmail_push import GmailPushConfig, GmailPushService

        push_config = GmailPushConfig.from_env()
        if not push_config.configured:
            push_report = {"status": "not_configured", "config": push_config.redacted()}
        else:
            push_service = GmailPushService(
                push_config, storage=storage, identity_id=identity_id,
                ingest=lambda items: None)
            push_report = push_service.health()
    except Exception as exc:
        push_report = {"status": "unknown", "error": f"{type(exc).__name__}: {exc}"[:200]}
    subsystems.append(_subsystem(
        "gmail_push", push_report.get("status", "unknown"),
        (f"watch expires {push_report['expiration']}"
         if push_report.get("expiration") else str(push_report.get("last_error", ""))[:160]),
        push_report.get("last_notification_at"), now))

    # ── model providers (reported, never fatal) ─────────────────────────
    providers = _adapter_health(storage, identity_id)
    for provider in providers:
        subsystems.append(_subsystem(
            f"model:{provider['name']}", provider["state"], provider["detail"], None, now))

    # ── optional device capabilities (never identity-critical) ───────────
    device = _device_bridge_status()
    subsystems.append(_subsystem("device_bridge", device["state"], device["detail"], None, now))

    # ── overall ─────────────────────────────────────────────────────────
    # "not_configured" and "unknown" are distinct: a transport that is not
    # installed cannot be failing, and unknown evidence is not healthy
    # evidence. Both are excluded from the offline test so an absent optional
    # capability never kills the identity.
    critical = {"identity_runtime", "persistent_store", "email_ingest", "email_sender"}
    # Existing identities have no maintenance evidence until the first tick.
    # Unknown is honest but not itself a regression; once a report exists, a
    # degraded diagnosis is identity-critical and affects overall health.
    if maintenance_state != _UNKNOWN:
        critical.add("self_maintenance")
    critical_states = [s["state"] for s in subsystems if s["name"] in critical]
    if _OFFLINE in critical_states:
        overall = _OFFLINE
    elif _DEGRADED in critical_states:
        overall = _DEGRADED
    elif _UNKNOWN in critical_states:
        # Unknown evidence is not healthy evidence, but it is also not proof of
        # death — report degraded and let the reason say which subsystem.
        overall = _DEGRADED
    else:
        overall = _HEALTHY

    return {
        "overall": overall,
        "checked_at": now.isoformat(),
        "last_heartbeat": (presence_record or {}).get("last_heartbeat"),
        "heartbeat_age_seconds": heartbeat_age,
        "loop_age_seconds": loop_age,
        "subsystems": subsystems,
        "email_jobs": jobs_summary,
        "providers": providers,
        "gmail_push": push_report,
        "maintenance": maintenance,
    }
