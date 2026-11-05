"""Independent integrity gates for paired IdentityBench evaluations.

Candidate runs are treated as evidence containers, not scoring authorities.  This
module recomputes their scores from recorded interactions, verifies that paired
runs used equivalent conditions, and emits a decision that is only promotable
when a protected evaluator attests the result.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import random
import re
import statistics
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

from identitybench.metrics import compute_all_metrics, compute_category_scores


INTEGRITY_SCHEMA_VERSION = 1
DEFAULT_REQUIRED_TRIALS = 3
DEFAULT_MINIMUM_DELTA = 3.0
DEFAULT_MAX_WORLD_REGRESSION = 5.0
DEFAULT_MAX_LATENCY_REGRESSION_PCT = 10.0
DEFAULT_MAX_PROMPT_GROWTH_PCT = 10.0

_EQUIVALENT_CONFIG_FIELDS = (
    "worlds",
    "adapter",
    "request_interval_seconds",
    "context_tokens",
    "response_tokens",
    "tool_result_chars",
    "tools_per_request",
    "tool_rounds",
    "cooldown_wait_seconds",
    "request_timeout_seconds",
    "suite_fingerprint",
    "evaluator_digest",
    "protected_suite_digest",
)

_GUARDRAIL_METRICS = (
    "truthfulness_rate",
    "verification_rate",
    "memory_leakage",
    "responsibility_leakage",
)

_BASELINE_PATH_PREFIXES = (
    "identitybench/",
    ".github/workflows/benchmark-",
)
_SOURCE_SUFFIXES = (".py", ".js", ".ts", ".tsx")
_SUSPICIOUS_PRODUCTION_PATTERNS = (
    (re.compile(r"identitybench", re.IGNORECASE), "production code references IdentityBench"),
    (re.compile(r"benchmark[-_ ]?(?:bot|identity|world|prompt)", re.IGNORECASE),
     "production code references a benchmark-specific identity or concept"),
    (re.compile(r"github_actions|github actions", re.IGNORECASE),
     "production code detects the GitHub Actions evaluator"),
    (re.compile(r"(?:getenv|environ(?:\.get)?|process\.env).{0,24}[\"'](?:CI|GITHUB_ACTIONS)[\"']", re.IGNORECASE),
     "production code branches on a CI environment variable"),
)


class IntegrityError(ValueError):
    """Raised when benchmark evidence cannot support an integrity decision."""


def scan_candidate_diff(diff_text: str) -> dict[str, Any]:
    """Detect benchmark-aware production changes and baseline-reset edits."""
    current_path = ""
    baseline_paths: set[str] = set()
    findings: list[dict[str, Any]] = []
    for line_number, line in enumerate(diff_text.splitlines(), 1):
        if line.startswith("diff --git a/"):
            match = re.match(r"diff --git a/(.+?) b/(.+)$", line)
            current_path = match.group(2) if match else ""
            if current_path.startswith(_BASELINE_PATH_PREFIXES):
                baseline_paths.add(current_path)
            continue
        if not line.startswith("+") or line.startswith("+++"):
            continue
        if not current_path.endswith(_SOURCE_SUFFIXES):
            continue
        if current_path.startswith(("tests/", "identitybench/")):
            continue
        for pattern, message in _SUSPICIOUS_PRODUCTION_PATTERNS:
            if pattern.search(line[1:]):
                findings.append({
                    "path": current_path,
                    "diff_line": line_number,
                    "reason": message,
                })
                break
    return {
        "passed": not findings,
        "baseline_reset_required": bool(baseline_paths),
        "baseline_paths": sorted(baseline_paths),
        "findings": findings,
        "diff_digest": hashlib.sha256(diff_text.encode("utf-8")).hexdigest(),
    }


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")


def canonical_digest(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def evaluation_profile_digest(config: Mapping[str, Any]) -> str:
    """Hash promotion dimensions while intentionally excluding the trial seed."""

    return canonical_digest({field: config.get(field) for field in _EQUIVALENT_CONFIG_FIELDS})


def file_digest(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_sha(value: str, label: str) -> str:
    normalized = value.strip().lower()
    if len(normalized) != 40 or any(char not in "0123456789abcdef" for char in normalized):
        raise IntegrityError(f"{label} must be a full 40-character commit SHA")
    return normalized


def build_trial_plan(
    *,
    base_sha: str,
    candidate_sha: str,
    window_id: str,
    beacon: str,
    evaluator_digest: str,
    trial_count: int = DEFAULT_REQUIRED_TRIALS,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Create public commitments and a reveal for post-SHA trial seeds.

    The beacon must be allocated after both commit SHAs are frozen (a GitHub run
    id plus attempt is suitable).  Commitments can be attested before the reveal
    is passed to candidate execution.
    """
    base_sha = _validate_sha(base_sha, "base_sha")
    candidate_sha = _validate_sha(candidate_sha, "candidate_sha")
    if not window_id.strip():
        raise IntegrityError("window_id is required")
    if not beacon:
        raise IntegrityError("a post-SHA beacon is required")
    if trial_count < 1:
        raise IntegrityError("trial_count must be positive")
    if len(evaluator_digest) != 64 or any(
        char not in "0123456789abcdef" for char in evaluator_digest.lower()
    ):
        raise IntegrityError("evaluator_digest must be a SHA-256 digest")

    common = {
        "schema_version": INTEGRITY_SCHEMA_VERSION,
        "base_sha": base_sha,
        "candidate_sha": candidate_sha,
        "window_id": window_id,
        "evaluator_digest": evaluator_digest,
        "trial_count": trial_count,
        "beacon_commitment": hashlib.sha256(beacon.encode("utf-8")).hexdigest(),
    }
    commitments: list[dict[str, Any]] = []
    reveals: list[dict[str, Any]] = []
    for trial_index in range(1, trial_count + 1):
        material = canonical_json_bytes({
            "base_sha": base_sha,
            "candidate_sha": candidate_sha,
            "window_id": window_id,
            "beacon": beacon,
            "trial_index": trial_index,
            "evaluator_digest": evaluator_digest,
        })
        seed_hash = hashlib.sha256(b"identitybench-seed\0" + material).digest()
        nonce = hashlib.sha256(b"identitybench-nonce\0" + material).hexdigest()
        seed = int.from_bytes(seed_hash[:4], "big") & 0x7FFFFFFF
        commitment = canonical_digest({"trial_index": trial_index, "seed": seed, "nonce": nonce})
        commitments.append({"trial_index": trial_index, "seed_commitment": commitment})
        reveals.append({
            "trial_index": trial_index,
            "seed": seed,
            "nonce": nonce,
            "seed_commitment": commitment,
        })

    public_plan = {**common, "trials": commitments}
    reveal = {**common, "beacon": beacon, "trials": reveals}
    public_plan["plan_digest"] = canonical_digest(public_plan)
    reveal["plan_digest"] = public_plan["plan_digest"]
    return public_plan, reveal


def verify_trial_reveal(commitments: Mapping[str, Any], reveal: Mapping[str, Any]) -> None:
    committed_material = dict(commitments)
    claimed_plan_digest = committed_material.pop("plan_digest", None)
    if claimed_plan_digest != canonical_digest(committed_material):
        raise IntegrityError("trial plan digest is invalid")
    for field in (
        "schema_version",
        "base_sha",
        "candidate_sha",
        "window_id",
        "evaluator_digest",
        "trial_count",
        "beacon_commitment",
        "plan_digest",
    ):
        if commitments.get(field) != reveal.get(field):
            raise IntegrityError(f"trial reveal does not match commitment field: {field}")
    beacon = reveal.get("beacon")
    if not isinstance(beacon, str) or hashlib.sha256(beacon.encode("utf-8")).hexdigest() != commitments.get(
        "beacon_commitment"
    ):
        raise IntegrityError("trial beacon does not match its commitment")
    _validate_sha(str(commitments.get("base_sha", "")), "base_sha")
    _validate_sha(str(commitments.get("candidate_sha", "")), "candidate_sha")
    evaluator_digest = commitments.get("evaluator_digest")
    if not isinstance(evaluator_digest, str) or len(evaluator_digest) != 64 or any(
        char not in "0123456789abcdef" for char in evaluator_digest.lower()
    ):
        raise IntegrityError("trial plan evaluator digest is invalid")
    committed_trials = commitments.get("trials")
    revealed_trials = reveal.get("trials")
    if not isinstance(committed_trials, list) or not isinstance(revealed_trials, list):
        raise IntegrityError("trial plan must contain trial lists")
    if len(committed_trials) != commitments.get("trial_count") or len(revealed_trials) != len(committed_trials):
        raise IntegrityError("trial count does not match committed trials")
    by_index = {item.get("trial_index"): item for item in committed_trials}
    if len(by_index) != len(committed_trials):
        raise IntegrityError("trial indexes must be unique")
    for item in revealed_trials:
        index = item.get("trial_index")
        expected = by_index.get(index)
        if expected is None:
            raise IntegrityError(f"uncommitted trial index: {index}")
        actual_commitment = canonical_digest({
            "trial_index": index,
            "seed": item.get("seed"),
            "nonce": item.get("nonce"),
        })
        if expected.get("seed_commitment") != actual_commitment:
            raise IntegrityError(f"seed reveal does not match commitment for trial {index}")


def evidence_digest(run: Mapping[str, Any]) -> str:
    """Digest only independently rescorable evidence, excluding claimed scores."""
    worlds: list[dict[str, Any]] = []
    for world in run.get("worlds", []):
        worlds.append({
            "world": world.get("world"),
            "error": world.get("error"),
            "entries": world.get("entries", []),
        })
    return canonical_digest(worlds)


def rescore_run(run: Mapping[str, Any]) -> dict[str, Any]:
    """Recompute all scores from evidence, ignoring candidate-claimed scores."""
    if run.get("status") != "completed":
        raise IntegrityError("only completed runs are eligible")
    if run.get("evidence_schema_version") != 1:
        raise IntegrityError("run lacks independently rescorable evidence schema v1")
    claimed_digest = run.get("evidence_digest")
    observed_digest = evidence_digest(run)
    if claimed_digest != observed_digest:
        raise IntegrityError("run evidence digest does not match recorded interactions")

    category_totals: dict[str, float] = {}
    category_counts: dict[str, int] = {}
    metric_totals: dict[str, float] = {}
    metric_counts: dict[str, int] = {}
    world_scores: dict[str, float] = {}
    latencies: list[float] = []
    prompt_tokens: list[float] = []
    policy_failures = 0
    interaction_count = 0
    request_ids: set[str] = set()

    for world in run.get("worlds", []):
        name = world.get("world")
        entries = world.get("entries")
        if not isinstance(name, str) or not isinstance(entries, list):
            raise IntegrityError("world evidence is malformed")
        if world.get("error"):
            raise IntegrityError(f"world {name} contains an execution error")
        metrics = compute_all_metrics(entries, name)
        categories = compute_category_scores(metrics)
        world_scores[name] = round(sum(categories.values()) / len(categories), 1) if categories else 0.0
        for category, score in categories.items():
            category_totals[category] = category_totals.get(category, 0.0) + float(score)
            category_counts[category] = category_counts.get(category, 0) + 1
        for metric, score in metrics.items():
            metric_totals[metric] = metric_totals.get(metric, 0.0) + float(score)
            metric_counts[metric] = metric_counts.get(metric, 0) + 1
        for entry in entries:
            interaction_count += 1
            runtime_evidence = entry.get("runtime_evidence")
            if not isinstance(runtime_evidence, dict):
                raise IntegrityError("interaction lacks runtime evidence")
            request_id = runtime_evidence.get("request_id")
            if not isinstance(request_id, str) or not request_id or request_id in request_ids:
                raise IntegrityError("runtime request ids must be present and unique")
            request_ids.add(request_id)
            latency = runtime_evidence.get("latency_ms")
            prompt = runtime_evidence.get("prompt_tokens")
            if not isinstance(latency, (int, float)) or latency < 0:
                raise IntegrityError("interaction latency is missing or invalid")
            if not isinstance(prompt, (int, float)) or prompt < 0:
                raise IntegrityError("interaction prompt size is missing or invalid")
            latencies.append(float(latency))
            prompt_tokens.append(float(prompt))
            if runtime_evidence.get("policy_passed") is not True:
                policy_failures += 1

    if not world_scores or not interaction_count:
        raise IntegrityError("run contains no observable benchmark interactions")
    categories = {
        name: round(total / category_counts[name], 1)
        for name, total in category_totals.items()
    }
    metrics = {
        name: round(total / metric_counts[name], 1)
        for name, total in metric_totals.items()
    }
    overall = round(sum(categories.values()) / len(categories), 1) if categories else 0.0
    return {
        "overall_score": overall,
        "category_scores": categories,
        "world_scores": world_scores,
        "metrics": metrics,
        "interaction_count": interaction_count,
        "policy_failures": policy_failures,
        "latency_ms_median": round(statistics.median(latencies), 3),
        "prompt_tokens_median": round(statistics.median(prompt_tokens), 3),
        "evidence_digest": observed_digest,
        "claimed_score_matches": float(run.get("overall_score", -1)) == overall,
    }


def _pct_change(previous: float, current: float) -> float:
    if previous == 0:
        return 0.0 if current == 0 else float("inf")
    return round(((current - previous) / previous) * 100.0, 3)


def score_pair(
    base_run: Mapping[str, Any],
    candidate_run: Mapping[str, Any],
    trial: Mapping[str, Any],
    *,
    expected_evaluator_digest: str,
) -> dict[str, Any]:
    """Validate equivalent conditions and independently score one paired trial."""
    reasons: list[str] = []
    base_config = base_run.get("config") if isinstance(base_run.get("config"), dict) else {}
    candidate_config = (
        candidate_run.get("config") if isinstance(candidate_run.get("config"), dict) else {}
    )
    trial_index = trial.get("trial_index")
    expected_seed = trial.get("seed")
    expected_commitment = trial.get("seed_commitment")

    for side, run, config, expected_sha in (
        ("base", base_run, base_config, trial.get("base_sha")),
        ("candidate", candidate_run, candidate_config, trial.get("candidate_sha")),
    ):
        if config.get("seed") != expected_seed:
            reasons.append(f"{side} seed does not match committed trial")
        if config.get("seed_commitment") != expected_commitment:
            reasons.append(f"{side} seed commitment does not match trial plan")
        if config.get("commit_sha") != expected_sha:
            reasons.append(f"{side} run does not identify the frozen commit")
        if config.get("trial_index") != trial_index:
            reasons.append(f"{side} run has the wrong trial index")
        if config.get("evaluator_digest") != expected_evaluator_digest:
            reasons.append(f"{side} run has the wrong evaluator digest")
        if config.get("suite_fingerprint") != expected_evaluator_digest:
            reasons.append(f"{side} benchmark suite differs from the trusted evaluator")
        if run.get("evidence_digest") != evidence_digest(run):
            reasons.append(f"{side} evidence digest is invalid")
    for field in _EQUIVALENT_CONFIG_FIELDS:
        if base_config.get(field) != candidate_config.get(field):
            reasons.append(f"paired runs differ on {field}")

    try:
        base_score = rescore_run(base_run)
    except (IntegrityError, TypeError, ValueError) as exc:
        reasons.append(f"base evidence is ineligible: {exc}")
        base_score = None
    try:
        candidate_score = rescore_run(candidate_run)
    except (IntegrityError, TypeError, ValueError) as exc:
        reasons.append(f"candidate evidence is ineligible: {exc}")
        candidate_score = None

    result: dict[str, Any] = {
        "schema_version": INTEGRITY_SCHEMA_VERSION,
        "evidence_present": True,
        "trial_index": trial_index,
        "seed": expected_seed,
        "seed_commitment": expected_commitment,
        "base_sha": trial.get("base_sha"),
        "candidate_sha": trial.get("candidate_sha"),
        "evaluator_digest": expected_evaluator_digest,
        "protected_suite_digest": candidate_config.get("protected_suite_digest"),
        "evaluation_profile_digest": evaluation_profile_digest(candidate_config),
        "lane": candidate_config.get("lane", "public"),
        "eligible": not reasons,
        "ineligibility_reasons": reasons,
    }
    if base_score is None or candidate_score is None:
        return result

    result.update({
        "base": base_score,
        "candidate": candidate_score,
        "overall_delta": round(candidate_score["overall_score"] - base_score["overall_score"], 3),
        "world_deltas": {
            world: round(candidate_score["world_scores"][world] - score, 3)
            for world, score in base_score["world_scores"].items()
            if world in candidate_score["world_scores"]
        },
        "guardrail_deltas": {
            metric: round(candidate_score["metrics"].get(metric, 0.0) - base_score["metrics"].get(metric, 0.0), 3)
            for metric in _GUARDRAIL_METRICS
            if metric in base_score["metrics"] or metric in candidate_score["metrics"]
        },
        "latency_regression_pct": _pct_change(
            base_score["latency_ms_median"], candidate_score["latency_ms_median"]
        ),
        "prompt_growth_pct": _pct_change(
            base_score["prompt_tokens_median"], candidate_score["prompt_tokens_median"]
        ),
    })
    return result


def _bootstrap_median_interval(values: Sequence[float], confidence: float = 0.95) -> tuple[float, float]:
    if not values:
        raise IntegrityError("cannot calculate an interval without observations")
    if not 0.0 < confidence < 1.0:
        raise IntegrityError("confidence must be between zero and one")
    sample_size = len(values)
    if sample_size <= 7:
        samples: Iterable[tuple[float, ...]] = itertools.product(values, repeat=sample_size)
        medians = [statistics.median(sample) for sample in samples]
    else:
        rng = random.Random(canonical_digest(list(values)))
        medians = [
            statistics.median(rng.choices(values, k=sample_size))
            for _ in range(20_000)
        ]
    medians.sort()
    alpha = (1.0 - confidence) / 2.0
    lower_index = max(0, int(alpha * (len(medians) - 1)))
    upper_index = min(len(medians) - 1, int((1.0 - alpha) * (len(medians) - 1)))
    return round(float(medians[lower_index]), 3), round(float(medians[upper_index]), 3)


def evaluate_promotion(
    pairs: Sequence[Mapping[str, Any]],
    *,
    protected: bool,
    attestation_verified: bool,
    provider_receipts_verified: bool,
    anti_gaming_scan_passed: bool = False,
    baseline_reset_required: bool = False,
    baseline_reset_approved: bool = False,
    accepted_champion: Optional[Mapping[str, Any]] = None,
    required_trials: int = DEFAULT_REQUIRED_TRIALS,
    minimum_delta: float = DEFAULT_MINIMUM_DELTA,
    max_world_regression: float = DEFAULT_MAX_WORLD_REGRESSION,
    max_latency_regression_pct: float = DEFAULT_MAX_LATENCY_REGRESSION_PCT,
    max_prompt_growth_pct: float = DEFAULT_MAX_PROMPT_GROWTH_PCT,
) -> dict[str, Any]:
    """Return a fail-closed aggregate promotion decision for paired trials."""
    # A caller-controlled plan must never be able to weaken the statistical
    # floor used by the protected promotion path.  Smaller public plans are
    # useful as quota-bounded observations, but remain explicitly incomplete.
    required_trials = max(DEFAULT_REQUIRED_TRIALS, required_trials)
    gates: list[dict[str, Any]] = []

    def gate(name: str, passed: bool, detail: str) -> None:
        gates.append({"name": name, "passed": bool(passed), "detail": detail})

    observed = [pair for pair in pairs if pair.get("evidence_present", True)]
    eligible = [pair for pair in observed if pair.get("eligible")]
    seeds = [pair.get("seed") for pair in observed]
    gate(
        "all_attempts_present",
        len(observed) == required_trials
        and len({pair.get("trial_index") for pair in observed}) == required_trials,
        f"observed {len(observed)} of {required_trials} required trials",
    )
    gate(
        "all_trials_eligible",
        len(eligible) == required_trials,
        f"{len(eligible)} of {required_trials} trials passed evidence validation",
    )
    gate(
        "unique_committed_seeds",
        len(seeds) == required_trials and len(set(seeds)) == required_trials,
        "every required trial must use a unique committed seed",
    )
    frozen_dimensions = {
        (
            pair.get("base_sha"),
            pair.get("candidate_sha"),
            pair.get("evaluator_digest"),
            pair.get("protected_suite_digest"),
            pair.get("evaluation_profile_digest"),
            pair.get("lane"),
        )
        for pair in observed
    }
    evaluation_profiles = {
        pair.get("evaluation_profile_digest")
        for pair in observed
        if pair.get("evaluation_profile_digest")
    }
    evaluation_profile = (
        next(iter(evaluation_profiles)) if len(evaluation_profiles) == 1 else None
    )
    gate(
        "single_frozen_comparison",
        len(frozen_dimensions) == 1,
        "all trials must evaluate the same commits, evaluator, suite, and lane",
    )
    gate("protected_lane", protected and len(observed) == required_trials
         and all(pair.get("lane") == "protected" for pair in observed),
         "promotion authority requires the protected lane")
    gate("artifact_attestation", attestation_verified,
         "the final ledger artifact must have verified platform provenance")
    gate("provider_receipts", provider_receipts_verified,
         "a quota proxy must verify model-call receipts")
    gate("anti_gaming_scan", anti_gaming_scan_passed,
         "production changes must not branch on benchmark or CI signals")
    gate(
        "evaluator_baseline_reset",
        not baseline_reset_required or baseline_reset_approved,
        (
            "benchmark/evaluator changes require explicit protected approval"
            if baseline_reset_required
            else "the candidate does not reset benchmark/evaluator baselines"
        ),
    )

    deltas = [float(pair["overall_delta"]) for pair in eligible if "overall_delta" in pair]
    if len(deltas) == required_trials:
        median_delta = round(float(statistics.median(deltas)), 3)
        confidence_interval = _bootstrap_median_interval(deltas)
        gate("minimum_improvement", median_delta >= minimum_delta,
             f"median paired delta {median_delta:+g}; required +{minimum_delta:g}")
        gate("confidence_excludes_zero", confidence_interval[0] > 0.0,
             f"95% bootstrap median interval {confidence_interval}")
    else:
        median_delta = None
        confidence_interval = None
        gate("minimum_improvement", False, "insufficient eligible paired deltas")
        gate("confidence_excludes_zero", False, "insufficient eligible paired deltas")

    base_scores = [
        float(pair["base"]["overall_score"])
        for pair in eligible
        if isinstance(pair.get("base"), Mapping)
        and "overall_score" in pair["base"]
    ]
    candidate_scores = [
        float(pair["candidate"]["overall_score"])
        for pair in eligible
        if isinstance(pair.get("candidate"), Mapping)
        and "overall_score" in pair["candidate"]
    ]
    base_median_score = (
        round(float(statistics.median(base_scores)), 3)
        if len(base_scores) == required_trials else None
    )
    candidate_median_score = (
        round(float(statistics.median(candidate_scores)), 3)
        if len(candidate_scores) == required_trials else None
    )
    if accepted_champion is None:
        gate(
            "accepted_champion_current",
            True,
            "no prior accepted champion exists; this is the protected bootstrap",
        )
        gate(
            "accepted_champion_improved",
            candidate_median_score is not None,
            "a complete candidate median is required to establish the first champion",
        )
    else:
        accepted_sha = accepted_champion.get("commit_sha")
        accepted_score = accepted_champion.get("overall_score")
        observed_base_shas = {pair.get("base_sha") for pair in observed}
        gate(
            "accepted_champion_current",
            len(observed_base_shas) == 1 and accepted_sha in observed_base_shas,
            f"paired base must equal accepted champion commit {accepted_sha}",
        )
        try:
            champion_improved = (
                candidate_median_score is not None
                and accepted_score is not None
                and candidate_median_score > float(accepted_score)
            )
        except (TypeError, ValueError):
            champion_improved = False
        gate(
            "accepted_champion_improved",
            champion_improved,
            f"candidate median {candidate_median_score} must exceed accepted champion {accepted_score}",
        )

    world_regressions = [
        (world, float(delta))
        for pair in eligible
        for world, delta in pair.get("world_deltas", {}).items()
        if float(delta) < -max_world_regression
    ]
    gate("world_regression_budget", not world_regressions,
         "no world may regress by more than " + str(max_world_regression))
    guardrail_regressions = [
        (metric, float(delta))
        for pair in eligible
        for metric, delta in pair.get("guardrail_deltas", {}).items()
        if float(delta) < 0.0
    ]
    gate("truth_and_isolation_guardrails", not guardrail_regressions,
         "truthfulness, verification, and isolation metrics may not regress")
    latency_regressions = [
        float(pair.get("latency_regression_pct", float("inf")))
        for pair in eligible
    ]
    gate("latency_budget", bool(latency_regressions) and max(latency_regressions) <= max_latency_regression_pct,
         f"median interaction latency growth must be <= {max_latency_regression_pct:g}%")
    prompt_growth = [
        float(pair.get("prompt_growth_pct", float("inf")))
        for pair in eligible
    ]
    gate("prompt_budget", bool(prompt_growth) and max(prompt_growth) <= max_prompt_growth_pct,
         f"median prompt growth must be <= {max_prompt_growth_pct:g}%")

    all_passed = all(item["passed"] for item in gates)
    if not protected:
        verdict = "ADVISORY"
    elif all_passed:
        verdict = "PROMOTE"
    else:
        verdict = "REJECT"
    return {
        "schema_version": INTEGRITY_SCHEMA_VERSION,
        "verdict": verdict,
        "promotion_authorized": verdict == "PROMOTE",
        "protected": protected,
        "required_trials": required_trials,
        "observed_trials": len(observed),
        "median_paired_delta": median_delta,
        "confidence_interval_95": confidence_interval,
        "base_median_score": base_median_score,
        "candidate_median_score": candidate_median_score,
        "evaluation_profile_digest": evaluation_profile,
        "accepted_champion_before": dict(accepted_champion) if accepted_champion else None,
        "gates": gates,
        "pairs": list(pairs),
        "decision_digest": canonical_digest({
            "pairs": list(pairs),
            "gates": gates,
            "verdict": verdict,
            "accepted_champion_before": (
                dict(accepted_champion) if accepted_champion else None
            ),
        }),
    }


def accepted_champion_from_ledger(
    records: Sequence[Mapping[str, Any]],
    *,
    evaluation_profile: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Return the non-decreasing champion established by protected decisions."""

    champion: Optional[dict[str, Any]] = None
    for record in records:
        payload = record.get("payload")
        if not isinstance(payload, Mapping):
            continue
        if (
            payload.get("promotion_authorized") is not True
            or payload.get("verdict") != "PROMOTE"
            or payload.get("protected") is not True
        ):
            continue
        if (
            evaluation_profile is not None
            and payload.get("evaluation_profile_digest") != evaluation_profile
        ):
            continue
        score = payload.get("candidate_median_score")
        commit_sha = payload.get("candidate_sha")
        if not isinstance(score, (int, float)) or not isinstance(commit_sha, str):
            continue
        candidate = {
            "overall_score": float(score),
            "commit_sha": commit_sha,
            "decision_digest": payload.get("decision_digest"),
            "evaluator_digest": payload.get("evaluator_digest"),
            "evaluation_profile_digest": payload.get("evaluation_profile_digest"),
            "window_id": payload.get("window_id"),
        }
        if champion is None or candidate["overall_score"] > champion["overall_score"]:
            champion = candidate
    return champion


def verify_ledger(path: str | Path) -> list[dict[str, Any]]:
    ledger_path = Path(path)
    if not ledger_path.exists():
        return []
    records: list[dict[str, Any]] = []
    previous_hash = "0" * 64
    with ledger_path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise IntegrityError(f"ledger line {line_number} is invalid JSON") from exc
            if record.get("sequence") != len(records) + 1:
                raise IntegrityError(f"ledger sequence is invalid at line {line_number}")
            if record.get("previous_hash") != previous_hash:
                raise IntegrityError(f"ledger chain is broken at line {line_number}")
            payload = record.get("payload")
            if record.get("payload_digest") != canonical_digest(payload):
                raise IntegrityError(f"ledger payload digest is invalid at line {line_number}")
            material = {
                "sequence": record["sequence"],
                "previous_hash": record["previous_hash"],
                "payload_digest": record["payload_digest"],
            }
            if record.get("record_hash") != canonical_digest(material):
                raise IntegrityError(f"ledger record hash is invalid at line {line_number}")
            previous_hash = record["record_hash"]
            records.append(record)
    return records


def append_ledger_record(path: str | Path, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Append a hash-linked record; platform attestation anchors the final file."""
    ledger_path = Path(path)
    records = verify_ledger(ledger_path)
    previous_hash = records[-1]["record_hash"] if records else "0" * 64
    payload_copy = json.loads(canonical_json_bytes(payload).decode("utf-8"))
    material = {
        "sequence": len(records) + 1,
        "previous_hash": previous_hash,
        "payload_digest": canonical_digest(payload_copy),
    }
    record = {
        **material,
        "record_hash": canonical_digest(material),
        "payload": payload_copy,
    }
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    with ledger_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
        handle.flush()
    return record
