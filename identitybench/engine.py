from __future__ import annotations

import os
import sys
import time as real_time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Type

from runtime.orchestrator import IdentityRuntime
from runtime.persistence import JSONFileBackend
from adapters import build_adapter_from_env, describe_adapter
from adapters.chain import ChainAdapter
from adapters.groq_adapter import GroqAdapter

from identitybench.storage import BenchmarkStorage
from identitybench.worlds.base import BenchmarkWorld, WorldResult
from identitybench.worlds.research import ResearchWorld
from identitybench.worlds.project import ProjectWorld
from identitybench.worlds.assistant import AssistantWorld
from identitybench.worlds.knowledge import KnowledgeWorld
from identitybench.worlds.multi_agent import MultiAgentWorld
from identitybench.worlds.trust import TrustWorld
from identitybench.worlds.evolution import EvolutionWorld
from identitybench.metrics import (
    compute_all_metrics,
    compute_category_scores,
    compute_category_explanations,
)
from identitybench.journal.capability_journal import CapabilityJournal
from identitybench.journal.evolution_history import EvolutionHistory
from identitybench.analytics.diff import compute_benchmark_diff
from identitybench.analytics.regression import detect_regressions
from identitybench.analytics.recommendations import generate_recommendations
from identitybench.analytics.roi import calculate_capability_roi
from identitybench.analytics.root_cause import analyze_root_causes
from identitybench.analytics.timeline import build_evolution_timeline
from identitybench.provenance import (
    BENCHMARK_SCHEMA_VERSION,
    build_comparison_signature,
    capability_manifest_fingerprint,
    runs_are_comparable,
    suite_fingerprint,
)
from identitybench.integrity import evidence_digest
from identitybench.champion import assess_observed_champion, rescored_run


DEFAULT_WORLDS: List[Type[BenchmarkWorld]] = [
    ResearchWorld,
    ProjectWorld,
    AssistantWorld,
    KnowledgeWorld,
    TrustWorld,
    MultiAgentWorld,
    EvolutionWorld,
]

SMOKE_WORLDS: List[Type[BenchmarkWorld]] = [
    EvolutionWorld,
    TrustWorld,
    ResearchWorld,
]


class IdentityBench:
    def __init__(
        self,
        identity_id: str,
        storage: Optional[BenchmarkStorage] = None,
        runtime: Optional[IdentityRuntime] = None,
        storage_path: str = ".identitybench",
        request_timeout_seconds: Optional[float] = None,
    ):
        self.identity_id = identity_id
        self.storage = storage or BenchmarkStorage(storage_path)
        self.runtime = runtime
        self._world_results: List[WorldResult] = []
        self.capability_journal = CapabilityJournal(storage_path)
        self.evolution_history = EvolutionHistory(storage_path)
        self._seed = 42
        self._request_interval_seconds = 0.0
        self._context_tokens = 1200
        self._response_tokens = 256
        self._tool_result_chars = 1200
        self._tools_per_request = 3
        self._tool_rounds = 1
        self._request_timeout_seconds = (
            max(0.1, float(request_timeout_seconds))
            if request_timeout_seconds is not None else None
        )
        self._cooldown_wait_seconds = 30.0

    def load_identity(self, identity_id: Optional[str] = None) -> None:
        target = identity_id or self.identity_id
        if not self.runtime:
            storage_backend = JSONFileBackend(root_dir=".identity_store")
            adapter = build_adapter_from_env()
            if adapter is None:
                raise RuntimeError(
                    "IdentityBench requires a configured model adapter; refusing to score "
                    "the runtime's no-adapter placeholder as model behavior."
                )
            self._context_tokens = int(os.environ.get("IDENTITYBENCH_CONTEXT_TOKENS", "1200"))
            self._response_tokens = int(os.environ.get("IDENTITYBENCH_RESPONSE_TOKENS", "256"))
            self._tool_result_chars = int(
                os.environ.get("IDENTITYBENCH_TOOL_RESULT_CHARS", "1200")
            )
            self._tools_per_request = max(
                1, int(os.environ.get("IDENTITYBENCH_TOOLS_PER_REQUEST", "3"))
            )
            self._tool_rounds = max(
                1, int(os.environ.get("IDENTITYBENCH_TOOL_ROUNDS", "1"))
            )
            self._cooldown_wait_seconds = max(
                0.0, float(os.environ.get("IDENTITYBENCH_COOLDOWN_WAIT_SECONDS", "30"))
            )
            configured_timeout = os.environ.get("IDENTITYBENCH_REQUEST_TIMEOUT_SECONDS")
            if configured_timeout:
                self._request_timeout_seconds = max(0.1, float(configured_timeout))
            self.runtime = IdentityRuntime(
                storage=storage_backend,
                adapter=adapter,
                max_context_tokens=self._context_tokens,
                max_tool_result_chars=self._tool_result_chars,
                max_tools_per_request=self._tools_per_request,
                generation_budget_seconds=self._request_timeout_seconds,
            )
            leaves = adapter.adapters if isinstance(adapter, ChainAdapter) else (adapter,)
            for leaf in leaves:
                if hasattr(leaf, "max_tokens"):
                    leaf.max_tokens = min(int(leaf.max_tokens), self._response_tokens)
                if hasattr(leaf, "max_tool_rounds"):
                    leaf.max_tool_rounds = self._tool_rounds
                if isinstance(leaf, GroqAdapter):
                    leaf._MAX_COOLDOWN_WAIT = self._cooldown_wait_seconds
            configured_interval = os.environ.get("IDENTITYBENCH_REQUEST_INTERVAL_SECONDS")
            if configured_interval is not None:
                self._request_interval_seconds = max(0.0, float(configured_interval))
            elif any(isinstance(leaf, GroqAdapter) for leaf in leaves):
                # A tool-using interaction can make multiple provider requests.
                # Leave enough room for the complete loop under Groq's smallest
                # observed token-per-minute quota.
                self._request_interval_seconds = 35.0
            self.runtime._benchmark_request_interval_seconds = self._request_interval_seconds
            if self._request_interval_seconds > 0:
                # Reserve a clean provider window before the first interaction,
                # including when another CI job used the shared quota recently.
                self.runtime._benchmark_last_request_monotonic = real_time.monotonic()
            self.runtime.load_persisted()
        spec = self.runtime.load(target)
        if not spec:
            raise ValueError(
                f"Identity '{target}' not found. "
                f"Available: {[s.id for s in self.runtime.identity_store.list_all()]} or run 'identity list'."
            )

    def run(
        self,
        world_classes: Optional[List[Type[BenchmarkWorld]]] = None,
        speed: float = 1.0,
        seed: int = 42,
    ) -> Dict[str, WorldResult]:
        if world_classes is None:
            world_classes = DEFAULT_WORLDS
        if not self.runtime:
            self.load_identity()
        if getattr(self.runtime, "adapter", None) is None:
            raise RuntimeError("IdentityBench cannot run without a model adapter.")
        self._seed = seed

        for cls in world_classes:
            if cls is MultiAgentWorld and self.runtime:
                secondary_id = f"{self.identity_id}-writer"
                if not self.runtime.load(secondary_id):
                    from core.identity import create_identity
                    spec = create_identity(name="Writer", identity_id=secondary_id, persona="technical writer")
                    self.runtime.register(spec)

        results: Dict[str, WorldResult] = {}
        total = len(world_classes)
        start_wall = real_time.time()

        for idx, cls in enumerate(world_classes, 1):
            print(f"  [{idx}/{total}] {cls.name}...", end=" ", flush=True)
            try:
                world = cls(seed=seed)
                result = world.run(self.runtime, self.identity_id, speed=speed)
                results[cls.name] = result
                score = result.overall_score
                print(f"overall={score}")
            except Exception as e:
                print(f"FAILED ({e})")
                import traceback
                traceback.print_exc()
                results[cls.name] = WorldResult(
                    world_name=cls.name,
                    overall_score=0.0,
                    raw_data={"error": str(e)},
                )

        elapsed = real_time.time() - start_wall
        self._world_results = list(results.values())
        self._save_results(elapsed)
        failures = [result for result in results.values() if result.raw_data.get("error")]
        if failures:
            names = ", ".join(result.world_name for result in failures)
            raise RuntimeError(f"Benchmark worlds failed: {names}")
        return results

    def _save_results(self, elapsed_seconds: float) -> None:
        all_metrics: Dict[str, float] = {}
        all_categories: Dict[str, float] = {}
        category_counts: Dict[str, int] = {}
        all_explanations: Dict[str, Dict[str, list]] = {}
        world_data = []
        for wr in self._world_results:
            metrics = wr.metrics
            cats = wr.category_scores
            world_data.append({
                "world": wr.world_name,
                "description": wr.world_description,
                "overall_score": wr.overall_score,
                "metrics": metrics,
                "category_scores": cats,
                "error": wr.raw_data.get("error"),
                "entries": [dict(entry) for entry in wr.entries],
            })
            all_metrics.update(metrics)
            for cat, score in cats.items():
                all_categories[cat] = all_categories.get(cat, 0) + score
                category_counts[cat] = category_counts.get(cat, 0) + 1
            
            try:
                explanations = compute_category_explanations(
                    [{"type": e["type"], "response": e.get("response", "")}
                     for e in wr.entries],
                    wr.world_name,
                )
                for cat, exp in explanations.items():
                    if cat not in all_explanations:
                        all_explanations[cat] = {"reasons": [], "confidence": 1.0, "evidence_count": 0}
                    all_explanations[cat]["reasons"].extend(exp.get("reasons", []))
                    all_explanations[cat]["evidence_count"] += exp.get("evidence_count", 0)
                    all_explanations[cat]["confidence"] = min(
                        all_explanations[cat]["confidence"],
                        exp.get("confidence", 0.5),
                    )
            except Exception:
                pass

        for cat in all_categories:
            all_categories[cat] = round(all_categories[cat] / category_counts[cat], 1)
        overall = round(sum(all_categories.values()) / len(all_categories), 1) if all_categories else 0.0
        
        # Load history before saving this run.  The latest run is useful for
        # short-term diagnostics; the verified champion is the stable baseline.
        prior_runs = self.storage.load_all_runs(self.identity_id)
        capability_history = self.capability_journal.list_capabilities(self.identity_id)
        cap_entries = []
        for cap_id in capability_history:
            cap_entries.extend(self.capability_journal.get_journal(self.identity_id, cap_id))

        current_suite_fingerprint = suite_fingerprint()
        trial_index_raw = os.environ.get("IDENTITYBENCH_TRIAL_INDEX", "")
        try:
            trial_index = int(trial_index_raw) if trial_index_raw else None
        except ValueError:
            trial_index = None
        run_config = {
            "seed": self._seed,
            "worlds": [wr.world_name for wr in self._world_results],
            "adapter": describe_adapter(getattr(self.runtime, "adapter", None)),
            "request_interval_seconds": self._request_interval_seconds,
            "context_tokens": self._context_tokens,
            "response_tokens": self._response_tokens,
            "tool_result_chars": self._tool_result_chars,
            "tools_per_request": self._tools_per_request,
            "tool_rounds": self._tool_rounds,
            "cooldown_wait_seconds": self._cooldown_wait_seconds,
            "request_timeout_seconds": self._request_timeout_seconds,
            "suite_fingerprint": current_suite_fingerprint,
            "evaluator_digest": os.environ.get(
                "IDENTITYBENCH_EVALUATOR_DIGEST", current_suite_fingerprint
            ),
            "protected_suite_digest": os.environ.get(
                "IDENTITYBENCH_PROTECTED_SUITE_DIGEST", ""
            ),
            "lane": os.environ.get("IDENTITYBENCH_LANE", "public"),
            "commit_sha": os.environ.get(
                "IDENTITYBENCH_COMMIT_SHA", os.environ.get("GITHUB_SHA", "")
            ),
            "base_commit_sha": os.environ.get("IDENTITYBENCH_BASE_SHA", ""),
            "candidate_commit_sha": os.environ.get("IDENTITYBENCH_CANDIDATE_SHA", ""),
            "trial_index": trial_index,
            "seed_commitment": os.environ.get("IDENTITYBENCH_SEED_COMMITMENT", ""),
            "identity_state_origin": os.environ.get(
                "IDENTITYBENCH_IDENTITY_STATE_ORIGIN", "local"
            ),
            "capability_manifest_digest": capability_manifest_fingerprint(
                self.runtime, self.identity_id
            ),
        }
        run_config["comparison_signature"] = build_comparison_signature(run_config)
        run_data: Dict[str, Any] = {
            "schema_version": BENCHMARK_SCHEMA_VERSION,
            "evidence_schema_version": 1,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "identity_id": self.identity_id,
            "elapsed_seconds": round(elapsed_seconds, 1),
            "overall_score": overall,
            "category_scores": all_categories,
            "explanations": all_explanations,
            "worlds": world_data,
            "config": run_config,
            "status": "failed" if any(wr.raw_data.get("error") for wr in self._world_results) else "completed",
        }
        run_data["evidence_digest"] = evidence_digest(run_data)
        champion_assessment, champion = assess_observed_champion(run_data, prior_runs)
        run_data["champion_baseline"] = champion_assessment

        comparable_prior_runs = [
            prior for prior in prior_runs
            if prior.get("status") == "completed" and runs_are_comparable(prior, run_data)
        ]
        prev_run = comparable_prior_runs[0] if comparable_prior_runs else None

        # Analytics
        if prev_run and runs_are_comparable(prev_run, run_data):
            diff = compute_benchmark_diff(prev_run, run_data)
            run_data["diff_vs_previous"] = diff
            if champion is not None:
                run_data["diff_vs_champion"] = compute_benchmark_diff(
                    rescored_run(champion), run_data
                )
            signature = run_config["comparison_signature"]
            trends = [
                trend for trend in self.storage.load_trends(self.identity_id)
                if trend.get("comparison_signature") == signature
            ]
            regressions = detect_regressions(trends) if trends else []
            run_data["regressions"] = regressions
            baseline_run = rescored_run(champion) if champion is not None else prev_run
            baseline_diff = run_data.get("diff_vs_champion", diff)
            root_causes = analyze_root_causes(
                baseline_diff, baseline_run, run_data, cap_entries
            )
            run_data["root_causes"] = root_causes
            run_data["recommendations"] = generate_recommendations(
                cat_scores=all_categories,
                trends=trends,
                regressions=regressions,
                capability_history=cap_entries,
            )
        elif prior_runs:
            run_data["comparison_status"] = {
                "comparable": False,
                "reason": "benchmark schema, suite, model, or resource profile changed",
            }

        filepath = self.storage.save_run(self.identity_id, run_data)
        trend_entry = {
            "timestamp": run_data["timestamp"],
            "overall_score": overall,
            "comparison_signature": run_config["comparison_signature"],
            **all_categories,
        }
        self.storage.save_trend(self.identity_id, trend_entry)
        self.evolution_history.record_run(self.identity_id, run_data)
        print(f"\n  Results saved: {filepath}")

    def get_results(self) -> List[WorldResult]:
        return self._world_results
