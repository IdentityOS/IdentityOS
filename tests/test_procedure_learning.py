"""Procedure generalization, held-out promotion, and capability tests."""

from __future__ import annotations

import pytest

from core.capabilities.registry import CapabilityRegistry
from core.executive.engine import ExecutiveRuntime, register_executive
from core.executive.models import Evidence, Task, TaskStatus, TaskStep, TaskStepStatus
from core.procedures import HeldOutExample, ProcedureLearner, ProcedureLearningError, ProcedureStore
from runtime.persistence import JSONFileBackend


def completed_task(
    task_id: str,
    identity_id: str,
    *,
    path: str = "/workspace/training.txt",
    content: str = "alpha",
) -> Task:
    task = Task(task_id=task_id, goal="write and validate", identity_id=identity_id)
    task.status = TaskStatus.COMPLETED
    task.steps = [
        TaskStep(
            action="write_file",
            description="Write output",
            params={"path": path, "content": content},
            status=TaskStepStatus.COMPLETED,
            evidence=[Evidence("write_file", "written", "observed write", True)],
        ),
        TaskStep(
            action="validate_syntax",
            description="Validate output",
            params={"path": path},
            status=TaskStepStatus.COMPLETED,
            evidence=[Evidence("validate_syntax", "valid", "observed validation", True)],
        ),
    ]
    return task


def example(example_id: str, path: str, content: str) -> HeldOutExample:
    return HeldOutExample(
        example_id=example_id,
        bindings={"path": path, "content": content},
        expected_steps=(
            {"action": "write_file", "params": {"path": path, "content": content}},
            {"action": "validate_syntax", "params": {"path": path}},
        ),
    )


def test_completed_task_becomes_parameterized_promoted_procedure(tmp_path):
    storage = JSONFileBackend(root_dir=str(tmp_path / "store"))
    learner = ProcedureLearner(storage)
    learner.register_held_out_suite(
        "learner",
        "write-suite-v1",
        [
            example("case-one", "/workspace/one.py", "first"),
            example("case-two", "/workspace/two.py", "second"),
        ],
    )

    version = learner.learn(
        identity_id="learner",
        procedure_id="write_and_validate",
        source_task=completed_task("training-1", "learner"),
        parameter_bindings={"path": "/workspace/training.txt", "content": "alpha"},
        held_out_suite_id="write-suite-v1",
    )

    assert version.status == "promoted"
    assert version.held_out_score == 1.0
    rendered = ProcedureLearner(storage).compile(
        "learner",
        "write_and_validate",
        {"path": "/workspace/new.py", "content": "new value"},
    )
    assert rendered[0]["params"] == {
        "path": "/workspace/new.py", "content": "new value",
    }
    assert rendered[1]["params"] == {"path": "/workspace/new.py"}
    assert "first" not in str(version.evaluations)
    assert all("expected_sha256" in result for result in version.evaluations)


def test_candidate_can_improve_but_cannot_replace_champion_without_beating_it(tmp_path):
    storage = JSONFileBackend(root_dir=str(tmp_path / "store"))
    learner = ProcedureLearner(storage)
    learner.register_held_out_suite(
        "learner",
        "mixed-suite",
        [
            example("same-content", "/workspace/one.py", "alpha"),
            example("new-content", "/workspace/two.py", "beta"),
        ],
    )
    first = learner.learn(
        identity_id="learner",
        procedure_id="improving_writer",
        source_task=completed_task("training-one", "learner"),
        parameter_bindings={"path": "/workspace/training.txt"},
        held_out_suite_id="mixed-suite",
        minimum_score=0.5,
    )
    improved = learner.learn(
        identity_id="learner",
        procedure_id="improving_writer",
        source_task=completed_task(
            "training-two", "learner", path="/workspace/train-two.txt", content="beta",
        ),
        parameter_bindings={"path": "/workspace/train-two.txt", "content": "beta"},
        held_out_suite_id="mixed-suite",
        minimum_score=0.5,
    )
    tied = learner.learn(
        identity_id="learner",
        procedure_id="improving_writer",
        source_task=completed_task(
            "training-three", "learner", path="/workspace/train-three.txt", content="gamma",
        ),
        parameter_bindings={"path": "/workspace/train-three.txt", "content": "gamma"},
        held_out_suite_id="mixed-suite",
        minimum_score=0.5,
    )

    stored = ProcedureStore(storage).get("learner", "improving_writer")
    assert stored is not None
    stored_first = next(version for version in stored.versions if version.version == first.version)
    assert stored_first.status == "superseded" and stored_first.held_out_score == 0.5
    assert improved.status == "promoted" and improved.held_out_score == 1.0
    assert tied.status == "rejected"
    assert "did not beat champion" in tied.rejection_reason
    assert stored.champion_version == improved.version


def test_rejects_unverified_nondeterministic_or_secret_bearing_tasks(tmp_path):
    learner = ProcedureLearner(JSONFileBackend(root_dir=str(tmp_path / "store")))
    task = completed_task("bad", "learner")
    task.steps[0].evidence[0].success = False
    with pytest.raises(ProcedureLearningError, match="successful execution evidence"):
        learner.learn(
            identity_id="learner",
            procedure_id="bad_evidence",
            source_task=task,
            parameter_bindings={"path": "/workspace/training.txt"},
            held_out_suite_id="missing",
        )

    task = completed_task("side-effect", "learner")
    task.steps[0].action = "append_file"
    with pytest.raises(ProcedureLearningError, match="not deterministic"):
        learner.learn(
            identity_id="learner",
            procedure_id="unsafe_replay",
            source_task=task,
            parameter_bindings={"path": "/workspace/training.txt"},
            held_out_suite_id="missing",
        )

    task = completed_task("secret", "learner")
    task.steps[0].params["api_key"] = "do-not-store"
    with pytest.raises(ProcedureLearningError, match="sensitive"):
        learner.learn(
            identity_id="learner",
            procedure_id="secret_writer",
            source_task=task,
            parameter_bindings={"path": "/workspace/training.txt"},
            held_out_suite_id="missing",
        )


def test_held_out_suite_is_immutable_and_digest_checked(tmp_path):
    storage = JSONFileBackend(root_dir=str(tmp_path / "store"))
    learner = ProcedureLearner(storage)
    suite = [
        example("one", "/workspace/one", "one"),
        example("two", "/workspace/two", "two"),
    ]
    learner.register_held_out_suite("learner", "fixed", suite)
    with pytest.raises(ValueError, match="immutable"):
        learner.register_held_out_suite(
            "learner", "fixed", [suite[0], example("changed", "/x", "x")],
        )

    raw = storage.load("learner", ProcedureStore.SUITE_NAMESPACE)
    raw["suites"]["fixed"]["examples"][0]["bindings"]["content"] = "tampered"
    storage.save("learner", ProcedureStore.SUITE_NAMESPACE, raw)
    with pytest.raises(ValueError, match="content digest"):
        ProcedureStore(storage).load_suite("learner", "fixed")


def test_capability_learns_only_through_registered_executive_and_permission(tmp_path):
    storage = JSONFileBackend(root_dir=str(tmp_path / "store"))
    registry = CapabilityRegistry(storage)
    executive = ExecutiveRuntime(storage=storage, capability_registry=registry)
    register_executive(executive)
    task = completed_task("capability-task", "learner")
    executive.store.save(task)
    ProcedureLearner(storage).register_held_out_suite(
        "learner",
        "capability-suite",
        [
            example("one", "/workspace/one", "one"),
            example("two", "/workspace/two", "two"),
        ],
    )
    registry.install("learner", "procedure_learning")

    denied = registry.call(
        "learner",
        "procedure_learning.learn",
        task_id=task.task_id,
        procedure_id="learned_writer",
        bindings={"path": "/workspace/training.txt", "content": "alpha"},
        held_out_suite_id="capability-suite",
    )
    assert denied.success is False
    assert denied.error["type"] == "permission_denied"

    registry.grant("learner", "procedure_learning", "procedure:learn")
    learned = registry.call(
        "learner",
        "procedure_learning.learn",
        task_id=task.task_id,
        procedure_id="learned_writer",
        bindings={"path": "/workspace/training.txt", "content": "alpha"},
        held_out_suite_id="capability-suite",
    )
    listed = registry.call("learner", "procedure_learning.list")
    executive.shutdown()

    assert learned.success is True
    assert learned.data["status"] == "promoted"
    assert listed.data["procedures"][0]["held_out_score"] == 1.0
