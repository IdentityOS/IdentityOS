"""Behavioral tests for deterministic, evidence-backed procedure reflexes."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.capabilities.registry import CapabilityRegistry
from core.executive.engine import ExecutiveRuntime, register_executive
from core.executive.models import TaskStatus
from core.identity import create_identity
from core.procedures import HeldOutExample, ProcedureLearner
from core.reflexes import ReflexEngine, ReflexError
from runtime.orchestrator import IdentityRuntime, InteractionRequest
from runtime.persistence import JSONFileBackend


def _run_to_terminal(executive: ExecutiveRuntime, identity_id: str, task_id: str):
    for _ in range(20):
        executive.process_ready(identity_id, max_steps=10)
        task = executive.get_task(identity_id, task_id)
        if task.status in {TaskStatus.COMPLETED, TaskStatus.FAILED}:
            return task
    return executive.get_task(identity_id, task_id)


def _learn_writer(storage, registry, executive, identity_id, workspace):
    registry.install(
        identity_id, "file_tools", config={"allowed_roots": [str(workspace)]},
    )
    registry.install(identity_id, "skill_validator")
    registry.grant(identity_id, "file_tools", "filesystem:write")
    training_path = workspace / "training.py"
    source = executive.start_task(
        "write and validate Python",
        identity_id,
        steps=[
            {
                "action": "write_file",
                "description": "Write Python",
                "params": {"path": str(training_path), "content": "answer = 1"},
            },
            {
                "action": "validate_syntax",
                "description": "Validate Python",
                "params": {"path": str(training_path)},
            },
        ],
    )
    source = _run_to_terminal(executive, identity_id, source.task_id)
    assert source.status == TaskStatus.COMPLETED
    assert source.steps[0].evidence[0].data["skill"] == "file_tools.write_file"

    learner = ProcedureLearner(storage)
    cases = []
    for number in (2, 3):
        path = str(workspace / f"held-out-{number}.py")
        content = f"answer = {number}"
        cases.append(HeldOutExample(
            f"case-{number}",
            {"path": path, "content": content},
            (
                {"action": "write_file", "params": {"path": path, "content": content}},
                {"action": "validate_syntax", "params": {"path": path}},
            ),
        ))
    learner.register_held_out_suite(identity_id, "writer-suite", cases)
    version = learner.learn(
        identity_id=identity_id,
        procedure_id="write_and_validate",
        source_task=source,
        parameter_bindings={
            "path": str(training_path),
            "content": "answer = 1",
        },
        held_out_suite_id="writer-suite",
    )
    assert version.status == "promoted" and version.held_out_score == 1.0
    return version


def _setup(tmp_path, identity_id="reflex-tester"):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    storage = JSONFileBackend(root_dir=str(tmp_path / "store"))
    registry = CapabilityRegistry(storage)
    executive = ExecutiveRuntime(storage, capability_registry=registry)
    register_executive(executive)
    _learn_writer(storage, registry, executive, identity_id, workspace)
    engine = ReflexEngine(storage, executive, registry)
    binding = engine.register(
        identity_id,
        "write_python",
        "write_and_validate",
        "write {content} to {path}",
    )
    return storage, registry, executive, engine, binding, workspace


def test_reflex_dispatch_executes_real_steps_and_reconciles_evidence(tmp_path):
    storage, registry, executive, engine, binding, workspace = _setup(tmp_path)
    target = workspace / "result.py"

    dispatched = engine.dispatch(
        binding.identity_id,
        f"write value = 42 to {target}",
        autostart=False,
    )
    assert dispatched["model_planning_calls"] == 0
    task = _run_to_terminal(executive, binding.identity_id, dispatched["task_id"])
    assert task.status == TaskStatus.COMPLETED
    assert target.read_text() == "value = 42"
    assert all(step.evidence and all(item.success for item in step.evidence) for step in task.steps)

    verified = engine.reconcile(binding.identity_id, dispatched["run_id"])
    assert verified["status"] == "verified"
    assert verified["correct"] is True
    assert verified["expected_plan_sha256"] == verified["observed_plan_sha256"]

    executive.shutdown()


def test_reflex_preflight_enforces_current_installation_and_permissions(tmp_path):
    _, registry, executive, engine, binding, workspace = _setup(tmp_path)
    registry.revoke(binding.identity_id, "file_tools", "filesystem:write")

    with pytest.raises(ReflexError, match="preflight denied file_tools.write_file"):
        engine.dispatch(
            binding.identity_id,
            f"write value = 7 to {workspace / 'denied.py'}",
            autostart=False,
        )

    direct = executive.start_task(
        "unauthorized write",
        binding.identity_id,
        steps=[{
            "action": "write_file",
            "description": "Write",
            "params": {"path": str(workspace / "also-denied.py"), "content": "x = 1"},
        }],
    )
    executive.process_ready(binding.identity_id)
    direct = executive.get_task(binding.identity_id, direct.task_id)
    assert direct.status.value == "blocked"
    assert direct.steps[0].result["block_type"] == "authorization_required"
    assert not (workspace / "also-denied.py").exists()

    executive.shutdown()


def test_reflex_fails_closed_when_pinned_champion_changes(tmp_path):
    storage, _, executive, engine, binding, workspace = _setup(tmp_path)
    raw = storage.load(binding.identity_id, "procedure.library")
    procedure = raw["procedures"][binding.procedure_id]
    replacement = dict(procedure["versions"][0])
    replacement["version"] = 2
    replacement["status"] = "promoted"
    procedure["versions"][0]["status"] = "superseded"
    procedure["versions"].append(replacement)
    procedure["champion_version"] = 2
    storage.save(binding.identity_id, "procedure.library", raw)

    assert engine.prepare(
        binding.identity_id,
        f"write value = 9 to {workspace / 'stale.py'}",
    ) is None
    listed = engine.list(binding.identity_id)[0]
    assert listed["ready"] is False
    assert "version changed" in listed["reason"]

    executive.shutdown()


def test_reflex_persists_and_reuses_after_process_restart(tmp_path):
    storage, _, executive, _, binding, workspace = _setup(tmp_path)
    executive.shutdown()

    restarted_storage = JSONFileBackend(root_dir=str(tmp_path / "store"))
    restarted_registry = CapabilityRegistry(restarted_storage)
    restarted_executive = ExecutiveRuntime(
        restarted_storage, capability_registry=restarted_registry,
    )
    register_executive(restarted_executive)
    restarted = ReflexEngine(
        restarted_storage, restarted_executive, restarted_registry,
    )
    target = workspace / "after-restart.py"
    dispatched = restarted.dispatch(
        binding.identity_id,
        f"write restarted = True to {target}",
        autostart=False,
    )
    final = _run_to_terminal(
        restarted_executive, binding.identity_id, dispatched["task_id"],
    )

    assert final.status == TaskStatus.COMPLETED
    assert target.read_text() == "restarted = True"
    assert restarted.reconcile(binding.identity_id, dispatched["run_id"])["correct"] is True
    restarted_executive.shutdown()


def test_runtime_exact_match_skips_adapter_but_nonmatch_uses_it(tmp_path):
    class CountingAdapter:
        model = "counting-adapter"

        def __init__(self):
            self.calls = 0

        def generate(self, context, user_input, identity, **kwargs):
            self.calls += 1
            return "model response"

    adapter = CountingAdapter()
    storage = JSONFileBackend(root_dir=str(tmp_path / "store"))
    runtime = IdentityRuntime(storage=storage, adapter=adapter)
    identity = create_identity("Reflex Bot", identity_id="reflex-bot")
    runtime.register(identity)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _learn_writer(
        storage, runtime.capability_registry, runtime.executive,
        identity.id, workspace,
    )
    runtime.capability_registry.install(identity.id, "reflex")
    runtime.capability_registry.grant(identity.id, "reflex", "task:execute")
    runtime.reflex_engine.register(
        identity.id,
        "runtime_writer",
        "write_and_validate",
        "write {content} to {path}",
    )
    target = workspace / "runtime.py"

    response = runtime.process(InteractionRequest(
        identity_id=identity.id,
        user_input=f"write runtime_value = 5 to {target}",
    ))
    assert adapter.calls == 0
    assert response.metadata["reflex"]["model_planning_calls"] == 0
    assert "No model planning call was made" in response.output
    assert any(
        memory.memory_type.value == "episodic"
        for memory in runtime.memory_store.by_identity(identity.id)
    )

    fallback = runtime.process(InteractionRequest(
        identity_id=identity.id,
        user_input="summarize something unrelated",
    ))
    assert adapter.calls == 1
    assert fallback.output == "model response"
    assert fallback.metadata["reflex"] is None
    runtime.shutdown()


@pytest.mark.parametrize("reflex_seconds,planner_seconds,improved", [
    (0.001, 0.010, True),
    (0.020, 0.010, False),
])
def test_benchmark_measures_both_planners_and_checks_plan_equivalence(
    tmp_path, monkeypatch, reflex_seconds, planner_seconds, improved,
):
    _, _, executive, engine, binding, workspace = _setup(tmp_path)
    utterance = f"write measured = True to {workspace / 'measured.py'}"
    expected = engine.prepare(binding.identity_id, utterance)["steps"]

    # Exercise both real planning paths while controlling the benchmark clock.
    # Host load must not turn this reporting test into a performance gate.
    clock = [0.0]
    monkeypatch.setattr(
        "core.reflexes.engine.time", SimpleNamespace(monotonic=lambda: clock[0]),
    )
    real_prepare = engine.prepare
    prepare_calls = []
    planner_calls = []

    def measured_prepare(identity_id, request):
        prepare_calls.append(request)
        result = real_prepare(identity_id, request)
        clock[0] += reflex_seconds
        return result

    monkeypatch.setattr(engine, "prepare", measured_prepare)

    def planner(request):
        planner_calls.append(request)
        clock[0] += planner_seconds
        return expected

    report = engine.benchmark_planning(
        binding.identity_id,
        binding.reflex_id,
        utterance,
        planner,
        iterations=3,
    )
    assert report["correctness_preserved"] is True
    assert prepare_calls == [utterance] * 3
    assert planner_calls == [utterance] * 3
    assert report["latency_improved"] is improved
    assert report["reflex_median_ms"] == pytest.approx(reflex_seconds * 1000)
    assert report["planner_median_ms"] == pytest.approx(planner_seconds * 1000)

    executive.shutdown()


def test_trigger_contract_rejects_ambiguous_complex_or_sensitive_fields(tmp_path):
    _, _, executive, engine, binding, _ = _setup(tmp_path)
    champion = engine.procedures.get(binding.identity_id, binding.procedure_id).champion

    with pytest.raises(ReflexError, match="adjacent"):
        engine._compile_trigger("write {content}{path}", champion.parameter_schema)
    complex_schema = {
        "type": "object",
        "properties": {"items": {"type": "array"}},
        "required": ["items"],
        "additionalProperties": False,
    }
    with pytest.raises(ReflexError, match="must be a scalar"):
        engine._compile_trigger("process {items}", complex_schema)
    sensitive_schema = {
        "type": "object",
        "properties": {"api_token": {"type": "string"}},
        "required": ["api_token"],
        "additionalProperties": False,
    }
    with pytest.raises(ReflexError, match="Sensitive|sensitive"):
        engine._compile_trigger("use {api_token}", sensitive_schema)

    executive.shutdown()
