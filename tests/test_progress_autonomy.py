"""Persistent eligibility and honest progress: no remote calls or model fixtures."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core.operations.engine import TickReport
from core.operations.progress import Progress, fingerprint, meaningful, projection
from core.operations.surfaces import CultureCommonsSurface
from core.capabilities.result import CapabilityResult
from runtime.persistence import JSONFileBackend
from tests.test_aster_control import _engine, _serve_once

NOW = datetime(2026, 9, 24, 12, tzinfo=timezone.utc)


@pytest.mark.parametrize("classification", ["AUTHORITY_GAP", "CONFIGURATION_ERROR", "WAITING_FOR_EXECUTOR"])
def test_persistent_blocker_suppression_and_condition_wakeup(tmp_path, classification):
    storage = JSONFileBackend(tmp_path / "state")
    engine, _ = _engine(tmp_path, storage=storage, adapter=None)
    p = engine.progress
    p.begin(NOW)
    original = p.wait("skill:demo.read", classification, "v1", "Waiting", principal=classification == "AUTHORITY_GAP")
    for _ in range(4):
        assert not p.eligible("skill:demo.read", "v1")
    restored, _ = _engine(tmp_path, storage=JSONFileBackend(tmp_path / "state"), adapter=None)
    q = restored.progress
    q.begin(NOW)
    assert not q.eligible("skill:demo.read", "v1")
    assert q.eligible("skill:demo.read", "v2")
    updated = q.wait("skill:demo.read", classification, "v2", "Waiting")
    assert updated["fingerprint"] == original["fingerprint"]
    assert updated["first_seen"] == original["first_seen"]
    assert len([n for n in q.store.list_needs() if n.metadata.get("blocker")]) == 1
    q.resolve("skill:demo.read", "Verified executable")
    assert not projection(q.store)["waiting"]


def test_permission_configuration_and_catalog_wakeups(tmp_path):
    engine, _ = _engine(tmp_path, adapter=None)
    p = engine.progress
    authority = p.condition("authority")
    configuration = p.condition("configuration")
    engine.store._storage.save("aster", "capabilities", {"mcp": {"config": {"servers": ["local-fixture"]}}})
    assert p.condition("configuration") != configuration
    engine.store._storage.save("aster", "capability.permissions", {"grants": [{"permission": "network"}]})
    assert p.condition("authority") != authority
    # Real catalog store content, not a model assertion, changes eligibility.
    # File-backed registry is exercised in the closure service tests; here verify
    # the source digest consumes the actual catalog response.
    from unittest.mock import patch

    with patch("core.services.integration.existing_store") as source:
        source.return_value.rows.return_value = [{"identity": "engineer", "document": "develop-v1"}]
        first = p.condition("configuration")
        source.return_value.rows.return_value = [{"identity": "engineer", "document": "develop-v2"}]
        assert p.condition("configuration") != first


def test_project_no_change_and_real_change_outcomes(tmp_path):
    engine, _ = _engine(tmp_path, adapter=None)
    options = dict(detect_needs=False, discover=False, evaluate=False, act=False, monitor=False, follow_ups=False)
    first = engine.tick(now=NOW, **options).cycle_outcome
    second = engine.tick(now=NOW + timedelta(minutes=1), **options).cycle_outcome
    assert first["result"] == "PROGRESS"
    assert second["result"] == "NO_CHANGE"
    assert second["last_meaningful_progress"] == first["last_meaningful_progress"]
    from pathlib import Path

    (Path(engine.config.project_root) / "README.md").write_text(
        "# Changed project\nNew meaningful project information.\n"
    )
    third = engine.tick(now=NOW + timedelta(minutes=2), **options).cycle_outcome
    assert third["result"] == "PROGRESS"
    assert len(projection(engine.store)["cycles"]) == 3
    assert fingerprint(meaningful({"observed_at": "a", "facts": [1]})) == fingerprint(
        meaningful({"observed_at": "b", "facts": [1]})
    )


def _surface(engine, reads=0):
    cap = SimpleNamespace(read_budget_state=lambda now: {"reads": reads, "limit": 120})
    registry = Mock()
    registry.get.return_value = cap
    registry.call.return_value = CapabilityResult.from_data(
        "culture_commons", "culture_commons.observe", {"room": {}, "boards": {}, "observed_at": NOW.isoformat()}
    )
    engine._capability_registry = registry
    engine._cycle_now = NOW
    engine.progress.begin(NOW)
    return CultureCommonsSurface(engine), registry, cap


def test_daily_budget_suppression_and_reset_wakeup(tmp_path):
    engine, _ = _engine(tmp_path, adapter=None)
    surface, registry, cap = _surface(engine, 120)
    assert surface.observe()["reason"] == "daily_budget_exhausted"
    for _ in range(5):
        assert surface.observe()["suppressed"]
    registry.call.assert_not_called()
    engine._cycle_now = NOW + timedelta(days=1)
    engine.progress.begin(engine._cycle_now)
    cap.read_budget_state = lambda now: {"reads": 0, "limit": 120}
    assert surface.observe()["observed"]
    assert registry.call.call_count == 1


def test_unchanged_surface_backs_off_and_reserves_budget(tmp_path):
    engine, _ = _engine(tmp_path, adapter=None)
    surface, registry, cap = _surface(engine)
    assert surface.observe()["changed"]
    assert surface.observe()["suppressed"]
    engine._cycle_now = NOW + timedelta(seconds=300)
    engine.progress.begin(engine._cycle_now)
    assert not surface.observe()["changed"]
    b = engine.progress.blocker("surface:culture_commons").metadata["blocker"]
    assert b["next_eligible_retry"] == (NOW + timedelta(seconds=900)).timestamp()
    engine._cycle_now = NOW + timedelta(seconds=900)
    engine.progress.begin(engine._cycle_now)
    cap.read_budget_state = lambda now: {"reads": 96, "limit": 120}
    assert surface.observe()["reason"] == "budget_reserved"
    assert registry.call.call_count == 2


def test_activity_read_only_projection_preserves_provenance(tmp_path):
    import http.client, json

    engine, presence = _engine(tmp_path, adapter=None)
    p = engine.progress
    p.begin(NOW)
    p.wait("skill:web.search", "AUTHORITY_GAP", "v1", "Network authority", principal=True, reason="Permission changes")
    p.wait("surface:culture_commons", "DAILY_BUDGET_EXHAUSTED", "v1", "Daily reset", retry=NOW.timestamp() + 3600)
    p.finish(TickReport())
    before = len(engine.store.list_provenance())
    server, port = _serve_once(presence)
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port)
        conn.request("GET", "/api/activity")
        response = conn.getresponse()
        payload = json.loads(response.read())
        assert response.status == 200
        assert payload["autonomy"]["latest"]["result"] == "PRINCIPAL_REQUIRED"
        assert len(payload["autonomy"]["waiting"]) == 2
        assert payload["autonomy"]["latest"]["resources"]["model_calls"] == 0
        assert payload["events"]
        assert len(engine.store.list_provenance()) == before
    finally:
        server.shutdown()
        server.server_close()


def test_deferred_principal_not_reconsidered_until_runtime_changes(tmp_path):
    from core.operations.principal import submit_principal_message
    from core.operations.models import MessageStatus

    engine, _ = _engine(tmp_path, adapter=None)
    engine.progress.begin(NOW)
    message = submit_principal_message(engine.store, "Hello Aster")
    first = engine._phase_principal(NOW)
    assert first[0]["outcome"] == "deferred"
    for minute in range(1, 5):
        assert engine._phase_principal(NOW + timedelta(minutes=minute)) == []
    assert engine.store.get_message(message.id).status is MessageStatus.DEFERRED
    from tests.test_aster_control import _ReplyAdapter

    engine._adapter = _ReplyAdapter()
    assert engine._phase_principal(NOW + timedelta(minutes=5))[0]["outcome"] == "completed"
    assert engine.progress.blocker("principal:" + message.id).metadata["blocker"]["state"] == "RESOLVED"


def test_gap_detection_suppressed_across_ticks_then_permission_wakes(tmp_path):
    from core.operations.capability_gap import CapabilityGap

    engine, _ = _engine(tmp_path, adapter=None, required_skills=["web.search", "mcp.discover", "a2a.discover"])
    gaps = [
        CapabilityGap("web.search", status="installed_permission_missing"),
        CapabilityGap("mcp.discover", status="configuration_error"),
        CapabilityGap("a2a.discover", status="configuration_error"),
    ]
    engine.gap_detector.check = Mock(side_effect=lambda skills: [g for g in gaps if g.required_skill in skills])
    engine.gap_detector.resolve = Mock(side_effect=lambda gap: gap)
    for minute in range(4):
        engine.progress.begin(NOW + timedelta(minutes=minute))
        engine._phase_gaps(TickReport())
    assert engine.gap_detector.resolve.call_count == 3
    assert all(call.args[0] == [] for call in engine.gap_detector.check.call_args_list[1:])
    engine.store._storage.save(
        "aster", "capability.permissions", {"grants": [{"capability": "web", "permission": "network"}]}
    )
    engine._phase_gaps(TickReport())
    assert engine.gap_detector.resolve.call_count == 6


def test_waiting_blocker_does_not_become_outreach_need(tmp_path):
    engine, _ = _engine(tmp_path, adapter=None)
    engine.progress.begin(NOW)
    engine.progress.wait("surface:culture_commons", "DAILY_BUDGET_EXHAUSTED", "v1", "Waiting for reset")
    engine.discoverer.discover = Mock(return_value=[])
    assert engine._phase_discover(TickReport()) == []
    engine.discoverer.discover.assert_not_called()


def test_control_changes_from_separate_process_wake_operator(tmp_path):
    from core.operations import OperationsStore, ControlState

    storage = JSONFileBackend(tmp_path / "state")
    engine, _ = _engine(tmp_path, storage=storage, adapter=None)
    other = OperationsStore(JSONFileBackend(tmp_path / "state"), "aster")
    controls = other.controls()
    controls.paused = True
    other.set_controls(controls)
    assert engine.tick(now=NOW).skipped == [{"reason": "paused"}]
    controls.paused = False
    other.set_controls(controls)
    report = engine.tick(
        now=NOW, detect_needs=False, discover=False, evaluate=False, act=False, monitor=False, follow_ups=False
    )
    assert report.cycle_outcome


def test_cycle_includes_actual_service_events_and_policy_waits(tmp_path):
    from core.operations.models import ProvenanceEntry, ProvenancePhase

    engine, _ = _engine(tmp_path, adapter=None)
    p = engine.progress
    p.begin(NOW)
    entry = engine.store.append_provenance(
        ProvenanceEntry(phase=ProvenancePhase.CONTROL, action="service.requester", result="job_created")
    )
    outcome = p.finish(TickReport())
    assert outcome["result"] == "PROGRESS"
    assert outcome["delegations"] == [{"state": "job_created", "evidence": entry.id}]
    assert outcome["actions_attempted"][0]["evidence"] == entry.id
    p.begin(NOW + timedelta(minutes=1))
    outcome = p.finish(TickReport(escalations=["fixture draft awaiting authorization"]))
    assert outcome["result"] == "PRINCIPAL_REQUIRED"
    assert projection(engine.store)["waiting"][0]["principal_action_required"]
    assert outcome["last_meaningful_progress"]["at"] == NOW.isoformat()
