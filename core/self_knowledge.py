"""Read-only, identity-bound projection of operational truth.

No models, installation hooks, network probes, or mutable duplicate facts. Field
absence in a bounded or unavailable source is UNVERIFIED, never proof of absence.
"""

import hashlib
import json
import re
import sqlite3
import time
from datetime import datetime, timezone

from core.capabilities.registry import CapabilityRegistry
from core.services.integration import database_path
from runtime.sensitive import protect_explicit_secrets

SECTIONS = (
    "identity",
    "presence",
    "capabilities",
    "permissions",
    "services",
    "jobs",
    "agreements",
    "relationships",
    "economy",
    "artifacts",
    "recent_actions",
    "provenance",
    "needs",
)
MAX_ROWS = 30
MAX_NAMESPACE_BYTES = 512_000
TTL_SECONDS = 30


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


class ReadLimitError(Exception):
    pass


class SelfKnowledge:
    def __init__(self, storage, identity_id, *, scrub=None):
        self.storage = storage
        self.identity_id = identity_id
        if scrub is None:
            from core.secrets.store import SecretStore, default_secret_store_dir

            scrub = SecretStore(default_secret_store_dir()).scrub
        owner = getattr(scrub, "__self__", None)
        from core.secrets.store import SecretStore

        self._secret_owner = owner if isinstance(owner, SecretStore) else None
        self.scrub = scrub if self._secret_owner is None else None
        self._secrets = set()

    def load(self, namespace):
        # The JSON backend is document-based. Do not parse huge histories merely
        # to show status; report this limit rather than pretending history is empty.
        if hasattr(self.storage, "_ns_path"):
            path = self.storage._ns_path(self.identity_id, namespace)
            if path.exists() and path.stat().st_size > MAX_NAMESPACE_BYTES:
                raise ReadLimitError("namespace read budget exceeded")
        return self.storage.load(self.identity_id, namespace)

    def _clean(self, value):
        if isinstance(value, dict):
            return {self._clean(str(k)): self._clean(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._clean(v) for v in value]
        if not isinstance(value, str):
            return value
        if self.scrub:
            value = self.scrub(value)  # failure is closed by snapshot(), never ignored
        for secret in sorted(self._secrets, key=len, reverse=True):
            value = value.replace(secret, "[REDACTED]")
        value, protected = protect_explicit_secrets(value)
        for reference in protected:
            value = value.replace(reference, "[REDACTED]")
        value = re.sub(r"(?:secret(?:-ref)?://|https?://)\S+", "[private reference]", value)
        return value[:600]

    def _collect_secrets(self, value, sensitive=False):
        if isinstance(value, dict):
            for key, item in value.items():
                self._collect_secrets(
                    item,
                    sensitive
                    or bool(
                        re.search(r"key|token|password|secret|topic|credential|headers|login|host", str(key), re.I)
                    ),
                )
        elif isinstance(value, list):
            for item in value:
                self._collect_secrets(item, sensitive)
        elif sensitive and isinstance(value, str) and len(value) >= 4:
            self._secrets.add(value)

    def snapshot(self, sections=None):
        started = time.perf_counter()
        self._secrets = set()
        redaction_failed = False
        if self.scrub is not None:
            try:
                self.scrub("")
            except Exception:
                redaction_failed = True
        if self._secret_owner is not None:
            try:
                self._secrets.update(value for value, _ in self._secret_owner._redact_map())
            except Exception:
                redaction_failed = True
        selected = list(SECTIONS if sections is None else sections)
        if any(s not in SECTIONS for s in selected):
            raise ValueError("unknown self-state section")
        now = datetime.now(timezone.utc).isoformat()
        result = {
            "schema": "identityos.self.v1",
            "identity_id": self.identity_id,
            "observed_at": now,
            "valid_for_seconds": TTL_SECONDS,
            "execution_policy": "revalidate_at_action_time",
            "sections": {},
        }
        try:
            self._collect_secrets(self.load("capabilities") or {})
        except (ReadLimitError, ValueError, OSError):
            pass  # only allowlisted fields are emitted; configs are never projected
        for name in selected:
            try:
                if redaction_failed:
                    raise ValueError("sanitization unavailable")
                data, source, complete = getattr(self, "_" + name)()
                clean = self._clean(data)
                section = {
                    "kind": "FACT",
                    "status": "VERIFIED",
                    "source": source,
                    "complete": complete,
                    "data": clean,
                    "evidence_ref": f"self:{name}:{_digest(clean)[:20]}",
                }
            except Exception as exc:
                section = {
                    "kind": "UNKNOWN",
                    "status": "UNVERIFIED",
                    "source": name,
                    "complete": False,
                    "data": None,
                    "reason": "read_budget_exceeded" if isinstance(exc, ReadLimitError) else "source_unavailable",
                }
            result["sections"][name] = section
        result["revision"] = _digest(result["sections"])
        result["snapshot_id"] = _digest({"revision": result["revision"], "at": now, "identity": self.identity_id})
        result["latency_ms"] = round((time.perf_counter() - started) * 1000, 3)
        return result

    def _identity(self):
        spec = self.load("identity_spec")
        if not spec:
            raise ValueError("identity specification unavailable")
        return {k: spec.get(k) for k in ("id", "name", "identity_class", "status", "version")}, "identity_spec", True

    def _presence(self):
        raw = self.load("operations.presence")
        if not raw:
            raise ValueError("presence unavailable")
        from core.operations.presence import PresenceStore

        view = PresenceStore(self.storage, self.identity_id).public_view()
        return (
            {
                "status": view.get("status"),
                "health": view.get("health"),
                "last_recorded_status": raw.get("status"),
                "last_heartbeat": raw.get("last_heartbeat"),
                "current_objective": view.get("current_objective"),
            },
            "operations.presence + liveness evaluation",
            True,
        )

    def _capabilities(self):
        self.load("capabilities")  # enforce read budgets before registry projection
        self.load("capability.permissions")
        data = CapabilityRegistry(self.storage).inspect_state(self.identity_id)
        observations = self.load("capability.observations") or {}
        commons = self.load("operations.cc_surface") or {}
        for name, skill in data['skills'].items():
            skill['readiness'] = ('PERMISSION_REQUIRED' if not skill['authority'] else
                'CONFIGURATION_REQUIRED' if skill['state']=='INSTALLED_MISCONFIGURED' else
                'PROVIDER_UNAVAILABLE' if skill['state']=='INSTALLED_PROVIDER_UNAVAILABLE' else
                'READY' if skill['executable'] else 'NOT_RECENTLY_VERIFIED')
            evidence = observations.get(name,{})
            if evidence.get('last_verified_at'):
                skill['last_verified_at'] = evidence['last_verified_at']
                skill['verified_effect'] = evidence.get('effect','call')
            elif name=='culture_commons.observe' and commons.get('last_observed_at'):
                skill['last_verified_at'] = commons['last_observed_at']
                skill['verified_effect'] = 'historical observation'
        if not data["storage_present"]:
            raise ValueError("installation namespace not initialized")
        return data, "capabilities + capability.permissions + registered descriptors", data["complete"]

    def _permissions(self):
        raw = self.load("capability.permissions") or {}
        grants = raw.get("grants", [])
        data = [{k: g.get(k) for k in ("capability", "permission", "granted_at")} for g in grants[:MAX_ROWS]]
        return (
            {"grants": data, "default": "deny nonpublic scopes without a matching grant"},
            "capability.permissions",
            len(grants) <= MAX_ROWS,
        )

    def _connection(self):
        path = database_path(self.storage)
        if not path or not path.is_file():
            raise ValueError("service registry unavailable")
        db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
        db.row_factory = sqlite3.Row
        return db

    def _rows(self, sql, params=()):
        db = self._connection()
        try:
            return [dict(r) for r in db.execute(sql, params)]
        finally:
            db.close()

    def _services(self):
        rows = self._rows("SELECT identity,document FROM services ORDER BY identity LIMIT ?", (MAX_ROWS + 1,))
        items = []
        for row in rows[:MAX_ROWS]:
            doc = json.loads(row["document"])
            items.append(
                {
                    k: doc.get(k)
                    for k in ("identity", "display_name", "services", "availability", "supported_artifacts", "version")
                }
            )
        for item in items:
            count = self._rows(
                "SELECT COUNT(*) AS count FROM jobs WHERE provider=? AND state='COMPLETED'", (item["identity"],)
            )[0]["count"]
            item["completed_jobs"] = count
        return items, "services.sqlite3/services + jobs (advertisements, not trust)", len(rows) <= MAX_ROWS

    def _jobs(self):
        rows = self._rows(
            "SELECT id,requester,provider,state,price,artifact,updated FROM jobs WHERE requester=? OR provider=? ORDER BY updated DESC LIMIT ?",
            (self.identity_id, self.identity_id, MAX_ROWS + 1),
        )
        for row in rows:
            evidence = self._rows(
                "SELECT seq FROM events WHERE job=? AND kind='acceptance_passed' LIMIT 1", (row["id"],)
            )
            row["acceptance_evidence"] = bool(evidence)
            row["verified_completed"] = row["state"] == "COMPLETED" and bool(evidence)
        counts = self._rows(
            "SELECT state,COUNT(*) AS count FROM jobs WHERE requester=? OR provider=? GROUP BY state",
            (self.identity_id, self.identity_id),
        )
        return (
            {"items": rows[:MAX_ROWS], "counts": {r["state"]: r["count"] for r in counts}},
            "services.sqlite3/jobs + acceptance events",
            len(rows) <= MAX_ROWS,
        )

    def _agreements(self):
        rows = self._rows("SELECT id,requester,provider,state,revision,job,authority,updated FROM negotiations WHERE requester=? OR provider=? ORDER BY updated DESC LIMIT ?", (self.identity_id,self.identity_id,MAX_ROWS+1))
        return rows[:MAX_ROWS], "services.sqlite3/negotiations (terms withheld)", len(rows)<=MAX_ROWS

    def _relationships(self):
        raw = self.load("operations.relationships") or {}
        items = raw.get("items", [])
        graph = self.load("relationships") or {}
        edges = graph.get("edges", [])
        return (
            {
                "operations": [
                    {
                        "id": r.get("id"),
                        "display_name": r.get("display_name"),
                        "role": r.get("role"),
                        "status": r.get("status"),
                    }
                    for r in items[:MAX_ROWS]
                ],
                "identity_graph": [
                    {"id": r.get("id"), "target_id": r.get("target_id"), "type": r.get("edge_type")}
                    for r in edges[:MAX_ROWS]
                ],
            },
            "operations.relationships + relationships",
            max(len(items), len(edges)) <= MAX_ROWS,
        )

    def _economy(self):
        db = self._connection()
        try:
            db.execute("BEGIN")
            balance = db.execute(
                "SELECT COALESCE(SUM(amount),0) FROM entries WHERE account=?", (self.identity_id,)
            ).fetchone()[0]
            account = db.execute("SELECT spending_limit FROM accounts WHERE identity=?", (self.identity_id,)).fetchone()
            providers = db.execute(
                "SELECT identity FROM services ORDER BY identity LIMIT ?", (MAX_ROWS + 1,)
            ).fetchall()
            favors = {
                r["identity"]: not bool(
                    db.execute(
                        "SELECT 1 FROM jobs WHERE requester=? AND provider=? AND state='COMPLETED' LIMIT 1",
                        (self.identity_id, r["identity"]),
                    ).fetchone()
                )
                for r in providers[:MAX_ROWS]
            }
            counts = {
                r[0]: r[1]
                for r in db.execute(
                    "SELECT state,COUNT(*) FROM jobs WHERE provider=? GROUP BY state", (self.identity_id,)
                )
            }
            acceptance = {
                r[0]: r[1]
                for r in db.execute(
                    "SELECT kind,COUNT(*) FROM events WHERE identity=? AND kind IN ('acceptance_passed','acceptance_failed','rework_requested') GROUP BY kind",
                    (self.identity_id,),
                )
            }
            return (
                {
                    "unit": "IDC",
                    "redeemable": False,
                    "account_exists": account is not None,
                    "balance": balance,
                    "spending_limit": account[0] if account else 0,
                    "first_favor_available": favors,
                    "reputation": {
                        "completed_jobs": counts.get("COMPLETED", 0),
                        "failed_jobs": counts.get("FAILED", 0),
                        **acceptance,
                    },
                },
                "services.sqlite3/accounts + entries + jobs + events",
                len(providers) <= MAX_ROWS,
            )
        finally:
            db.close()

    def _artifacts(self):
        rows = self._rows(
            "SELECT DISTINCT a.hash,j.id AS job,j.provider,j.requester,j.state FROM artifacts a JOIN jobs j ON j.artifact=a.hash WHERE j.requester=? OR j.provider=? LIMIT ?",
            (self.identity_id, self.identity_id, MAX_ROWS + 1),
        )
        return (
            {
                "items": rows[:MAX_ROWS],
                "scope": "governed service artifacts; does not inventory arbitrary files or drafts",
            },
            "services.sqlite3/artifacts + jobs",
            len(rows) <= MAX_ROWS,
        )

    def _recent_actions(self):
        rows = self._rows(
            "SELECT seq,job,kind,created FROM events WHERE identity=? AND kind IN ('job_completed','artifact_delivered','capability_installed','acceptance_passed','settled','message_delivered') ORDER BY seq DESC LIMIT ?",
            (self.identity_id, MAX_ROWS),
        )
        # Records are transport evidence only with sent status AND a transport id.
        # Control-chat replies are never confused with external email delivery.
        messages = (self.load("operations.messages") or {}).get("items", [])
        email = [
            {"message_id": m.get("id"), "effect": "email_submitted_to_transport", "at": m.get("sent_at")}
            for m in messages[-100:]
            if m.get("channel") == "email"
            and m.get("direction") == "outbound"
            and m.get("status") == "sent"
            and m.get("external_id")
        ]
        return (
            {
                "service_events": rows,
                "email_transport_submissions": email[-MAX_ROWS:],
                "scope": "bounded recent runtime records, not an exhaustive history or proof of final email delivery",
            },
            "services.sqlite3/events + operations.messages",
            False,
        )

    def _provenance(self):
        rows = self._rows(
            "SELECT seq,job,kind,created FROM events WHERE identity=? ORDER BY seq DESC LIMIT ?",
            (self.identity_id, MAX_ROWS + 1),
        )
        return rows[:MAX_ROWS], "services.sqlite3/events (raw payloads withheld)", len(rows) <= MAX_ROWS

    def _needs(self):
        raw = self.load("operations.needs") or {}
        items = raw.get("items", [])
        return (
            [{"id": r.get("id"), "category": r.get("category"), "status": r.get("status")} for r in items[:MAX_ROWS]],
            "operations.needs",
            len(items) <= MAX_ROWS,
        )


_OPERATIONAL = re.compile(
    r"\b(capabilit\w*|permission\w*|authori\w*|web|search|service\w*|job\w*|balance|credit\w*|artifact\w*|template\w*|objective\w*|identityos|engineer|installed|sent|email|notifi\w*|reputation|relationship\w*|your identity|status)\b",
    re.I,
)


# Grounding exists for claims about the identity's own state (its services,
# jobs, balance, permissions). A prompt that asks to fetch/summarize live
# external data ("the gmail inbox", "my emails from the past 3 days") must go
# through tools instead: the snapshot holds no inbox contents, and the
# citation schema suppresses tool calls, so the only possible outcome used to
# be the fallback card. Such prompts are data-fetch requests with a fetch verb
# plus a target noun, and must not engage the self-state contract.
_FETCH_VERB = re.compile(
    r"\b(list|summari[sz]e|show|read|fetch|check(ing)?|pull|view|count|describe|do|status)\b",
    re.I,
)
_FETCH_TARGET = re.compile(
    r"\b(inboxe?s?|mailbox(?:es)?|emails?|e-mails?|mail|messages?)\b", re.I,
)

# Question-topic -> self-state sections. Grounding must attach RELEVANT truth,
# never the whole audit: the capabilities inventory alone is ~16k chars, and a
# question about recent email was paying for the full registry (live finding).
_SECTION_HINTS = (
    (re.compile(r"\b(status|uptime|alive|online|health|heartbeat)\b", re.I), ("presence",)),
    (re.compile(r"\b(capabilit\w*|skill\w*|tool\w*)\b", re.I), ("capabilities",)),
    (re.compile(r"\b(permission\w*|authorit\w*|grant\w*|allow\w*|deny|denied|can you)\b", re.I), ("permissions",)),
    (re.compile(r"\b(service\w*|job\w*|agreement\w*|contract\w*|provider\w*|requester\w*)\b", re.I), ("services", "jobs", "agreements")),
    (re.compile(r"\b(balance|credit\w*|payment|spend\w*|budget)\b", re.I), ("economy",)),
    (re.compile(r"\b(relationship\w*|contact\w*|correspondence|partner\w*)\b", re.I), ("relationships",)),
    (re.compile(r"\b(send\w*|sent|respond\w*|replied|repl\w+|wrote|recent|lately|last|interactions?|email)\b", re.I), ("recent_actions",)),
    (re.compile(r"\b(need\w*|objective\w*|priority|priorities)\b", re.I), ("needs",)),
    (re.compile(r"\b(artifact\w*)\b", re.I), ("artifacts",)),
    (re.compile(r"\b(provenance|audit)\b", re.I), ("provenance",)),
)


def grounding_sections(text: str) -> tuple[str, ...]:
    """Return the snapshot sections relevant to *text*.

    The identity section always ships: every grounded answer identifies itself.
    A query matching nothing gets identity alone, not every section.
    """
    text = text or ""
    selected: list[str] = ["identity"]
    for hint, sections in _SECTION_HINTS:
        if hint.search(text):
            for section in sections:
                if section not in selected:
                    selected.append(section)
    return tuple(selected)


def needs_grounding(text):
    text = text or ""
    if re.search(r"\b(?:your|current|active|unresolved) (?:needs|work|runtime)\b", text, re.I):
        return True
    if not _OPERATIONAL.search(text):
        return False
    # Correspondence-content retrieval is a data request, not a self-state
    # claim. "What did he send, what did you respond in the last 3
    # interactions" is only answerable from evidence, and grounding would
    # force a self-citation contract that forbids tools and dumps the card.
    if re.search(
        r"\b(last|latest|recent|past|so far)\b.{0,60}\b(email\w*|message\w*|interaction\w*|correspondence|mail|thread\w*)\b"
        r"|\b(email\w*|message\w*|interaction\w*|correspondence|mail|thread\w*)\b.{0,60}\b(last|latest|recent|past)\b",
        text, re.I,
    ):
        return False
    if _FETCH_VERB.search(text) and _FETCH_TARGET.search(text):
        return False
    return True


def grounding_context(snapshot):
    from core.expression import context
    return context(snapshot)


def _pointer(snapshot, path):
    if not isinstance(path, str) or not path.startswith("/sections/") or "/data" not in path:
        raise ValueError("fact must reference section data")
    parts = path.split("/")[1:]
    section = snapshot["sections"].get(parts[1], {})
    if section.get("status") != "VERIFIED":
        raise ValueError("unverified source")
    current = snapshot
    for part in parts:
        part = part.replace("~1", "/").replace("~0", "~")
        current = current[int(part)] if isinstance(current, list) else current[part]
    return current


def check_claims(snapshot, claims, *, now=None):
    observed = datetime.fromisoformat(snapshot["observed_at"]).timestamp()
    if (time.time() if now is None else now) - observed > snapshot["valid_for_seconds"]:
        return ["stale_snapshot"]
    errors = []
    if not isinstance(claims, list) or not 1 <= len(claims) <= 30:
        return ["missing_or_unbounded_claims"]
    for i, claim in enumerate(claims):
        if not isinstance(claim, dict) or claim.get("kind") not in {"FACT", "INFERENCE", "PROPOSAL", "UNKNOWN"}:
            errors.append(f"claim_{i}:invalid_kind")
            continue
        if claim["kind"] != "FACT":
            continue
        try:
            actual = _pointer(snapshot, claim.get("path"))
            if json.dumps(actual, sort_keys=True) != json.dumps(claim.get("value"), sort_keys=True):
                errors.append(f"claim_{i}:contradicted")
        except (ValueError, KeyError, IndexError, TypeError):
            errors.append(f"claim_{i}:unsupported")
    return errors


def render_verified(snapshot):
    from core.expression import render
    return render(snapshot)


def guard_response(text, snapshot, *, current=None):
    from core.expression import validate_render
    return validate_render(text, snapshot, current)
