"""Concurrency regressions for inbound email processing.

The hazard: the operator tick and the dedicated email ingest thread run in
one process against one engine. If they also share a claim owner id,
``claim_job`` treats the second claim as reentrant and grants it, so both
threads generate and send a reply to the same message. Two emails to one
human is a worse failure than none.

These tests drive real threads, because a sequential test cannot reproduce an
interleaving. The generation adapter sleeps so a second thread genuinely has
time to enter the pipeline.

Two independent defects are covered:

* a *shared* claim owner across threads, which makes ``claim_job`` grant a
  second claim and generate/send repeatedly;
* a stale job ledger in a long-lived process, which hides work another
  process (the Gmail push drain) already recorded.

Reverting the per-thread worker identity makes
``test_concurrent_ingest_sends_exactly_one_reply`` fail with four
generations and four sends for a single inbound message.
"""

from __future__ import annotations

import os
import threading
import time

import pytest

from core.capabilities.email.backends import FileMailboxBackend, MailboxTransport
from core.operations import (
    ControlState,
    OperationsEngine,
    OperatorConfig,
    PresenceStore,
    RequirementRule,
)
from core.operations.email_jobs import (
    EmailJobStatus,
    _owner_alive,
    _owner_pid,
    claim_job,
    ensure_job,
)
from core.operations.models import EmailJob
from core.operations.monitor import message_answered
from core.operations.store import OperationsStore
from runtime.persistence import InMemoryBackend


# ── helpers ───────────────────────────────────────────────────────────────


def _project(tmp_path):
    root = tmp_path / "project"
    root.mkdir(exist_ok=True)
    (root / "README.md").write_text(
        "# Sample Project\n\nA small experiment in distributed identity systems.\n",
        encoding="utf-8",
    )
    (root / "pyproject.toml").write_text(
        '[project]\nname = "sample"\nversion = "0.1.0"\n', encoding="utf-8"
    )
    return root


class _SlowResponder:
    """Deterministic model stand-in, slow enough for a real interleave."""

    model = "responder-slow"

    def __init__(self, text="Subject: Re: hello\nThanks for writing back."):
        self.text = text
        self.calls = 0
        self._lock = threading.Lock()

    def generate(self, context, user_input, identity, **kwargs):
        with self._lock:
            self.calls += 1
        time.sleep(0.05)
        return self.text


def _engine(tmp_path, storage=None, *, adapter=None, transport=None):
    storage = storage or InMemoryBackend()
    backend = None
    if transport is None:
        backend = FileMailboxBackend(tmp_path / "aster", mailbox="aster")
        transport = MailboxTransport(backend)
    config = OperatorConfig(
        identity_id="aster",
        project_root=str(_project(tmp_path)),
        project_name="IdentityOS",
        sender_name="Aster",
        sender_email="aster@identityos.local",
        signature="Aster",
        transparency="I am an AI operator.",
        purpose="IdentityOS outreach",
        need_rules=[RequirementRule(
            category="funding", description="Secure funding",
            probe=r"\b(fund|funding|grant|invest|sponsor)\b", expect="absent",
            urgency=0.8, impact=0.9)],
        candidate_sources=[],
        required_skills=[],
        pursue_threshold=0.45,
        hold_threshold=0.3,
    )
    engine = OperationsEngine(
        storage, config, transport=transport, adapter=adapter,
        presence=PresenceStore(storage, "aster"),
    )
    engine.store.set_controls(ControlState(outbound_mode="autonomous"))
    return engine, backend


def _item(external="ext-race"):
    return {
        "external_id": external,
        "from": "friend@example.org",
        "subject": "hello",
        "body": "Hello Aster. We fund open source identity work. Interested?",
        "thread_id": "thread-race",
    }


def _concurrently(fn, count, timeout=30):
    """Run ``fn`` in ``count`` threads released simultaneously."""
    barrier = threading.Barrier(count, timeout=10)
    results: list = []
    errors: list[BaseException] = []

    def run():
        try:
            barrier.wait()
            results.append(fn())
        except BaseException as exc:  # noqa: BLE001 - surfaced by the assert
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=timeout)
    alive = [t for t in threads if t.is_alive()]
    assert not alive, f"{len(alive)} ingest threads did not finish (deadlock?)"
    assert not errors, f"ingest raised: {errors!r}"
    return results


# ── owner id parsing ───────────────────────────────────────────────────────


def test_owner_pid_parses_two_and_three_part_ids():
    assert _owner_pid("abc123:4242") == 4242
    assert _owner_pid("abc123:4242:140737488355328") == 4242


@pytest.mark.parametrize("value", ["", "nocolon", "run:notapid", "run:"])
def test_owner_pid_tolerates_junk(value):
    assert _owner_pid(value) is None


def test_owner_alive_detects_this_process():
    assert _owner_alive(f"run:{os.getpid()}:1") is True
    assert _owner_alive(f"run:{os.getpid()}") is True


def test_worker_ids_are_distinct_per_thread_and_share_the_run():
    engine, _ = _engine(__import__("pathlib").Path(os.environ.get("TMPDIR", "/tmp")) /
                        f"worker-{os.getpid()}")
    ids: set[str] = set()
    lock = threading.Lock()

    def capture():
        value = engine._worker_id()
        with lock:
            ids.add(value)

    threads = [threading.Thread(target=capture, name=f"w{i}") for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert len(ids) == 4, f"worker ids collided: {ids}"
    for value in ids:
        assert value.startswith(engine._run_id + ":"), value
        assert _owner_pid(value) == os.getpid(), value


# ── the double-claim invariant ─────────────────────────────────────────────


def test_second_claim_by_a_different_live_worker_is_refused():
    storage = InMemoryBackend()
    store = OperationsStore(storage, "aster")
    job = ensure_job(store, inbound_message_id="<m1@x>")

    assert claim_job(store, job, f"run:{os.getpid()}:1")[0] is True
    granted, reason = claim_job(store, job, f"run:{os.getpid()}:2")
    assert granted is False, "a second worker must not claim a live job"
    assert reason == "claimed-by-live-owner"


def test_same_worker_may_reclaim_its_own_job():
    """Reentrancy is still required for the retry path within one worker."""
    storage = InMemoryBackend()
    store = OperationsStore(storage, "aster")
    job = ensure_job(store, inbound_message_id="<m2@x>")
    owner = f"run:{os.getpid()}:1"

    assert claim_job(store, job, owner)[0] is True
    assert claim_job(store, job, owner)[0] is True


def test_dead_owner_claim_is_reclaimable():
    """Crash recovery must still work: a dead pid is not a live owner."""
    dead_pid = int(open("/proc/sys/kernel/pid_max").read()) + 1
    assert not os.path.exists(f"/proc/{dead_pid}"), "chosen pid unexpectedly exists"

    storage = InMemoryBackend()
    store = OperationsStore(storage, "aster")
    job = ensure_job(store, inbound_message_id="<m3@x>")
    assert claim_job(store, job, f"run:{dead_pid}:1")[0] is True

    granted, _ = claim_job(store, job, f"run:{os.getpid()}:1")
    assert granted is True, "a job held by a dead pid must be reclaimable"


# ── concurrent ingest ──────────────────────────────────────────────────────


def test_concurrent_ingest_sends_exactly_one_reply(tmp_path):
    """Four threads ingest the same message at once: one reply, one job."""
    adapter = _SlowResponder()
    engine, backend = _engine(tmp_path, adapter=adapter)
    item = _item("ext-one")

    _concurrently(lambda: engine.ingest_messages([dict(item)]), 4)

    assert adapter.calls == 1, f"model called {adapter.calls} times for one message"
    outbox = backend.outbox()
    assert len(outbox) == 1, f"{len(outbox)} messages sent for one inbound"

    job = engine.store.find_email_job_by_inbound("ext-one")
    assert job is not None
    assert job.status is EmailJobStatus.COMPLETED, job.status.value
    assert job.attempt_count == 1, f"{job.attempt_count} claims for one message"
    assert message_answered(engine.store, engine.store.find_message_by_external_id("ext-one"))


def test_concurrent_ingest_records_exactly_one_job_and_message(tmp_path):
    adapter = _SlowResponder()
    engine, _ = _engine(tmp_path, adapter=adapter)
    item = _item("ext-dup")

    _concurrently(lambda: engine.ingest_messages([dict(item)]), 4)

    jobs = [j for j in engine.store.list_email_jobs()
            if j.inbound_message_id == "ext-dup"]
    assert len(jobs) == 1, f"{len(jobs)} jobs created for one message"
    inbound = [m for m in engine.store.list_messages() if m.external_id == "ext-dup"]
    assert len(inbound) == 1, f"{len(inbound)} inbound records for one message"


def test_poll_and_tick_racing_produce_one_reply(tmp_path):
    """The real production shape: the tick and the email thread, together."""
    adapter = _SlowResponder()
    engine, backend = _engine(tmp_path, adapter=adapter)
    item = _item("ext-poll")

    _concurrently(lambda: engine.poll_email(), 1)
    _concurrently(
        lambda: (engine.ingest_messages([dict(item)]), engine.tick()), 1
    )

    assert adapter.calls <= 1, f"model called {adapter.calls} times for one message"
    assert len(backend.outbox()) <= 1, "more than one reply left the mailbox"


def test_ingest_lock_is_reentrant():
    """The lock must be an RLock: the tick re-enters ingest internally."""
    engine, _ = _engine(__import__("pathlib").Path(os.environ.get("TMPDIR", "/tmp")) /
                        f"lock-{os.getpid()}")
    with engine._ingest_lock:
        with engine._ingest_lock:
            pass


def test_cross_process_job_visibility(tmp_path):
    """A job written by another process must be visible after a refresh.

    With event-driven ingest the push drain and the operator hold separate
    store instances over the same backend. Without an explicit refresh the
    operator's cached ledger never shows the drain's work, and the message
    looks permanently unanswered.
    """
    storage = InMemoryBackend()
    writer = OperationsStore(storage, "aster")
    reader = OperationsStore(storage, "aster")

    job = ensure_job(writer, inbound_message_id="<cross@x>")
    assert reader.find_email_job_by_inbound("<cross@x>") is None

    reader.refresh_email_jobs()
    found = reader.find_email_job_by_inbound("<cross@x>")
    assert found is not None, "stale cache hid another process's job"
    assert found.id == job.id


def test_refresh_email_jobs_preserves_terminal_state(tmp_path):
    storage = InMemoryBackend()
    engine, _ = _engine(tmp_path, storage=storage, adapter=_SlowResponder())
    other = OperationsStore(storage, "aster")
    job = ensure_job(other, inbound_message_id="<term@x>")
    claim_job(other, job, "run:1:1")
    other.get_email_job(job.id).status = EmailJobStatus.COMPLETED
    other.save_email_job(other.get_email_job(job.id))

    engine.store.refresh_email_jobs()
    fresh = engine.store.get_email_job(job.id)
    assert fresh is not None
    assert fresh.status is EmailJobStatus.COMPLETED
