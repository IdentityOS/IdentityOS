"""Bounded model-free worker. Durable records are the queue; idle does no reasoning."""

from types import SimpleNamespace
from core.executive.engine import ExecutiveRuntime


def tick(runtime, provider):
    session = runtime.bind(provider)
    negotiated = runtime.negotiation.provider_tick(session)
    if negotiated:
        return negotiated
    executive = ExecutiveRuntime(runtime.storage, runtime.registry)
    executive._ctx(provider, runtime=SimpleNamespace(services=runtime))
    executive.recover(provider)
    # One provider action per tick; accepted terms are checked at execution.
    for job in runtime.store.jobs(provider):
        if job["provider"] != provider:
            continue
        try:
            if job["state"] == "REQUESTED":
                runtime.quote(session, job["id"])
                return "quoted"
            if job["state"] == "ACCEPTED":
                runtime.enqueue(session, job["id"], executive)
                executive.process_ready(provider, max_steps=1)
                return runtime.store.job(job["id"])["state"]
        except (PermissionError, ValueError) as exc:
            with runtime.store.transaction() as db:
                db.execute(
                    "UPDATE jobs SET state='BLOCKED' WHERE id=? AND state IN ('REQUESTED','ACCEPTED')", (job["id"],)
                )
                runtime.store.event(db, provider, "worker_blocked", {"reason": type(exc).__name__}, job["id"])
            return "blocked"
    return "idle"


def requester_tick(runtime, identity):
    session = runtime.bind(identity)
    for spec in runtime.negotiation.list(session):
        if spec['requester'] == identity and spec['state'] == 'AGREED' and spec['job'] is None:
            runtime.negotiation.start_job(session, spec['id'])
            return 'job_created'
    for job in runtime.store.jobs(identity):
        if job["requester"] != identity:
            continue
        try:
            if job["state"] == "QUOTED":
                runtime.accept_quote(session, job["id"])
                return "accepted"
            if job["state"] == "DELIVERED":
                return runtime.accept_delivery(session, job["id"])["state"]
        except (PermissionError, ValueError):
            # Keep quote reviewable; never change limits or spend on denial.
            return "principal_review_required"
    return "idle"
