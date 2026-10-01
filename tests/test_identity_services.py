"""Isolated integration proof: these are NOT live Aster jobs."""

import json
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

from core.capabilities.registry import CapabilityRegistry
from core.identity import create_identity
from core.operations.capability_gap import CapabilityGapDetector
from core.services.runtime import ServiceRuntime, Session
from core.services.integration import database_path, work_cards
from runtime.persistence import JSONFileBackend


def world(tmp_path):
    storage = JSONFileBackend(str(tmp_path / "state"))
    storage.save(
        "test_requester",
        "identity_spec",
        create_identity(name="Test requester", identity_id="test_requester").to_dict(),
    )
    runtime = ServiceRuntime(storage, CapabilityRegistry(storage), database_path(storage))
    engineer = runtime.ensure_engineer()
    requester = runtime.bind("test_requester")
    return runtime, requester, engineer


def contract(key="one", skill="lines.canonicalize", steps=None):
    return {
        "service": "capability.develop",
        "skill": skill,
        "outcome": "Sort and deduplicate local lines",
        "constraints": {"effect": "local_transform", "steps": steps or ["split_lines", "sort", "unique"]},
        "acceptance": [{"input": "b\na\nb", "expected": ["a", "b"]}],
        "budget": 20,
        "request_key": key,
    }


def deliver(runtime, requester, engineer, c=None):
    job = runtime.request(requester, "engineer", c or contract())
    runtime.quote(engineer, job)
    runtime.accept_quote(requester, job)
    from core.executive.engine import ExecutiveRuntime

    executive = ExecutiveRuntime(runtime.storage, runtime.registry)
    runtime.enqueue(engineer, job, executive)
    executive.process_ready("engineer", max_steps=1)
    assert runtime.store.job(job)["state"] == "DELIVERED", executive.history("engineer")
    return job


def test_complete_lifecycle_second_job_and_fresh_process(tmp_path):
    runtime, requester, engineer = world(tmp_path)
    assert runtime.registry.can("test_requester", "lines.canonicalize")[0] is False
    assert runtime.discover("capability.develop")[0]["identity"] == "engineer"
    job = deliver(runtime, requester, engineer)
    assert runtime.store.job(job)["price"] == 0
    assert runtime.accept_delivery(requester, job)["state"] == "COMPLETED"
    assert runtime.store.reputation("engineer")["completed_jobs"] == 1
    assert runtime.store.reputation("engineer")["acceptance_passes"] == 1
    runtime.store.bootstrap("test_requester", 30, allocation_id="test-allocation")
    runtime.store.set_spending_limit("test_requester", 20, principal_reference="isolated-test-principal")
    second = deliver(runtime, requester, engineer, contract("two"))
    assert runtime.store.job(second)["price"] == 10
    assert runtime.accept_delivery(requester, second)["state"] == "COMPLETED"
    assert runtime.store.balance("test_requester") == 20
    assert runtime.store.balance("engineer") == 10
    with pytest.raises(ValueError):
        runtime.accept_delivery(requester, second)
    assert runtime.store.balance("engineer") == 10
    # New interpreter, new registry, same identities and persisted artifact.
    script = """
import json,sys
from runtime.persistence import JSONFileBackend
from core.capabilities.registry import CapabilityRegistry
from core.services.runtime import ServiceRuntime
from core.services.integration import database_path
from core.operations.store import OperationsStore
s=JSONFileBackend(sys.argv[1]); r=CapabilityRegistry(s); w=ServiceRuntime(s,r,database_path(s))
assert s.load('engineer','identity_spec')['id']=='engineer'
assert len(w.store.jobs('test_requester'))==2
assert w.store.reputation('engineer')['completed_jobs']==2
assert w.store.balance('engineer')==10
assert len(OperationsStore(s,'test_requester').list_relationships())==1
assert w.store.rows('SELECT id FROM messages')
result=r.call('test_requester','lines.canonicalize',value='b\\na\\nb')
assert result.success and result.data=={'value':['a','b']}
print('RESTART_PROOF_PASS')
"""
    result = subprocess.run([sys.executable, "-c", script, str(runtime.storage.root)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "RESTART_PROOF_PASS" in result.stdout
    events = runtime.store.rows("SELECT data FROM events WHERE kind='reuse_search'")
    assert json.loads(events[-1]["data"])["decision"] == "reuse"


def test_authority_gap_never_acquires_discovers_delegates_or_spends(tmp_path):
    runtime, requester, engineer = world(tmp_path)
    runtime.registry.install("test_requester", "web")
    calls = []
    detector = CapabilityGapDetector(
        capability_registry=runtime.registry,
        identity_id="test_requester",
        acquisition=lambda skill: calls.append("acquire"),
        delegation=lambda gap: calls.append("delegate"),
    )
    (gap,) = detector.check(["web.search"])
    assert gap.to_dict()["classification"] == "AUTHORITY_GAP"
    detector.resolve(gap)
    assert "PERMISSION_REQUIRED" in gap.resolution
    assert calls == []
    for service in ["capability.develop", "runtime.diagnose", "capability.test"]:
        c = contract(skill="web.search")
        c["service"] = service
        with pytest.raises(PermissionError, match="AUTHORITY_GAP"):
            runtime.request(requester, "engineer", c)
    assert runtime.store.jobs("test_requester") == []
    assert runtime.store.rows("SELECT * FROM transactions") == []
    assert runtime.store.rows("SELECT * FROM messages") == []
    assert runtime.registry.call("test_requester", "web.search", query="test").error["type"] == "permission_denied"


def test_sender_authenticity_and_message_restart(tmp_path):
    runtime, requester, engineer = world(tmp_path)
    with pytest.raises(PermissionError):
        runtime.send(Session("engineer"), "test_requester", "forged")
    requester.identity = "engineer"
    with pytest.raises(PermissionError):
        runtime.send(requester, "test_requester", "forged")
    requester = runtime.bind("test_requester")
    mid = runtime.send(requester, "engineer", "technical question")
    new = ServiceRuntime(runtime.storage, CapabilityRegistry(runtime.storage), database_path(runtime.storage))
    messages = new.receive(new.bind("engineer"))
    assert messages[0]["id"] == mid and messages[0]["status"] == "DELIVERED"
    new.send(new.bind("engineer"), "test_requester", "response", reply_to=mid)
    assert new.store.rows("SELECT status FROM messages WHERE id=?", (mid,))[0]["status"] == "RESPONDED"


def test_multiple_providers_and_dedupe(tmp_path):
    runtime, requester, engineer = world(tmp_path)
    runtime.storage.save("other", "identity_spec", create_identity(name="Other", identity_id="other").to_dict())
    runtime.advertise(runtime.bind("other"), ["capability.develop"], price=5)
    assert len(runtime.discover("capability.develop")) == 2

    def submit(_):
        return runtime.request(requester, "engineer", contract())

    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(submit, range(2)))
    assert ids[0] == ids[1]
    assert len(runtime.store.jobs("test_requester")) == 1
    changed = contract()
    changed["outcome"] = "changed"
    with pytest.raises(ValueError):
        runtime.request(requester, "engineer", changed)


def test_first_favor_failure_cancellation_and_reservation(tmp_path):
    runtime, requester, engineer = world(tmp_path)
    c = contract()
    c["acceptance"][0]["expected"] = "wrong"
    job = runtime.request(requester, "engineer", c)
    runtime.quote(engineer, job)
    runtime.accept_quote(requester, job)
    with pytest.raises(ValueError, match="TEST_FAILED"):
        runtime.work(engineer, job)
    assert runtime.store.job(job)["state"] == "FAILED"
    job2 = runtime.request(requester, "engineer", contract("two"))
    assert runtime.quote(engineer, job2)["price"] == 0
    runtime.accept_quote(requester, job2)
    job3 = runtime.request(requester, "engineer", contract("three"))
    runtime.quote(engineer, job3)
    with pytest.raises(ValueError, match="reserved"):
        runtime.accept_quote(requester, job3)
    runtime.cancel(requester, job2)
    runtime.accept_quote(requester, job3)
    runtime.work(engineer, job3)
    assert runtime.accept_delivery(requester, job3)["state"] == "COMPLETED"


def test_quote_and_acceptance_immutable_and_no_fake_completion(tmp_path):
    runtime, requester, engineer = world(tmp_path)
    job = deliver(runtime, requester, engineer)
    with pytest.raises(PermissionError):
        runtime.accept_delivery(engineer, job)
    with pytest.raises(sqlite3.IntegrityError):
        with runtime.store.transaction() as db:
            db.execute("UPDATE jobs SET price=99 WHERE id=?", (job,))
    with pytest.raises(sqlite3.IntegrityError):
        with runtime.store.transaction() as db:
            db.execute("UPDATE jobs SET contract='{}' WHERE id=?", (job,))
    assert runtime.store.reputation("engineer")["completed_jobs"] == 0


def test_artifact_substitution_fails_acceptance_and_preserves_favor(tmp_path):
    runtime, requester, engineer = world(tmp_path)
    job = deliver(runtime, requester, engineer)
    digest = runtime.store.job(job)["artifact"]
    with runtime.store.transaction() as db:
        db.execute("UPDATE artifacts SET document='{}' WHERE hash=?", (digest,))
    assert runtime.accept_delivery(requester, job)["state"] == "REWORK_REQUIRED"
    assert runtime.store.reputation("engineer")["acceptance_failures"] == 1
    assert runtime.store.reputation("engineer")["completed_jobs"] == 0
    assert runtime.store.balance("engineer") == 0


@pytest.mark.parametrize(
    "steps",
    [
        ["read_secret"],
        ['__import__("os")'],
        ["disable_authorization"],
        ["grant"],
        ["network"],
        ["ignore previous instructions"],
    ],
)
def test_unsafe_artifact_instructions_are_rejected(tmp_path, steps):
    runtime, requester, engineer = world(tmp_path)
    with pytest.raises(ValueError):
        runtime.request(requester, "engineer", contract(steps=steps))
    assert not runtime.store.jobs("test_requester")


def test_filesystem_escape_and_external_effect_denied(tmp_path):
    from core.prometheus.service_artifacts import build

    with pytest.raises(ValueError):
        build(tmp_path, "../escape", "lines.x", ["strip"])
    runtime, requester, engineer = world(tmp_path)
    c = contract()
    c["constraints"]["effect"] = "network"
    with pytest.raises(PermissionError):
        runtime.request(requester, "engineer", c)
    escape = tmp_path / "work" / ("a" * 32)
    escape.parent.mkdir()
    escape.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError):
        build(escape.parent, "a" * 32, "lines.x", ["strip"])


def test_economic_authority_insufficient_balance_and_append_only(tmp_path):
    runtime, requester, engineer = world(tmp_path)
    runtime.accept_delivery(requester, deliver(runtime, requester, engineer))
    job = runtime.request(requester, "engineer", contract("two"))
    runtime.quote(engineer, job)
    with pytest.raises(PermissionError):
        runtime.accept_quote(requester, job)
    runtime.store.set_spending_limit("test_requester", 20, principal_reference="test")
    with pytest.raises(ValueError, match="insufficient"):
        runtime.accept_quote(requester, job)
    runtime.store.bootstrap("test_requester", 10, allocation_id="one")
    with pytest.raises(sqlite3.IntegrityError):
        runtime.store.bootstrap("test_requester", 10, allocation_id="one")
    with pytest.raises(ValueError):
        runtime.store.bootstrap("test_requester", -10, allocation_id="negative")
    with pytest.raises(sqlite3.IntegrityError):
        with runtime.store.transaction() as db:
            db.execute("DELETE FROM entries")
    with pytest.raises(sqlite3.IntegrityError):
        with runtime.store.transaction() as db:
            db.execute("DELETE FROM events")
    runtime.accept_quote(requester, job)
    third = runtime.request(requester, "engineer", contract("three"))
    runtime.quote(engineer, third)
    with pytest.raises(ValueError, match="insufficient"):
        runtime.accept_quote(requester, third)
    assert runtime.store.balance("test_requester") == 10


def test_interrupted_local_execution_recovers(tmp_path):
    runtime, requester, engineer = world(tmp_path)
    job = runtime.request(requester, "engineer", contract())
    runtime.quote(engineer, job)
    runtime.accept_quote(requester, job)
    with runtime.store.transaction() as db:
        db.execute("UPDATE jobs SET state='IN_PROGRESS' WHERE id=?", (job,))
    assert runtime.recover(engineer) == 1
    assert runtime.store.job(job)["state"] == "ACCEPTED"
    runtime.work(engineer, job)
    assert runtime.accept_delivery(requester, job)["state"] == "COMPLETED"


def test_work_empty_and_no_relationship_before_interaction(tmp_path):
    runtime, requester, engineer = world(tmp_path)
    from core.operations.store import OperationsStore

    assert OperationsStore(runtime.storage, "test_requester").list_relationships() == []
    assert work_cards(runtime.storage, "test_requester") == []
    assert runtime.store.balance("engineer") == 0


def test_acceptance_failure_rework_and_rollback(tmp_path, monkeypatch):
    from core.capabilities.result import CapabilityResult

    runtime, requester, engineer = world(tmp_path)
    job = deliver(runtime, requester, engineer)
    original = runtime.registry.call

    def fail(identity, *args, **kwargs):
        if identity == "test_requester":
            return CapabilityResult.fail(
                "service_artifacts", "lines.canonicalize", "test_failure", "isolated requester error"
            )
        return original(identity, *args, **kwargs)

    monkeypatch.setattr(runtime.registry, "call", fail)
    assert runtime.accept_delivery(requester, job)["state"] == "REWORK_REQUIRED"
    assert runtime.registry.get("test_requester", "service_artifacts") is None
    monkeypatch.setattr(runtime.registry, "call", original)
    runtime.rework(requester, job)
    runtime.work(engineer, job)
    assert runtime.accept_delivery(requester, job)["state"] == "COMPLETED"
    assert runtime.store.reputation("engineer")["rework"] == 1


def test_changed_model_metadata_keeps_pending_identity_and_agreement(tmp_path):
    runtime, requester, engineer = world(tmp_path)
    job = runtime.request(requester, "engineer", contract())
    spec = runtime.storage.load("engineer", "identity_spec")
    spec["preferred_model"] = "isolated-provider-B"
    runtime.storage.save("engineer", "identity_spec", spec)
    new = ServiceRuntime(runtime.storage, CapabilityRegistry(runtime.storage), database_path(runtime.storage))
    new.ensure_engineer()
    assert new.storage.load("engineer", "identity_spec")["preferred_model"] == "isolated-provider-B"
    assert new.store.job(job)["provider"] == "engineer"
    assert new.quote(new.bind("engineer"), job)["price"] == 0


def test_activity_coalescing_preserves_raw_evidence(tmp_path):
    from core.operations.store import OperationsStore
    from core.operations.models import ProvenanceEntry, ProvenancePhase
    from runtime.health_server import _timeline

    runtime, requester, engineer = world(tmp_path)
    store = OperationsStore(runtime.storage, "test_requester")
    for i in range(8):
        store.append_provenance(
            ProvenanceEntry(
                phase=ProvenancePhase.CONTROL, summary="web.search permission missing", result="PERMISSION_REQUIRED"
            )
        )
    view = _timeline(store)
    assert len(view) == 1 and view[0]["count"] == 8
    assert len(store.list_provenance()) == 8


def test_automatic_gap_escalation_deduplicates_without_inventing_acceptance(tmp_path):
    runtime, requester, engineer = world(tmp_path)
    detector = CapabilityGapDetector(
        capability_registry=runtime.registry,
        identity_id="test_requester",
        acquisition=lambda skill: (False, "no provider"),
        delegation=lambda gap: runtime.escalate_gap(requester, gap),
    )
    for _ in range(2):
        (gap,) = detector.check(["unknown.operation"])
        detector.resolve(gap)
        assert not gap.resolved
    jobs = runtime.store.jobs("test_requester")
    assert jobs == []
    specs = runtime.negotiation.list(requester)
    assert len(specs) == 1 and specs[0]["state"] == "PROPOSED"
    assert specs[0]["contract"]["acceptance"] == []
    assert runtime.store.balance("test_requester") == 0


def test_worker_idle_and_requester_processing(tmp_path):
    from core.services.worker import tick, requester_tick

    runtime, requester, engineer = world(tmp_path)
    assert tick(runtime, "engineer") == "idle"
    job = runtime.request(requester, "engineer", contract())
    assert tick(runtime, "engineer") == "quoted"
    assert requester_tick(runtime, "test_requester") == "accepted"
    assert tick(runtime, "engineer") == "DELIVERED"
    assert requester_tick(runtime, "test_requester") == "COMPLETED"
    assert tick(runtime, "engineer") == "idle"
    assert runtime.store.job(job)["state"] == "COMPLETED"


def test_control_views_expose_verified_provenance_not_contract_contents(tmp_path):
    from runtime.health_server import _capability_cards
    from core.operations.store import OperationsStore

    runtime, requester, engineer = world(tmp_path)
    c = contract()
    c["outcome"] = "PRIVATE_OUTCOME_NOT_FOR_DASHBOARD"
    job = deliver(runtime, requester, engineer, c)
    runtime.accept_delivery(requester, job)
    cards = _capability_cards(runtime.registry, "test_requester", OperationsStore(runtime.storage, "test_requester"))
    skill = next(s for c in cards for s in c["skills"] if s["name"] == "lines.canonicalize")
    assert skill["provenance"]["accepted_by"] == "test_requester"
    assert skill["provenance"]["job"] == job
    assert "PRIVATE_OUTCOME" not in json.dumps(work_cards(runtime.storage, "test_requester"))
    assert "PRIVATE_OUTCOME" not in json.dumps(cards)


def test_paid_settlement_race_has_one_winner(tmp_path):
    runtime, requester, engineer = world(tmp_path)
    runtime.accept_delivery(requester, deliver(runtime, requester, engineer))
    runtime.store.bootstrap("test_requester", 20, allocation_id="race")
    runtime.store.set_spending_limit("test_requester", 20, principal_reference="test")
    job = deliver(runtime, requester, engineer, contract("two"))

    def accept(_):
        local = ServiceRuntime(runtime.storage, CapabilityRegistry(runtime.storage), database_path(runtime.storage))
        try:
            return local.accept_delivery(local.bind("test_requester"), job)["state"]
        except ValueError:
            return "replay_rejected"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(accept, range(2)))
    assert sorted(results) == ["COMPLETED", "replay_rejected"]
    assert runtime.store.balance("engineer") == 10
    assert len(runtime.store.rows("SELECT id FROM transactions WHERE job=?", (job,))) == 1


def test_malformed_contract_and_wrong_counterparty_rejected(tmp_path):
    runtime, requester, engineer = world(tmp_path)
    for mutate in [
        lambda c: c.update(sender="engineer"),
        lambda c: c.update(budget=-1),
        lambda c: c.update(budget=True),
    ]:
        c = contract()
        mutate(c)
        with pytest.raises(ValueError):
            runtime.request(requester, "engineer", c)
    job = runtime.request(requester, "engineer", contract())
    with pytest.raises(PermissionError):
        runtime.quote(requester, job)
    with pytest.raises(PermissionError):
        runtime.accept_quote(engineer, job)


def test_interrupted_acceptance_resumes_without_duplicate_settlement(tmp_path):
    runtime, requester, engineer = world(tmp_path)
    job = deliver(runtime, requester, engineer)
    with runtime.store.transaction() as db:
        db.execute("UPDATE jobs SET state='ACCEPTANCE_TESTING' WHERE id=?", (job,))
    runtime.recover(requester)
    assert runtime.store.job(job)["state"] == "DELIVERED"
    assert runtime.accept_delivery(requester, job)["state"] == "COMPLETED"
    assert runtime.store.reputation("engineer")["acceptance_passes"] == 1


def test_writer_process_exits_before_restart_proof(tmp_path):
    setup = """
import runpy,sys,json
from pathlib import Path
helpers=runpy.run_path('tests/test_identity_services.py')
r,q,e=helpers['world'](Path(sys.argv[1]))
j=helpers['deliver'](r,q,e)
assert r.accept_delivery(q,j)['state']=='COMPLETED'
print(j)
"""
    created = subprocess.run([sys.executable, "-c", setup, str(tmp_path)], capture_output=True, text=True, check=True)
    job_id = created.stdout.strip()
    verify = """
import sys
from runtime.persistence import JSONFileBackend
from core.capabilities.registry import CapabilityRegistry
from core.services.runtime import ServiceRuntime
from core.services.integration import database_path
from core.operations.store import OperationsStore
s=JSONFileBackend(sys.argv[1]); r=CapabilityRegistry(s); w=ServiceRuntime(s,r,database_path(s))
assert w.store.job(sys.argv[2])['state']=='COMPLETED'
assert w.store.reputation('engineer')['completed_jobs']==1
assert w.discover('capability.develop')[0]['identity']=='engineer'
assert w.store.rows('SELECT id FROM messages')
assert OperationsStore(s,'test_requester').list_relationships()
assert r.call('test_requester','lines.canonicalize',value='b\\na\\nb').data=={'value':['a','b']}
assert w.store.balance('engineer')==0
print('writer exited; restart passed')
"""
    restarted = subprocess.run(
        [sys.executable, "-c", verify, str(tmp_path / "state"), job_id], capture_output=True, text=True
    )
    assert restarted.returncode == 0, restarted.stderr


def test_explicit_credentials_rejected_before_persistence(tmp_path):
    runtime, requester, engineer = world(tmp_path)
    c = contract()
    c["outcome"] = "api_key=PRIVATE_SENTINEL"
    with pytest.raises(ValueError, match="credentials"):
        runtime.request(requester, "engineer", c)
    with pytest.raises(ValueError, match="credentials"):
        runtime.send(requester, "engineer", "password=PRIVATE_SENTINEL")
    assert not runtime.store.jobs("test_requester")
    assert "PRIVATE_SENTINEL" not in runtime.store.path and not runtime.store.rows("SELECT * FROM messages")
