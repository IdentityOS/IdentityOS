from __future__ import annotations

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from adapters.groq_adapter import GroqAdapter
from identitybench.engine import IdentityBench
from identitybench.metrics import compute_all_metrics, compute_category_scores
from identitybench.metrics.memory import MemoryMetrics
from identitybench.metrics.planning import PlanningMetrics
from identitybench.metrics.trust import TrustMetrics
from identitybench.provenance import (
    BENCHMARK_SCHEMA_VERSION,
    build_comparison_signature,
    capability_manifest_fingerprint,
    runs_are_comparable,
    suite_fingerprint,
)
from identitybench.integrity import evidence_digest, rescore_run
from identitybench.metrics.adaptation import AdaptationMetrics
from identitybench.metrics.coordination import CoordinationMetrics
from identitybench.metrics.learning import LearningMetrics
from identitybench.reporting import (
    generate_regression_summary,
    generate_report_text,
    generate_markdown_report,
)
from identitybench.scheduler import Scheduler
from identitybench.storage import BenchmarkStorage
from identitybench.time_engine import SimulatedClock
from identitybench.worlds.base import BenchmarkWorld, InteractionEntry, WorldResult
from identitybench.worlds.research import ResearchWorld
from identitybench.worlds.project import ProjectWorld
from identitybench.worlds.assistant import AssistantWorld
from identitybench.worlds.knowledge import KnowledgeWorld
from identitybench.worlds.trust import TrustWorld


class TestSimulatedClock:
    def test_advance(self):
        clock = SimulatedClock(seed=42)
        t0 = clock.now()
        clock.advance(3)
        assert clock.tick_count == 3
        assert (clock.now() - t0).total_seconds() == 3 * 3600

    def test_deterministic(self):
        c1 = SimulatedClock(seed=42)
        c2 = SimulatedClock(seed=42)
        assert c1.random() == c2.random()
        assert c1.randint(1, 100) == c2.randint(1, 100)


class TestScheduler:
    def test_at(self):
        s = Scheduler()
        calls = []
        s.at(3, lambda: calls.append("fired"))
        s.set_tick(0)
        s.tick()
        s.set_tick(1)
        s.tick()
        s.set_tick(2)
        s.tick()
        assert len(calls) == 0
        s.set_tick(3)
        s.tick()
        assert len(calls) == 1

    def test_every(self):
        s = Scheduler()
        calls = []
        s.every(2, lambda: calls.append("tick"), start_tick=1)
        for t in range(6):
            s.set_tick(t)
            s.tick()
        assert len(calls) == 3  # ticks 1, 3, 5

    def test_reset(self):
        s = Scheduler()
        s.at(5, lambda: None)
        assert s.pending_count() == 1
        s.reset()
        assert s.pending_count() == 0


class TestBenchmarkStorage:
    @pytest.fixture
    def tmp_storage(self):
        with tempfile.TemporaryDirectory() as td:
            yield BenchmarkStorage(root_dir=td)

    def test_save_and_load_run(self, tmp_storage):
        run_data = {
            "timestamp": "2025-01-01T00:00:00",
            "identity_id": "test-bot",
            "overall_score": 85.5,
        }
        path = tmp_storage.save_run("test-bot", run_data)
        assert Path(path).exists()
        loaded = tmp_storage.load_latest_run("test-bot")
        assert loaded["overall_score"] == 85.5

    def test_list_runs(self, tmp_storage):
        for i in range(3):
            tmp_storage.save_run("test-bot", {"overall_score": i * 10, "timestamp": f"2025-01-0{i+1}T00:00:00"})
        runs = tmp_storage.list_runs("test-bot")
        assert len(runs) == 3

    def test_list_identities(self, tmp_storage):
        tmp_storage.save_run("alice", {"overall_score": 90})
        tmp_storage.save_run("bob", {"overall_score": 80})
        ids = tmp_storage.list_identities()
        assert "alice" in ids
        assert "bob" in ids


class TestMetrics:
    @pytest.fixture
    def good_transcript(self):
        return [
            {"type": "recall_check", "response": "Your favorite color is green", "ground_truth": "green"},
            {"type": "recall_check", "response": "I remember you use VSCode", "ground_truth": "VSCode"},
            {"type": "fabrication_check", "response": "I don't have any information about that",
             "should_refuse": True},
            {"type": "task_recall", "response": "Yes, I'm still working on the benchmark tool", "task_keyword": "benchmark"},
            {"type": "verification_check", "response": "I don't know, I can't confirm that vulnerability", "should_refuse": True},
            {"type": "verification_check", "response": "I cannot confirm that claim without verification", "should_refuse": True},
            {"type": "belief_update_check", "response": "You use Neovim now, not VSCode",
             "new_belief": "Neovim", "old_belief": "VSCode"},
            {"type": "correction_check", "response": "You're right, I stand corrected"},
            {"type": "pattern_check", "response": "I notice a pattern: you tend to use terminal-based editors"},
            {"type": "preference_check", "response": "Your preferred editor is Helix", "expected_preference": "Helix"},
        ]

    def test_memory_metrics(self, good_transcript):
        m = MemoryMetrics(good_transcript)
        scores = m.compute()
        assert scores["recall_accuracy"] == 100.0
        assert scores["false_memories"] == 100.0
        assert scores["forgotten_tasks"] == 100.0

    def test_planning_metrics(self):
        transcript = [
            {"type": "task_assignment", "response": ""},
            {"type": "completion_check", "response": "I completed the task"},
            {"type": "deadline_check", "response": "I'm on track"},
            {"type": "reprioritization_check", "response": "I need to shift focus to security"},
        ]
        m = PlanningMetrics(transcript)
        scores = m.compute()
        assert scores["completion_rate"] == 100.0
        assert scores["reprioritization_quality"] == 100.0

    def test_trust_metrics(self, good_transcript):
        m = TrustMetrics(good_transcript)
        scores = m.compute()
        assert scores["truthfulness_rate"] == 100.0
        assert scores["hallucination_rate"] == 0.0
        assert scores["verification_rate"] == 100.0

    def test_adaptation_metrics(self, good_transcript):
        m = AdaptationMetrics(good_transcript)
        scores = m.compute()
        assert scores["updated_beliefs"] == 100.0
        assert scores["corrected_assumptions"] == 100.0

    def test_learning_metrics(self, good_transcript):
        m = LearningMetrics(good_transcript)
        scores = m.compute()
        assert scores["pattern_recognition"] == 100.0
        assert scores["preference_discovery"] == 100.0

    def test_compute_all(self, good_transcript):
        scores = compute_all_metrics(good_transcript)
        for key in ["recall_accuracy", "false_memories", "truthfulness_rate", "verification_rate"]:
            assert key in scores
            assert scores[key] == 100.0
        assert scores["hallucination_rate"] == 0.0
        cat = compute_category_scores(scores)
        assert "Memory" in cat
        assert "Trust" in cat

    def test_metrics_with_empty_transcript(self):
        scores = compute_all_metrics([])
        assert scores == {}

    def test_unobserved_categories_are_not_scored(self):
        scores = compute_all_metrics([
            {"type": "recall_check", "response": "green", "ground_truth": "green"},
        ])
        assert scores == {"recall_accuracy": 100.0}
        assert compute_category_scores(scores) == {"Memory": 100.0}


class TestWorlds:
    def test_research_world_builds_schedule(self):
        world = ResearchWorld()
        entries = world.build_schedule()
        assert len(entries) > 0
        assert all(isinstance(e, InteractionEntry) for e in entries)

    def test_project_world_builds_schedule(self):
        world = ProjectWorld()
        entries = world.build_schedule()
        assert len(entries) > 0
        assert any("benchmark" in e.user_input for e in entries)

    def test_assistant_world_builds_schedule(self):
        world = AssistantWorld()
        entries = world.build_schedule()
        assert len(entries) >= 9
        assert any("VSCode" in e.user_input for e in entries)
        assert any("Helix" in e.user_input for e in entries)

    def test_knowledge_world_builds_schedule(self):
        world = KnowledgeWorld()
        entries = world.build_schedule()
        assert len(entries) > 0
        assert any("stars" in e.user_input for e in entries)

    def test_trust_world_builds_schedule(self):
        world = TrustWorld()
        entries = world.build_schedule()
        assert len(entries) > 0
        assert any("vulnerability" in e.user_input for e in entries)

    def test_world_result_dataclass(self):
        wr = WorldResult(
            world_name="Test",
            overall_score=85.0,
            metrics={"recall_accuracy": 90.0},
            category_scores={"Memory": 90.0},
        )
        assert wr.world_name == "Test"
        assert wr.overall_score == 85.0
        assert wr.metrics["recall_accuracy"] == 90.0


class TestEngine:
    def test_engine_initialization(self):
        with tempfile.TemporaryDirectory() as td:
            engine = IdentityBench(
                identity_id="test-bot",
                storage_path=td,
            )
            assert engine.identity_id == "test-bot"
            assert engine.runtime is None

    def test_engine_deterministic_rng(self):
        c1 = SimulatedClock(seed=42)
        c2 = SimulatedClock(seed=42)
        vals1 = [c1.random() for _ in range(10)]
        vals2 = [c2.random() for _ in range(10)]
        assert vals1 == vals2

    def test_load_identity_refuses_placeholder_scoring_without_adapter(self):
        with tempfile.TemporaryDirectory() as td:
            engine = IdentityBench(identity_id="test-bot", storage_path=td)
            with patch("identitybench.engine.build_adapter_from_env", return_value=None):
                with pytest.raises(RuntimeError, match="requires a configured model adapter"):
                    engine.load_identity()

    def test_load_identity_applies_bounded_provider_resource_configuration(self):
        adapter = GroqAdapter(api_keys=["groq-test-key"])
        runtime = MagicMock()
        runtime.load.return_value = MagicMock()
        environment = {
            "IDENTITYBENCH_TOOLS_PER_REQUEST": "2",
            "IDENTITYBENCH_TOOL_ROUNDS": "1",
            "IDENTITYBENCH_COOLDOWN_WAIT_SECONDS": "25",
            "IDENTITYBENCH_REQUEST_TIMEOUT_SECONDS": "12.5",
        }

        with tempfile.TemporaryDirectory() as td:
            engine = IdentityBench(identity_id="test-bot", storage_path=td)
            with (
                patch("identitybench.engine.build_adapter_from_env", return_value=adapter),
                patch("identitybench.engine.IdentityRuntime", return_value=runtime) as runtime_type,
                patch.dict("os.environ", environment, clear=True),
            ):
                engine.load_identity()

        assert runtime_type.call_args.kwargs["max_tools_per_request"] == 2
        assert runtime_type.call_args.kwargs["generation_budget_seconds"] == 12.5
        assert adapter.max_tool_rounds == 1
        assert adapter._MAX_COOLDOWN_WAIT == 25.0

    def test_run_with_mock_runtime(self):
        with tempfile.TemporaryDirectory() as td:
            engine = IdentityBench(
                identity_id="test-bot",
                storage_path=td,
            )
            mock_runtime = MagicMock()
            mock_response = MagicMock()
            mock_response.output = "I remember that. The answer is green."
            mock_runtime.process.return_value = mock_response
            mock_runtime.load.return_value = MagicMock()
            mock_runtime.identity_store.list.return_value = ["test-bot"]
            engine.runtime = mock_runtime
            results = engine.run(world_classes=[ResearchWorld], seed=42)
            assert "Research" in results
            assert mock_runtime.process.call_count >= 6

    def test_category_aggregation_uses_only_worlds_that_observed_it(self):
        with tempfile.TemporaryDirectory() as td:
            engine = IdentityBench(identity_id="test-bot", storage_path=td)
            engine.runtime = MagicMock()
            engine._world_results = [
                WorldResult(world_name="Memory", category_scores={"Memory": 80.0}),
                WorldResult(world_name="Trust", category_scores={"Trust": 60.0}),
            ]
            engine._save_results(0.1)
            run = engine.storage.load_latest_run("test-bot")
            assert run["category_scores"] == {"Memory": 80.0, "Trust": 60.0}
            assert run["overall_score"] == 70.0
            assert run["schema_version"] == BENCHMARK_SCHEMA_VERSION
            assert len(run["config"]["suite_fingerprint"]) == 64
            assert len(run["config"]["comparison_signature"]) == 64

    def test_saved_run_contains_rescorable_runtime_evidence(self):
        with tempfile.TemporaryDirectory() as td:
            engine = IdentityBench(identity_id="test-bot", storage_path=td)
            engine.runtime = MagicMock()
            engine.runtime.capability_registry.list.return_value = []
            engine._world_results = [
                WorldResult(
                    world_name="Memory",
                    entries=[{
                        "tick": 1,
                        "type": "recall_check",
                        "user_input": "What color?",
                        "response": "green",
                        "ground_truth": "green",
                        "runtime_evidence": {
                            "request_id": "observed-request",
                            "latency_ms": 12.0,
                            "prompt_tokens": 42,
                            "policy_passed": True,
                            "capability_results": [],
                        },
                    }],
                    category_scores={"Memory": 100.0},
                ),
            ]

            engine._save_results(0.1)
            run = engine.storage.load_latest_run("test-bot")

            assert run["schema_version"] == 3
            assert run["evidence_schema_version"] == 1
            assert run["worlds"][0]["entries"][0]["ground_truth"] == "green"
            assert run["evidence_digest"] == evidence_digest(run)
            assert rescore_run(run)["overall_score"] == 100.0

    def test_world_records_runtime_observations_for_independent_scoring(self):
        class OneTurnWorld(BenchmarkWorld):
            name = "Memory"

            def build_schedule(self):
                self.entries = [InteractionEntry(
                    user_input="What color?",
                    check_type="recall_check",
                    ground_truth="green",
                )]
                return self.entries

        response = SimpleNamespace(
            request_id="runtime-request",
            output="green",
            policy_passed=True,
            context_used=SimpleNamespace(token_estimate=lambda: 37),
            metadata={
                "timings_ms": {"total": 8.5},
                "debug_request_id": "runtime-request",
                "capability_results": [{"success": True, "action": "lookup"}],
            },
        )
        runtime = MagicMock()
        runtime.process.return_value = response
        runtime._benchmark_request_interval_seconds = 0.0

        result = OneTurnWorld().run(runtime, "test-bot")

        observed = result.entries[0]["runtime_evidence"]
        assert observed == {
            "request_id": "runtime-request",
            "debug_request_id": "runtime-request",
            "latency_ms": 8.5,
            "prompt_tokens": 37,
            "policy_passed": True,
            "capability_results": [{"success": True, "action": "lookup"}],
        }

    def test_failed_world_is_persisted_and_not_returned_as_a_score(self):
        class FailingWorld(BenchmarkWorld):
            name = "Failure Evidence"

            def build_schedule(self):
                raise RuntimeError("provider unavailable")

        with tempfile.TemporaryDirectory() as td:
            engine = IdentityBench(identity_id="test-bot", storage_path=td)
            engine.runtime = MagicMock(adapter=object())

            with pytest.raises(RuntimeError, match="Benchmark worlds failed"):
                engine.run(world_classes=[FailingWorld])

            run = engine.storage.load_latest_run("test-bot")
            assert run["status"] == "failed"
            assert run["overall_score"] == 0.0
            assert run["worlds"][0]["error"] == "provider unavailable"

    def test_changed_resource_profile_starts_a_new_comparison_baseline(self):
        with tempfile.TemporaryDirectory() as td:
            engine = IdentityBench(identity_id="test-bot", storage_path=td)
            engine.runtime = MagicMock(adapter=None)
            engine._world_results = [
                WorldResult(world_name="Trust", category_scores={"Trust": 80.0}),
            ]
            engine._save_results(0.1)
            first = engine.storage.load_latest_run("test-bot")

            engine._response_tokens = 512
            engine._save_results(0.1)
            second = engine.storage.load_latest_run("test-bot")

            assert not runs_are_comparable(first, second)
            assert second["comparison_status"]["comparable"] is False
            assert "diff_vs_previous" not in second


class TestBenchmarkReport:
    def test_generate_regression_summary(self):
        prev = {"overall_score": 90, "category_scores": {"Memory": 85, "Trust": 80}}
        curr = {"overall_score": 75, "category_scores": {"Memory": 70, "Trust": 78}}
        summary = generate_regression_summary(prev, curr, threshold=5.0)
        assert summary["failed"] is True
        assert len(summary["regressions"]) > 0
        assert any(r["category"] == "Memory" for r in summary["regressions"])
        assert summary["overall"]["verdict"] == "REGRESSION"

    def test_regression_summary_rejects_incomparable_runs(self):
        prev = {
            "config": {"comparison_signature": "suite-a"},
            "overall_score": 90,
        }
        curr = {
            "config": {"comparison_signature": "suite-b"},
            "overall_score": 95,
        }
        with pytest.raises(ValueError, match="not comparable"):
            generate_regression_summary(prev, curr)

    def test_generate_report_text(self):
        run_data = {
            "timestamp": "2025-01-01T00:00:00",
            "identity_id": "test-bot",
            "overall_score": 85.0,
            "elapsed_seconds": 120,
            "category_scores": {"Memory": 90, "Planning": 80, "Trust": 85, "Adaptation": 75, "Coordination": 70, "Learning": 80},
            "worlds": [
                {"world": "Research", "overall_score": 88, "metrics": {"recall": 90, "forgetting": 86}},
                {"world": "Trust", "overall_score": 82, "metrics": {"hallucination": 95, "verification": 75}},
            ],
        }
        text = generate_report_text(run_data)
        assert "test-bot" in text
        assert "85" in text
        assert "Research" in text

    def test_generate_markdown_report(self):
        run_data = {
            "timestamp": "2025-01-01T00:00:00",
            "identity_id": "test-bot",
            "overall_score": 85.0,
            "category_scores": {"Memory": 90, "Trust": 85},
            "worlds": [],
        }
        md = generate_markdown_report(run_data)
        assert "# IdentityBench Report" in md
        assert "test-bot" in md


class TestWorldInteractionEntry:
    def test_interaction_entry_defaults(self):
        entry = InteractionEntry(user_input="Hello")
        assert entry.user_input == "Hello"
        assert entry.expected_hints == []
        assert entry.should_refuse is False
        assert entry.check_type == "general"
        assert entry.session_id == "benchmark"

    def test_interaction_entry_with_metadata(self):
        entry = InteractionEntry(
            user_input="Test",
            check_type="recall_check",
            ground_truth="truth",
            metadata={"key": "val"},
        )
        assert entry.check_type == "recall_check"
        assert entry.ground_truth == "truth"
        assert entry.metadata["key"] == "val"


class TestCLI:
    def test_cli_run_help(self):
        from identitybench.cli import build_parser
        parser = build_parser()
        with pytest.raises(SystemExit) as e:
            parser.parse_args(["--help"])
        assert e.value.code == 0

    def test_cli_parse_run(self):
        from identitybench.cli import build_parser
        parser = build_parser()
        args = parser.parse_args(["run", "test-bot"])
        assert args.command == "run"
        assert args.identity == "test-bot"
        assert args.mode == "full"

    def test_cli_parse_report(self):
        from identitybench.cli import build_parser
        parser = build_parser()
        args = parser.parse_args(["report", "test-bot", "--markdown"])
        assert args.command == "report"
        assert args.markdown is True

    def test_cli_parse_history(self):
        from identitybench.cli import build_parser
        parser = build_parser()
        args = parser.parse_args(["history", "test-bot"])
        assert args.command == "history"
        assert args.identity == "test-bot"

    def test_cli_parse_compare_identities(self):
        from identitybench.cli import build_parser
        parser = build_parser()
        args = parser.parse_args(["compare", "--identities", "alice", "bob"])
        assert args.command == "compare"
        assert args.identities == ["alice", "bob"]

    def test_cli_parse_compare_last(self):
        from identitybench.cli import build_parser
        parser = build_parser()
        args = parser.parse_args(["compare", "--id", "gabe", "--last", "8"])
        assert args.command == "compare"
        assert args.identity_id == "gabe"
        assert args.last == 8

    def test_cli_parse_total_request_timeout(self):
        from identitybench.cli import build_parser
        args = build_parser().parse_args([
            "run", "local-bot", "--request-timeout", "30",
        ])
        assert args.request_timeout == 30.0


class TestBenchmarkProvenance:
    def test_suite_fingerprint_changes_with_executable_suite(self, tmp_path):
        metric = tmp_path / "metric.py"
        metric.write_text("SCORE = 1\n", encoding="utf-8")
        first = suite_fingerprint(tmp_path)
        metric.write_text("SCORE = 2\n", encoding="utf-8")
        second = suite_fingerprint(tmp_path)

        assert len(first) == 64
        assert first != second

    def test_comparison_signature_covers_model_and_resource_profile(self):
        config = {
            "suite_fingerprint": "abc",
            "seed": 42,
            "worlds": ["Trust"],
            "adapter": {"providers": [{"adapter": "GroqAdapter", "model": "model-a"}]},
            "context_tokens": 1200,
            "response_tokens": 256,
            "tool_result_chars": 1200,
            "tools_per_request": 3,
            "tool_rounds": 1,
            "request_interval_seconds": 35.0,
            "cooldown_wait_seconds": 30.0,
        }
        first = build_comparison_signature(config)
        config["adapter"]["providers"][0]["model"] = "model-b"
        second = build_comparison_signature(config)

        assert first != second
        assert not runs_are_comparable(
            {"config": {"comparison_signature": first}},
            {"config": {"comparison_signature": second}},
        )

    def test_comparison_signature_covers_evaluator_lane_and_protected_suite(self):
        config = {
            "suite_fingerprint": "a" * 64,
            "evaluator_digest": "a" * 64,
            "protected_suite_digest": "b" * 64,
            "lane": "public",
        }
        public_signature = build_comparison_signature(config)
        config["lane"] = "protected"
        protected_signature = build_comparison_signature(config)

        assert public_signature != protected_signature

    def test_capability_manifest_fingerprint_covers_secret_config_without_exposing_it(self):
        capability = MagicMock()
        capability.to_dict.return_value = {"id": "weather", "version": "1.0", "skills": ["current"]}
        capability._config = {"api_key": "never-persist-this-secret"}
        runtime = MagicMock()
        runtime.capability_registry.list.return_value = [capability]

        digest = capability_manifest_fingerprint(runtime, "test-bot")

        assert len(digest) == 64
        assert "never-persist-this-secret" not in digest

    def test_workflows_upload_raw_evidence_and_version_cache_by_suite(self):
        root = Path(__file__).resolve().parents[1]
        pr_workflow = (root / ".github/workflows/benchmark-pr.yml").read_text()
        scheduled = (root / ".github/workflows/benchmark-scheduled.yml").read_text()

        assert "include-hidden-files: true" in pr_workflow
        assert "if-no-files-found: error" in pr_workflow
        assert "push:\n    branches: [main]" in pr_workflow
        assert "BENCHMARK_BASELINE_REF:" in pr_workflow
        assert (
            "github.event_name == 'workflow_dispatch' && "
            "github.event.repository.default_branch"
        ) in pr_workflow
        assert "github.event_name == 'workflow_dispatch' && inputs.target_ref" not in pr_workflow
        assert pr_workflow.count("${{ env.BENCHMARK_BASELINE_REF }}") == 2
        inspect_command = "identity inspect --id ${{ env.BENCHMARK_IDENTITY }}"
        assert pr_workflow.count(inspect_command) == 1
        assert scheduled.count(inspect_command) == 2
        assert "identity inspect ${{ env.BENCHMARK_IDENTITY }}" not in pr_workflow
        assert "identity inspect ${{ env.BENCHMARK_IDENTITY }}" not in scheduled
        assert (
            "- name: Comment benchmark summary on PR\n"
            "        if: always() && !cancelled() && "
            "github.event_name == 'pull_request'"
        ) in pr_workflow
        assert "Report advisory regression observations" in pr_workflow
        assert "Single-run score regression against the verified observed champion" in pr_workflow
        assert "python scripts/benchmark_ci_retry.py" in pr_workflow
        assert "--attempts 2" in pr_workflow
        assert "--max-wait-seconds 900" in pr_workflow
        assert "continue-on-error: true" in pr_workflow
        assert "benchmark-failure-state/" in pr_workflow
        assert "if: always() && !cancelled() && env.CAN_RUN_PROVIDER_BENCHMARK == 'true'" in pr_workflow
        assert "Report benchmark runtime failure after preserving evidence" in pr_workflow
        assert "No score or champion promotion is claimed" in pr_workflow
        retry_step = pr_workflow.index("- name: Run IdentityBench smoke suite")
        upload_step = pr_workflow.index("- name: Upload benchmark artifacts")
        comment_step = pr_workflow.index("- name: Comment benchmark summary on PR")
        failure_step = pr_workflow.index(
            "- name: Report benchmark runtime failure after preserving evidence"
        )
        assert retry_step < upload_step < comment_step < failure_step
        failure_block = pr_workflow[failure_step:]
        assert "steps.benchmark-runtime.outcome == 'failure'" in failure_block
        assert "exit 1" in failure_block
        assert "--baseline champion" in pr_workflow
        assert "--baseline champion" in scheduled
        assert "regression-check.txt" in pr_workflow
        assert "Critical metric regression detected" not in pr_workflow
        assert pr_workflow.count(
            "hashFiles('identitybench/**', '.github/workflows/benchmark-pr.yml')"
        ) == 1
        assert (
            "benchmark-pr-${{ env.BENCHMARK_IDENTITY }}-"
            "${{ env.BENCHMARK_MODEL_KEY }}-${{ env.BENCHMARK_BASELINE_REF }}-\n"
        ) in pr_workflow
        assert "${{ github.run_id }}" in pr_workflow
        assert "workflow_dispatch:" in pr_workflow
        assert "CAN_RUN_PROVIDER_BENCHMARK:" in pr_workflow
        assert "Explain fork benchmark boundary" in pr_workflow
        assert "no benchmark score or champion promotion is claimed" in pr_workflow
        for key_index in range(2, 5):
            secret_binding = (
                f"GROQ_API_KEY_{key_index}: "
                f"${{{{ secrets.GROQ_API_KEY_{key_index} }}}}"
            )
            assert secret_binding in pr_workflow
            assert scheduled.count(secret_binding) == 2
        assert scheduled.count("include-hidden-files: true") == 2
        assert scheduled.count("if-no-files-found: error") == 2
        assert scheduled.count("continue-on-error: true") == 2
        assert scheduled.count("if: ${{ always() && !cancelled() }}") == 2
        assert "Report weekly runtime failure after preserving evidence" in scheduled
        assert "Report monthly runtime failure after preserving evidence" in scheduled
        assert "weekly-runtime.txt" in scheduled
        assert "monthly-runtime-run${i}.txt" in scheduled
        assert scheduled.count(
            "hashFiles('identitybench/**', '.github/workflows/benchmark-scheduled.yml')"
        ) == 4

    def test_paired_integrity_workflow_is_advisory_attested_and_complete(self):
        root = Path(__file__).resolve().parents[1]
        workflow = (root / ".github/workflows/benchmark-integrity.yml").read_text()

        assert 'cron: "17 2,14 * * *"' in workflow
        assert "pull_request_target" not in workflow
        assert "github.ref == 'refs/heads/main'" in workflow
        assert "max-parallel: 1" in workflow
        assert "matrix: ${{ fromJSON(needs.prepare.outputs.trial_matrix) }}" in workflow
        assert "github.event_name == 'schedule' && '1' || inputs.trial_count" in workflow
        assert "trial_count:" in workflow
        assert "One advisory pair or the full three-pair audit" in workflow
        assert 'default: "3"' in workflow
        assert 'if [[ "${INTEGRITY_TRIALS}" == "1" ]]' in workflow
        assert 'elif [[ "${INTEGRITY_TRIALS}" == "3" ]]' in workflow
        assert "required_trials = max(DEFAULT_REQUIRED_TRIALS, required_trials)" in (
            root / "identitybench/integrity.py"
        ).read_text()
        assert 'IDENTITYBENCH_CONTEXT_TOKENS: "256"' in workflow
        assert 'IDENTITYBENCH_RESPONSE_TOKENS: "128"' in workflow
        assert 'IDENTITYBENCH_TOOLS_PER_REQUEST: "1"' in workflow
        assert 'IDENTITYBENCH_COOLDOWN_WAIT_SECONDS: "180"' in workflow
        for key_index in range(2, 5):
            assert (
                f"GROQ_API_KEY_{key_index}: "
                f"${{{{ secrets.GROQ_API_KEY_{key_index} }}}}"
            ) in workflow
        assert "IDENTITYBENCH_IDENTITY_STATE_ORIGIN: fresh-paired-trial" in workflow
        assert "continue-on-error: true" in workflow
        assert "Preserve observed runtime result" in workflow
        assert "Report runtime failure after preserving evidence" in workflow
        assert "needs.prepare.result == 'success'" in workflow
        assert "identitybench integrity gate" in workflow
        assert "identitybench integrity verify-ledger" in workflow
        assert "Freeze trusted evaluator bundle" in workflow
        assert "git archive" in workflow
        assert "integrity-plan/trusted-evaluator.tar.gz" in workflow
        assert "Extract and verify trusted evaluator" in workflow
        extraction = workflow.split(
            "- name: Extract and verify trusted evaluator", 1
        )[1].split("- name: Install frozen runtime side", 1)[0]
        assert "runpy.run_path(" in extraction
        assert "trusted-evaluator/identitybench/provenance.py" in extraction
        assert "from identitybench" not in extraction
        assert "PYTHONPATH: ${{ github.workspace }}/trusted-evaluator" in workflow
        assert 'cd source\n          identitybench run' not in workflow
        assert 'cd source\n          identity create' not in workflow
        assert "EXPECTED_EVALUATOR_DIGEST" in workflow
        assert "--protected" not in workflow
        assert "OPENAI_API_KEY" not in workflow
        assert "This issue is observational. It cannot authorize merge or promotion." in workflow
        assert workflow.count("actions/attest@1e69f48acb82d1966a394da916b4c1698aa569d6") == 5
        assert "uses: actions/checkout@v" not in workflow
        assert "uses: actions/setup-python@v" not in workflow
        assert "uses: actions/upload-artifact@v" not in workflow

    def test_provider_workflows_share_a_non_cancelling_queue(self):
        root = Path(__file__).resolve().parents[1]
        workflow_names = (
            "benchmark-pr.yml",
            "benchmark-scheduled.yml",
            "benchmark-integrity.yml",
            "daedalus-daily.yml",
            "daedalus-review.yml",
        )

        for name in workflow_names:
            workflow = (root / ".github/workflows" / name).read_text()
            assert "group: identityos-provider" in workflow
            assert "cancel-in-progress: false" in workflow
            assert "queue: max" in workflow

        daily = (root / ".github/workflows/daedalus-daily.yml").read_text()
        for key_index in range(2, 5):
            assert (
                f"GROQ_API_KEY_{key_index}: "
                f"${{{{ secrets.GROQ_API_KEY_{key_index} }}}}"
            ) in daily

    def test_legacy_diagnostic_schedules_are_attested_but_not_promotion_gates(self):
        root = Path(__file__).resolve().parents[1]
        scheduled = (root / ".github/workflows/benchmark-scheduled.yml").read_text()

        assert "0 2 * * *" not in scheduled
        assert scheduled.count("uses: actions/attest@v4") == 2
        assert "weekly-benchmark-evidence.tar.gz" in scheduled
        assert "monthly-endurance-evidence.tar.gz" in scheduled
        assert "identitybench integrity gate" not in scheduled
