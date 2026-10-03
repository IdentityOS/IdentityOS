"""Durable specification exchange before a job exists. No natural-language executor."""

import json
import time

from core.services.store import encode, fingerprint, uid


class Negotiation:
    def __init__(self, runtime):
        self.runtime = runtime
        self.store = runtime.store

    def get(self, session, reference):
        actor = self.runtime._actor(session)
        rows = self.store.rows("SELECT * FROM negotiations WHERE id=?", (reference,))
        if not rows or actor not in (rows[0]["requester"], rows[0]["provider"]):
            raise PermissionError("unknown negotiation or wrong participant")
        row = rows[0]
        row["contract"] = json.loads(row["contract"])
        return row

    def list(self, session):
        actor = self.runtime._actor(session)
        return [
            self.get(session, r["id"])
            for r in self.store.rows(
                "SELECT id FROM negotiations WHERE requester=? OR provider=? ORDER BY created LIMIT 100", (actor, actor)
            )
        ]

    def _validate(self, actor, provider, contract):
        self.runtime._authorize_effect(actor, contract.get("skill", ""))
        self.runtime._authorize_effect(provider, contract.get("skill", ""))
        from runtime.sensitive import protect_explicit_secrets

        if protect_explicit_secrets(encode(contract))[1]:
            raise ValueError("credentials are not accepted in specifications")
        return self.runtime._contract(contract)

    def propose(self, session, provider, contract):
        actor = self.runtime._actor(session)
        contract = self._validate(actor, provider, contract)
        if provider == actor or provider not in [c["identity"] for c in self.runtime.discover(contract["service"])]:
            raise ValueError("provider unavailable")
        key = fingerprint({"requester": actor, "request_key": contract["request_key"]})
        with self.store.transaction() as db:
            existing = db.execute("SELECT * FROM negotiations WHERE dedupe=?", (key,)).fetchone()
            if existing:
                if existing["initial_hash"] != fingerprint(contract) or existing["provider"] != provider:
                    raise ValueError("proposal replay changed original specification")
                return existing["id"]
            ref, now = uid(), time.time()
            db.execute(
                "INSERT INTO negotiations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    ref,
                    key,
                    actor,
                    provider,
                    1,
                    "PROPOSED",
                    encode(contract),
                    fingerprint(contract),
                    actor,
                    None,
                    "",
                    now,
                    now,
                    "local-only; no authority transfer",
                ),
            )
            self.store.event(
                db, actor, "specification_proposed", {"hash": fingerprint(contract), "provider": provider}, ref
            )
            self.runtime._message(db, actor, provider, ref, "specification.proposed", {"revision": 1})
        return ref

    def respond(self, session, reference, revision, decision, *, contract=None):
        actor = self.runtime._actor(session)
        old = self.get(session, reference)
        if old["revision"] != revision:
            raise ValueError("stale specification revision")
        if decision not in {"ACCEPT", "COUNTERPROPOSE", "REQUEST_CLARIFICATION", "DECLINE"}:
            raise ValueError("invalid negotiation decision")
        if old["state"] in {"AGREED", "DECLINED"}:
            raise ValueError("negotiation already final")
        if actor == old["offered_by"] and decision != "DECLINE":
            raise PermissionError("other participant must respond")
        spec = old["contract"] if contract is None else self._validate(old["requester"], old["provider"], contract)
        if decision == "COUNTERPROPOSE":
            if contract is None or spec["request_key"] != old["contract"]["request_key"]:
                raise ValueError("counterproposal requires specification with original request key")
        elif contract is not None:
            raise ValueError("only counterproposals can modify terms")
        if decision == "ACCEPT":
            self._validate(old["requester"], old["provider"], spec)
            if not spec["acceptance"] or spec["constraints"]["effect"] != "local_transform":
                raise ValueError("acceptance criteria and authorized scope required")
            state = "AGREED"
        else:
            state = {
                "COUNTERPROPOSE": "PROPOSED",
                "REQUEST_CLARIFICATION": "CLARIFICATION_REQUIRED",
                "DECLINE": "DECLINED",
            }[decision]
        with self.store.transaction() as db:
            changed = db.execute(
                "UPDATE negotiations SET revision=revision+1,state=?,contract=?,offered_by=?,updated=? WHERE id=? AND revision=? AND state NOT IN ('AGREED','DECLINED')",
                (state, encode(spec), actor, time.time(), reference, revision),
            ).rowcount
            if not changed:
                raise ValueError("concurrent specification change")
            self.store.event(
                db,
                actor,
                "specification_" + decision.lower(),
                {"revision": revision + 1, "hash": fingerprint(spec)},
                reference,
            )
            peer = old["provider"] if actor == old["requester"] else old["requester"]
            self.runtime._message(
                db, actor, peer, reference, "specification." + decision.lower(), {"revision": revision + 1}
            )
        return self.get(session, reference)

    def start_job(self, session, reference):
        actor = self.runtime._actor(session)
        spec = self.get(session, reference)
        if actor != spec["requester"] or spec["state"] != "AGREED":
            raise PermissionError("requester and agreed specification required")
        # Idempotent request key closes crash window between job insertion and link.
        job = self.runtime._create_agreed_job(session, spec["provider"], spec["contract"], reference)
        with self.store.transaction() as db:
            changed = db.execute("UPDATE negotiations SET job=? WHERE id=? AND job IS NULL", (job, reference)).rowcount
            if changed:
                self.store.event(
                    db, actor, "agreement_job_created", {"job": job, "revision": spec["revision"]}, reference
                )
        return job

    def provider_tick(self, session, reference=None):
        actor = self.runtime._actor(session)
        for spec in ([self.get(session, reference)] if reference else self.list(session)):
            if spec["provider"] == actor and spec["state"] == "PROPOSED" and spec["offered_by"] != actor:
                c = spec["contract"]
                decision = (
                    "ACCEPT"
                    if c["acceptance"] and c["constraints"]["effect"] == "local_transform"
                    else "REQUEST_CLARIFICATION"
                )
                self.respond(session, spec["id"], spec["revision"], decision)
                return decision.lower()
        return None
