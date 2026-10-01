"""Condition-based waiting in existing Need metadata; outcomes in provenance.

No planner, model, grants, or external calls. A check is not an attempted action.
"""

import hashlib
import json
from datetime import datetime, timezone
from uuid import uuid4
from .models import Need, NeedStatus, ProvenanceEntry, ProvenancePhase


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def iso(now):
    return datetime.fromtimestamp(now, timezone.utc).isoformat()


def meaningful(value):
    if isinstance(value, dict):
        return {
            k: meaningful(v)
            for k, v in value.items()
            if k
            not in {
                "observed_at",
                "last_checked",
                "last_seen",
                "timestamp",
                "checked_at",
                "fetched_at",
                "heartbeat",
                "uptime",
            }
        }
    if isinstance(value, list):
        return [meaningful(v) for v in value]
    return value


class Progress:
    def __init__(self, store, objective):
        self.store, self.objective = store, objective
        self.now = datetime.now(timezone.utc).timestamp()
        self.resources = {}
        self.changes = []

    def begin(self, now):
        self.now = now.timestamp()
        self.started = now.isoformat()
        self.event_start = len(self.store.list_provenance())
        self.resources = {"model_calls": 0, "surface_reads": 0, "suppressed_operations": 0}
        self.changes = []

    def blocker(self, key):
        return next((n for n in self.store.list_needs() if n.metadata.get("blocker", {}).get("key") == key), None)

    def eligible(self, key, condition, *, now=None):
        now = self.now if now is None else now
        need = self.blocker(key)
        if need is None:
            return True
        b = need.metadata["blocker"]
        b["last_checked"] = iso(now)
        b["check_count"] = b.get("check_count", 0) + 1
        changed = b.get("condition") != condition
        due = b.get("next_eligible_retry") is not None and now >= b["next_eligible_retry"]
        allowed = b["state"] in {"RESOLVED", "SUPERSEDED"} or changed or due or b.get("force_retry", False)
        if not allowed:
            b["suppressed_count"] = b.get("suppressed_count", 0) + 1
            self.resources["suppressed_operations"] = self.resources.get("suppressed_operations", 0) + 1
        b["force_retry"] = False
        self.store.update_need(need)
        return allowed

    def wait(self, key, classification, condition, description, *, retry=None, principal=False, reason="", now=None):
        now = self.now if now is None else now
        need = self.blocker(key)
        if need is None:
            # Reuse existing capability need instead of duplicating it during migration.
            skill = key.removeprefix("skill:")
            need = next(
                (
                    n
                    for n in self.store.list_needs()
                    if key.startswith("skill:") and n.category == "capability" and f"'{skill}'" in n.description
                ),
                None,
            )
        if need is None:
            need = Need(category="runtime_blocker", description=description, urgency=0.2, impact=0.5)
            self.store.add_need(need)
        previous = need.metadata.get("blocker", {})
        state = "PRINCIPAL_REQUIRED" if principal else ("RETRY_SCHEDULED" if retry is not None else "WAITING")
        changed = (
            previous.get("condition") != condition
            or previous.get("classification") != classification
            or previous.get("state") == "RESOLVED"
        )
        b = {
            **previous,
            "key": key,
            "fingerprint": fingerprint([key, classification]),
            "classification": classification,
            "state": state,
            "condition": condition,
            "description": description,
            "blocking_objective": self.objective,
            "first_seen": previous.get("first_seen", iso(now)),
            "last_checked": iso(now),
            "last_changed": iso(now) if changed else previous["last_changed"],
            "occurrence_count": previous.get("occurrence_count", 0) + 1,
            "next_eligible_retry": retry,
            "principal_action_required": principal,
            "retry_condition": reason or "Relevant prerequisite changes",
            "resolution_evidence": [],
        }
        need.metadata["blocker"] = b
        need.status = NeedStatus.OPEN
        self.store.update_need(need)
        if changed:
            self.changes.append({"kind": "blocker_changed", "key": key, "classification": classification})
        return b

    def resolve(self, key, evidence):
        need = self.blocker(key)
        if not need or need.metadata["blocker"]["state"] == "RESOLVED":
            return
        b = need.metadata["blocker"]
        b.update(state="RESOLVED", last_changed=iso(self.now), resolution_evidence=[evidence])
        need.status = NeedStatus.ADDRESSED
        self.store.update_need(need)
        self.changes.append({"kind": "blocker_resolved", "key": key, "evidence": evidence})

    def condition(self, domain, adapter=None):
        storage = self.store._storage
        identity = self.store.identity_id
        raw = storage.load(identity, "capabilities") or {}
        grants = storage.load(identity, "capability.permissions") or {}
        if domain == "authority":
            return fingerprint([raw, grants])
        from core.services.integration import existing_store

        service = existing_store(storage)
        catalog = service.rows("SELECT identity,document FROM services ORDER BY identity") if service else []
        value = [raw, grants, catalog]
        if domain == "principal":
            from adapters.configuration import describe_adapter

            controls = self.store.controls().to_dict()
            controls.pop("updated_at", None)
            value.extend([describe_adapter(adapter), controls])
        return fingerprint(value)  # Config/credential contents never emitted, only a digest.

    def finish(self, report, *, project_changed=False, surfaces_changed=False):
        waits = waiting(self.store)
        events = self.store.list_provenance()[self.event_start :]
        services = [e for e in events if e.action == "service.requester"]
        policy_waits = []
        for reference in report.escalations:
            policy_waits.append(
                {
                    "key": "policy:" + reference,
                    "description": "Principal review: " + reference,
                    "principal_action_required": True,
                    "retry_condition": "Principal reviews the pending action",
                    "next_eligible_retry": None,
                    "classification": "PRINCIPAL_REQUIRED",
                }
            )
        if any(s.get("reason") == "daily_budget_exhausted" for s in report.skipped):
            policy_waits.append(
                {
                    "key": "policy:outreach_budget",
                    "description": "Outreach daily budget exhausted",
                    "principal_action_required": False,
                    "retry_condition": "Outreach allowance resets",
                    "next_eligible_retry": None,
                    "classification": "DAILY_BUDGET_EXHAUSTED",
                }
            )
        waits += policy_waits
        completed = list(report.outreach_sent) + list(report.replies_sent) + list(report.follow_ups_sent)
        information = []
        if project_changed:
            observations = [e for e in events if e.action == "observe_project"]
            information.extend([e.summary for e in observations] or ["Project facts changed"])
            for event in observations:
                information.extend("Added: " + fact for fact in event.refs.get("added_facts", [])[:3])
                information.extend("Removed: " + fact for fact in event.refs.get("removed_facts", [])[:3])
        if surfaces_changed:
            information.append("External observation changed")
        information += ["New opportunity " + x for x in report.opportunities_created]
        information += ["Service workflow: " + e.result for e in services if e.result != "principal_review_required"]
        resolved = [x for x in self.changes if x["kind"] == "blocker_resolved"]
        progress = bool(completed or information or resolved)
        previous = projection(self.store)
        last = previous.get("last_meaningful_progress")
        if progress:
            last = {
                "at": iso(self.now),
                "summary": "; ".join(information)
                or (
                    "Completed " + str(len(completed)) + " objective action(s)"
                    if completed
                    else "Resolved an operational blocker"
                ),
            }
        outcome = {
            "cycle_id": uuid4().hex,
            "started_at": self.started,
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "objective": self.objective,
            "new_information": information,
            "actions_completed": completed,
            "actions_attempted": [{"action": e.action, "result": e.result, "evidence": e.id} for e in events],
            "state_changes": list(self.changes),
            "blockers_discovered": [x for x in self.changes if x["kind"] == "blocker_changed"],
            "blockers_resolved": resolved,
            "delegations": [{"state": e.result, "evidence": e.id} for e in services],
            "policy_waits": policy_waits,
            "external_effects": completed,
            "waiting_conditions": waits,
            "principal_decisions_needed": [b for b in waits if b["principal_action_required"]],
            "next_eligible_actions": [
                {"description": b["retry_condition"], "at": b["next_eligible_retry"]} for b in waits
            ],
            "resources": {
                **self.resources,
                "measurement_scope": "principal model requests and Culture Commons observation calls; other model paths/provider retries not yet instrumented",
            },
            "last_meaningful_progress": last,
            "safety": "No authority expansion performed; action policy remains authoritative",
            "result": "PROGRESS"
            if progress
            else (
                "DEGRADED"
                if report.errors
                else (
                    "PRINCIPAL_REQUIRED"
                    if any(b["principal_action_required"] for b in waits)
                    else ("WAITING" if waits else "NO_CHANGE")
                )
            ),
        }
        outcome["next_eligible_actions"].insert(
            0, {"description": "Inspect local project for changed facts at next scheduled cycle", "at": None}
        )
        self.store.append_provenance(
            ProvenanceEntry(
                phase=ProvenancePhase.CONTROL,
                action="cycle.outcome",
                summary="Autonomous cycle: " + outcome["result"],
                result=outcome["result"],
                refs={"outcome": outcome},
            )
        )
        return outcome


def waiting(store):
    keys = (
        "key",
        "classification",
        "state",
        "description",
        "first_seen",
        "last_checked",
        "last_changed",
        "occurrence_count",
        "suppressed_count",
        "next_eligible_retry",
        "retry_condition",
        "principal_action_required",
    )
    return [
        {k: b.get(k) for k in keys}
        for n in store.list_needs()
        if (b := n.metadata.get("blocker")) and b["state"] not in {"RESOLVED", "SUPERSEDED"}
    ]


def projection(store):
    outcomes = [
        e.refs["outcome"] for e in store.list_provenance() if e.action == "cycle.outcome" and "outcome" in e.refs
    ]
    latest = outcomes[-1] if outcomes else {}
    return {
        "latest": latest,
        "cycles": outcomes[-10:][::-1],
        "waiting": waiting(store) + latest.get("policy_waits", []),
        "last_meaningful_progress": latest.get("last_meaningful_progress"),
    }
