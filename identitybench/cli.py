from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from dotenv import load_dotenv

load_dotenv()

from identitybench.engine import IdentityBench, DEFAULT_WORLDS, SMOKE_WORLDS
from identitybench.reporting import (
    build_comparison_data,
    generate_markdown_report,
    generate_regression_summary,
    generate_report_text,
)
from identitybench.storage import BenchmarkStorage
from identitybench.reports.weekly import generate_weekly_report, format_weekly_report
from identitybench.analytics.roi import calculate_capability_roi, format_roi_entry
from identitybench.analytics.timeline import build_evolution_timeline, format_timeline
from identitybench.analytics.regression import detect_regressions, format_regression_warning
from identitybench.visualization.trends import render_trend_chart
from identitybench.journal.capability_journal import CapabilityJournal
from identitybench.journal.evolution_history import EvolutionHistory
from identitybench.atlas.health import compute_identity_health, format_health
from identitybench.atlas.prediction import predict_all_categories, format_prediction
from identitybench.atlas.forecast import build_forecast, format_forecast
from identitybench.atlas.strategy import generate_strategies, format_strategies
from identitybench.atlas.decision_engine import (
    analyze_capability_impact,
    generate_evidence_recommendations,
    format_evidence_recommendation,
)
from identitybench.atlas.capability_lifecycle import (
    compute_capability_ranking,
    format_capability_ranking,
    explain_score_change,
)
from identitybench.endurance import EnduranceMonitor
from identitybench.provenance import comparison_signature, suite_fingerprint
from identitybench.integrity import (
    INTEGRITY_SCHEMA_VERSION,
    IntegrityError,
    accepted_champion_from_ledger,
    append_ledger_record,
    build_trial_plan,
    evaluate_promotion,
    evaluation_profile_digest,
    scan_candidate_diff,
    score_pair,
    verify_ledger,
    verify_trial_reveal,
)
from identitybench.champion import (
    assess_observed_champion,
    rescored_run,
    select_observed_champion,
)


def cmd_run(args: argparse.Namespace) -> None:
    engine = IdentityBench(
        identity_id=args.identity,
        storage_path=args.storage_dir,
        request_timeout_seconds=args.request_timeout,
    )
    engine.load_identity()
    world_classes = DEFAULT_WORLDS if args.mode == "full" else SMOKE_WORLDS
    if args.worlds:
        world_map = {w.name.lower(): w for w in DEFAULT_WORLDS}
        selected = []
        for name in args.worlds:
            name_lower = name.lower()
            if name_lower in world_map:
                selected.append(world_map[name_lower])
            else:
                print(f"Unknown world: {name}. Available: {list(world_map.keys())}")
                return
        world_classes = selected
    print(f"Running IdentityBench [{args.mode}] for {args.identity}...")
    engine.run(world_classes=world_classes, seed=args.seed)
    monitor = EnduranceMonitor(benchmark_dir=args.storage_dir)
    sample = monitor.record_latest(args.identity)
    print(
        "  Endurance sample: "
        f"restart={sample['restart_recovery_pct']}% "
        f"consistency={sample['identity_consistency_pct']}%"
    )


def cmd_endurance(args: argparse.Namespace) -> None:
    monitor = EnduranceMonitor(
        benchmark_dir=args.storage_dir,
        identity_store=args.identity_store,
    )
    if args.action == "record":
        sample = monitor.record_latest(args.identity)
        print(json.dumps(sample, indent=2))
        return
    report = monitor.report(args.identity)
    if args.output:
        with open(args.output, "w") as handle:
            handle.write(report)
        print(f"Endurance report written to {args.output}")
    else:
        print(report)


def _read_json(path: str | Path) -> dict[str, Any]:
    with Path(path).open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise IntegrityError(f"expected a JSON object in {path}")
    return value


def _write_json(path: str | Path, value: Mapping[str, Any]) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")


def cmd_integrity_plan(args: argparse.Namespace) -> None:
    evaluator = args.evaluator_digest or suite_fingerprint()
    commitments, reveal = build_trial_plan(
        base_sha=args.base_sha,
        candidate_sha=args.candidate_sha,
        window_id=args.window_id,
        beacon=args.beacon,
        evaluator_digest=evaluator,
        trial_count=args.trials,
    )
    _write_json(args.commitments, commitments)
    _write_json(args.reveal, reveal)
    print(f"Trial commitments written to {args.commitments}")
    print(f"Trial reveal written to {args.reveal}")
    print(f"Plan digest: {commitments['plan_digest']}")


def cmd_integrity_trial(args: argparse.Namespace) -> None:
    commitments = _read_json(args.commitments)
    reveal = _read_json(args.reveal)
    verify_trial_reveal(commitments, reveal)
    trial = next(
        (item for item in reveal["trials"] if item.get("trial_index") == args.trial_index),
        None,
    )
    if trial is None:
        raise IntegrityError(f"trial {args.trial_index} is not in the reveal")
    values = {
        "seed": trial["seed"],
        "seed_commitment": trial["seed_commitment"],
        "base_sha": reveal["base_sha"],
        "candidate_sha": reveal["candidate_sha"],
        "evaluator_digest": reveal["evaluator_digest"],
        "plan_digest": reveal["plan_digest"],
    }
    if args.github_output:
        with Path(args.github_output).open("a", encoding="utf-8") as handle:
            for key, value in values.items():
                handle.write(f"{key}={value}\n")
    print(json.dumps(values, sort_keys=True))


def _integrity_summary(decision: Mapping[str, Any]) -> str:
    lines = [
        "# IdentityBench integrity decision",
        "",
        f"Verdict: **{decision['verdict']}**",
        f"Promotion authorized: **{str(decision['promotion_authorized']).lower()}**",
        f"Trials: {decision['observed_trials']}/{decision['required_trials']}",
        f"Median paired delta: {decision.get('median_paired_delta')}",
        f"Accepted champion before: {decision.get('accepted_champion_before')}",
        f"Candidate median score: {decision.get('candidate_median_score')}",
        f"95% interval: {decision.get('confidence_interval_95')}",
        "",
        "## Gates",
        "",
    ]
    for item in decision["gates"]:
        marker = "PASS" if item["passed"] else "FAIL"
        lines.append(f"- `{marker}` {item['name']}: {item['detail']}")
    return "\n".join(lines) + "\n"


def cmd_integrity_gate(args: argparse.Namespace) -> None:
    commitments = _read_json(args.commitments)
    reveal = _read_json(args.reveal)
    verify_trial_reveal(commitments, reveal)
    evaluator = suite_fingerprint()
    if commitments.get("evaluator_digest") != evaluator:
        raise IntegrityError(
            "the executing evaluator does not match the committed evaluator digest"
        )

    pairs = []
    for revealed_trial in reveal["trials"]:
        index = revealed_trial["trial_index"]
        trial_dir = Path(args.pairs_dir) / f"trial-{index}"
        trial = {
            **revealed_trial,
            "base_sha": reveal["base_sha"],
            "candidate_sha": reveal["candidate_sha"],
        }
        loaded: dict[str, dict[str, Any]] = {}
        load_errors: list[str] = []
        for side in ("base", "candidate"):
            path = trial_dir / f"{side}.json"
            try:
                loaded[side] = _read_json(path)
            except (OSError, UnicodeError, json.JSONDecodeError, IntegrityError) as exc:
                load_errors.append(f"{side} evidence unavailable: {exc}")
        if load_errors:
            available = loaded.get("candidate") or loaded.get("base") or {}
            config = available.get("config") if isinstance(available.get("config"), dict) else {}
            pairs.append({
                "schema_version": INTEGRITY_SCHEMA_VERSION,
                "evidence_present": False,
                "trial_index": index,
                "seed": revealed_trial.get("seed"),
                "seed_commitment": revealed_trial.get("seed_commitment"),
                "base_sha": reveal["base_sha"],
                "candidate_sha": reveal["candidate_sha"],
                "evaluator_digest": evaluator,
                "protected_suite_digest": config.get("protected_suite_digest"),
                "evaluation_profile_digest": evaluation_profile_digest(config),
                "lane": config.get("lane", "public"),
                "eligible": False,
                "ineligibility_reasons": load_errors,
            })
            continue
        pairs.append(score_pair(
            loaded["base"],
            loaded["candidate"],
            trial,
            expected_evaluator_digest=evaluator,
        ))

    diff_scan = _read_json(args.diff_scan) if args.diff_scan else {
        "passed": False,
        "baseline_reset_required": False,
        "findings": [{"reason": "no independently generated diff scan was supplied"}],
    }
    ledger_records = verify_ledger(args.ledger) if args.ledger else []
    observed_profiles = {
        pair.get("evaluation_profile_digest")
        for pair in pairs
        if pair.get("evaluation_profile_digest")
    }
    evaluation_profile = next(iter(observed_profiles)) if len(observed_profiles) == 1 else None
    accepted_champion = accepted_champion_from_ledger(
        ledger_records, evaluation_profile=evaluation_profile
    )
    decision = evaluate_promotion(
        pairs,
        protected=args.protected,
        attestation_verified=args.evidence_attestations_verified,
        provider_receipts_verified=args.provider_receipts_verified,
        anti_gaming_scan_passed=diff_scan.get("passed") is True,
        baseline_reset_required=diff_scan.get("baseline_reset_required") is True,
        baseline_reset_approved=args.baseline_reset_approved,
        accepted_champion=accepted_champion,
        required_trials=commitments["trial_count"],
        minimum_delta=args.minimum_delta,
        max_world_regression=args.max_world_regression,
        max_latency_regression_pct=args.max_latency_regression_pct,
        max_prompt_growth_pct=args.max_prompt_growth_pct,
    )
    decision["plan_digest"] = commitments["plan_digest"]
    decision["window_id"] = commitments["window_id"]
    decision["base_sha"] = commitments["base_sha"]
    decision["candidate_sha"] = commitments["candidate_sha"]
    decision["evaluator_digest"] = evaluator
    decision["evaluation_profile_digest"] = evaluation_profile
    decision["diff_scan"] = diff_scan
    if decision["promotion_authorized"]:
        decision["accepted_champion_after"] = {
            "overall_score": decision["candidate_median_score"],
            "commit_sha": commitments["candidate_sha"],
            "decision_digest": decision["decision_digest"],
            "evaluator_digest": evaluator,
            "evaluation_profile_digest": evaluation_profile,
            "window_id": commitments["window_id"],
        }
    else:
        decision["accepted_champion_after"] = accepted_champion
    _write_json(args.output, decision)
    if args.summary:
        destination = Path(args.summary)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(_integrity_summary(decision), encoding="utf-8")
    if args.ledger:
        record = append_ledger_record(args.ledger, decision)
        decision["ledger_record_hash"] = record["record_hash"]
        _write_json(args.output, decision)
    print(_integrity_summary(decision), end="")
    if args.enforce and not decision["promotion_authorized"]:
        raise SystemExit(1)


def cmd_integrity_verify_ledger(args: argparse.Namespace) -> None:
    records = verify_ledger(args.ledger)
    head = records[-1]["record_hash"] if records else "0" * 64
    print(json.dumps({"records": len(records), "head": head}, sort_keys=True))


def cmd_integrity_scan(args: argparse.Namespace) -> None:
    if args.diff_file == "-":
        diff_text = sys.stdin.read()
    else:
        diff_text = Path(args.diff_file).read_text(encoding="utf-8")
    result = scan_candidate_diff(diff_text)
    _write_json(args.output, result)
    print(json.dumps(result, sort_keys=True))
    if args.enforce and not result["passed"]:
        raise SystemExit(1)


def cmd_report(args: argparse.Namespace) -> None:
    storage = BenchmarkStorage(root_dir=args.storage_dir)
    if args.identity:
        identities = [args.identity]
    else:
        identities = storage.list_identities()
    for identity_id in identities:
        run = storage.load_latest_run(identity_id)
        if not run:
            print(f"No benchmark runs found for '{identity_id}'.")
            continue
        trends = storage.load_trends(identity_id)
        if args.markdown:
            report = generate_markdown_report(run, trend_data=trends)
            if args.output:
                with open(args.output, "w") as f:
                    f.write(report)
                print(f"Markdown report written to {args.output}")
            else:
                print(report)
        else:
            comparison = None
            if args.compare:
                ids = args.compare.split(",")
                comparison = build_comparison_data(storage, ids)
            text = generate_report_text(run, trend_data=trends, comparison_data=comparison)
            print(text)


def cmd_history(args: argparse.Namespace) -> None:
    storage = BenchmarkStorage(root_dir=args.storage_dir)
    identity_id = args.identity
    runs = storage.list_runs(identity_id)
    if not runs:
        print(f"No benchmark history for '{identity_id}'.")
        return
    print(f"Benchmark history for {identity_id}:")
    print(f"  {'#':>4s}  {'Date':>25s}  {'Score':>7s}")
    print(f"  {'-'*40}")
    for i, r in enumerate(runs, 1):
        run_data = storage.load_run(identity_id, r["filename"])
        score = run_data.get("overall_score", "?") if run_data else "?"
        ts = r["timestamp"][:19]
        print(f"  {i:>4d}  {ts:>25s}  {score:>7s}")


def cmd_compare(args: argparse.Namespace) -> None:
    storage = BenchmarkStorage(root_dir=args.storage_dir)
    if args.identities:
        ids = args.identities
        comparison = build_comparison_data(storage, ids)
        report = generate_report_text({}, comparison_data=comparison)
        report = report.replace("IdentityBench Report", "IdentityBench Comparison")
        print(report)
    elif args.last and args.identity_id:
        identity_id = args.identity_id
        runs = storage.list_runs(identity_id)
        loaded_runs = [
            storage.load_run(identity_id, entry["filename"])
            for entry in runs
        ]
        loaded_runs = [run for run in loaded_runs if run and run.get("status") != "failed"]
        latest_signature = comparison_signature(loaded_runs[0]) if loaded_runs else None
        loaded_runs = [
            run for run in loaded_runs
            if comparison_signature(run) == latest_signature
        ]
        recent = loaded_runs[:args.last]
        if not recent:
            print("Need at least 1 completed run to establish a baseline. Found 0.")
            return
        curr_run_data = recent[0]
        if args.baseline == "champion":
            champion = select_observed_champion(
                loaded_runs[1:], signature=latest_signature
            )
            assessment, _ = assess_observed_champion(
                curr_run_data, loaded_runs[1:]
            )
            if assessment["status"] == "INELIGIBLE":
                raise IntegrityError(
                    "latest run cannot be compared to the verified champion: "
                    + assessment["reason"]
                )
            if champion is None:
                print(f"Comparison for {identity_id} (verified observed champion baseline):\n")
                print(
                    "  Baseline initialized at "
                    f"{assessment['champion_score']:g}; the next comparable run must beat it."
                )
                print("  Authority: advisory observation; protected paired evidence is required for promotion.")
                return
            prev_run_data = rescored_run(champion)
            current = select_observed_champion(
                [curr_run_data], signature=latest_signature
            )
            if current is None:  # Defensive: assessment above already verifies it.
                raise IntegrityError("latest run unexpectedly lost champion eligibility")
            curr_run_data = rescored_run(current)
            comparison_label = "verified observed champion baseline"
        else:
            if len(recent) < 2:
                print(f"Need at least 2 comparable runs to compare. Found {len(recent)}.")
                return
            prev_run_data = recent[1]
            comparison_label = f"last {args.last} runs"
        if curr_run_data and prev_run_data:
            summary = generate_regression_summary(prev_run_data, curr_run_data)
            print(f"Comparison for {identity_id} ({comparison_label}):\n")
            ov = summary["overall"]
            arrow = "▲" if ov["change"] > 0 else ("▼" if ov["change"] < 0 else "─")
            print(f"  Overall: {ov['previous']} → {ov['current']} ({arrow}{ov['change']:+g}) [{ov['verdict']}]")
            if summary["regressions"]:
                print(f"\n  Regressions:")
                for r in summary["regressions"]:
                    print(f"    ▼ {r['category']:20s} {r['previous']} → {r['current']} ({r['change']:+g})")
            if summary["improvements"]:
                print(f"\n  Improvements:")
                for r in summary["improvements"]:
                    print(f"    ▲ {r['category']:20s} {r['previous']} → {r['current']} ({r['change']:+g})")
            if not summary["regressions"] and not summary["improvements"]:
                print(f"\n  No significant changes (threshold: {summary['threshold']} pts).")
            if args.baseline == "champion":
                if assessment["status"] == "ADVANCED":
                    print(
                        "\n  Observed champion advanced; this remains advisory until "
                        "the protected paired gate authorizes promotion."
                    )
                else:
                    print(
                        f"\n  Champion retained at {assessment['champion_score']:g}; "
                        "the lower or unsafe result was not adopted as the next baseline."
                    )
    else:
        print("Specify --identities for cross-identity comparison or --id with --last for historical comparison.")


def cmd_weekly(args: argparse.Namespace) -> None:
    storage = BenchmarkStorage(root_dir=args.storage_dir)
    identity_id = args.identity
    run_history = storage.load_all_runs(identity_id)
    if not run_history:
        print(f"No benchmark history for '{identity_id}'.")
        return
    cap_journal = CapabilityJournal(args.storage_dir)
    caps = cap_journal.list_capabilities(identity_id)
    cap_entries = []
    for cap_id in caps:
        cap_entries.extend(cap_journal.get_journal(identity_id, cap_id))
    report = generate_weekly_report(
        identity_id=identity_id,
        run_history=run_history,
        capability_history=cap_entries,
        fact_counts=None,
    )
    text = format_weekly_report(report)
    print(text)
    if args.output:
        with open(args.output, "w") as f:
            f.write(text)
        print(f"Weekly report written to {args.output}")


def cmd_roi(args: argparse.Namespace) -> None:
    storage = BenchmarkStorage(root_dir=args.storage_dir)
    identity_id = args.identity
    run_history = storage.load_all_runs(identity_id)
    cap_journal = CapabilityJournal(args.storage_dir)
    caps = cap_journal.list_capabilities(identity_id)
    cap_entries = []
    for cap_id in caps:
        cap_entries.extend(cap_journal.get_journal(identity_id, cap_id))
    roi = calculate_capability_roi(cap_entries, run_history)
    if not roi:
        print(f"No capability data for '{identity_id}'.")
        return
    print(f"Capability ROI for {identity_id}:")
    print(f"  {'-'*40}")
    for entry in roi:
        print(format_roi_entry(entry))
        print("")


def cmd_timeline(args: argparse.Namespace) -> None:
    storage = BenchmarkStorage(root_dir=args.storage_dir)
    identity_id = args.identity
    run_history = storage.load_all_runs(identity_id)
    cap_journal = CapabilityJournal(args.storage_dir)
    caps = cap_journal.list_capabilities(identity_id)
    cap_entries = []
    for cap_id in caps:
        cap_entries.extend(cap_journal.get_journal(identity_id, cap_id))
    timeline = build_evolution_timeline(run_history, cap_entries)
    print(f"Evolution timeline for {identity_id}:")
    print(format_timeline(timeline, max_entries=args.max_entries or 30))


def cmd_prometheus(args: argparse.Namespace) -> None:
    identity_id = args.identity
    evo = EvolutionHistory(args.storage_dir)
    cap_journal = CapabilityJournal(args.storage_dir)
    caps = cap_journal.list_capabilities(identity_id)
    cap_entries = []
    for cap_id in caps:
        cap_entries.extend(cap_journal.get_journal(identity_id, cap_id))
    health = evo.compute_prometheus_health(identity_id, cap_entries)
    print(f"Prometheus Health for {identity_id}:")
    print(f"  {'-'*40}")
    print(f"  Overall Health:           {health.get('overall_health', 0):.1f}/100")
    print(f"  Gap Detection Accuracy:   {health.get('gap_detection_accuracy', 0):.1f}")
    print(f"  Search Quality:           {health.get('search_quality', 0):.1f}")
    print(f"  Install Success Rate:     {health.get('install_success_rate', 0):.1f}%")
    print(f"  Validation Success:       {health.get('validation_success', 0):.1f}%")
    print(f"  Retry Success:            {health.get('retry_success', 0):.1f}%")
    print(f"  Capability Longevity:     {health.get('capability_longevity', 0):.1f}")


def cmd_regressions(args: argparse.Namespace) -> None:
    storage = BenchmarkStorage(root_dir=args.storage_dir)
    identity_id = args.identity
    trends = storage.load_trends(identity_id)
    if not trends or len(trends) < 3:
        print(f"Need at least 3 runs to detect regressions. Found {len(trends)}.")
        return
    signals = detect_regressions(trends, consecutive_threshold=args.threshold or 3)
    if not signals:
        print(f"No regressions detected for '{identity_id}'.")
        return
    print(f"Regression signals for {identity_id}:")
    print(f"  {'-'*40}")
    for sig in signals:
        print(format_regression_warning(sig))
        print("")


def cmd_forecast(args: argparse.Namespace) -> None:
    storage = BenchmarkStorage(root_dir=args.storage_dir)
    identity_id = args.identity
    run_history = storage.load_all_runs(identity_id)
    if not run_history:
        print(f"No benchmark history for '{identity_id}'.")
        return
    trends = storage.load_trends(identity_id)
    if not trends:
        print(f"No trend data for '{identity_id}'.")
        return
    latest_run = run_history[-1] if run_history else {}
    category_scores = latest_run.get("category_scores", {})
    if not category_scores:
        print(f"No category scores found in latest run for '{identity_id}'.")
        return

    predictions = predict_all_categories(trends, steps_ahead=5)

    if args.command == "forecast":
        forecast = build_forecast(category_scores, predictions, weeks=args.weeks)
        print(f"\nAtlas Forecast for {identity_id}:\n")
        print(format_forecast(forecast))
        return

    print(f"\nAtlas Strategic Analysis for {identity_id}:\n")
    print("─" * 50)

    health = compute_identity_health(
        category_scores=category_scores,
        regressions=None,
        predictions=predictions,
    )
    print("IDENTITY HEALTH")
    print(format_health(health))
    print()

    cap_journal = CapabilityJournal(args.storage_dir)
    caps = cap_journal.list_capabilities(identity_id)
    cap_entries = []
    for cap_id in caps:
        cap_entries.extend(cap_journal.get_journal(identity_id, cap_id))

    roi_data = []
    if cap_entries:
        from identitybench.analytics.roi import calculate_capability_roi
        roi_data = calculate_capability_roi(cap_entries, run_history)

    impacts = analyze_capability_impact(cap_entries, run_history)
    evidence_recs = generate_evidence_recommendations(
        current_scores=category_scores,
        capability_impacts=impacts,
        predictions=predictions,
    )
    ranking = compute_capability_ranking(roi_data, run_history, cap_entries)
    strategies = generate_strategies(health, predictions, evidence_recs, ranking)

    print("PREDICTIONS")
    for pred in predictions:
        print(format_prediction(pred))
        print()
    print("─" * 50)

    print("EVIDENCE-BASED RECOMMENDATIONS")
    if evidence_recs:
        for rec in evidence_recs[:5]:
            print(format_evidence_recommendation(rec))
            print()
    else:
        print("  No recommendations at this time.")
    print("─" * 50)

    print("CAPABILITY RANKING")
    print(format_capability_ranking(ranking))
    print("─" * 50)

    print("STRATEGIES")
    print(format_strategies(strategies))
    print("─" * 50)

    if args.output:
        lines = []
        lines.append(f"Atlas Strategic Analysis for {identity_id}")
        lines.append("")
        lines.append("IDENTITY HEALTH")
        lines.append(format_health(health))
        lines.append("")
        lines.append("PREDICTIONS")
        for pred in predictions:
            lines.append(format_prediction(pred))
        lines.append("")
        lines.append("EVIDENCE-BASED RECOMMENDATIONS")
        for rec in evidence_recs[:5]:
            lines.append(format_evidence_recommendation(rec))
        lines.append("")
        lines.append("CAPABILITY RANKING")
        lines.append(format_capability_ranking(ranking))
        lines.append("")
        lines.append("STRATEGIES")
        lines.append(format_strategies(strategies))
        with open(args.output, "w") as f:
            f.write("\n".join(lines))
        print(f"Atlas report written to {args.output}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="identitybench",
        description="IdentityBench — Benchmarking framework for IdentityOS identities",
    )
    parser.add_argument("--storage-dir", default=".identitybench", help="Benchmark data directory")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="Run benchmarks against an identity")
    p_run.add_argument("identity", help="Identity ID to benchmark")
    p_run.add_argument("--mode", choices=["full", "smoke"], default="full", help="Benchmark mode")
    p_run.add_argument("--worlds", nargs="*", help="Specific worlds to run")
    p_run.add_argument("--seed", type=int, default=42, help="RNG seed for determinism")
    p_run.add_argument(
        "--request-timeout", type=float, default=None, metavar="SECONDS",
        help="Total generation budget per interaction, including tool rounds",
    )
    p_run.set_defaults(func=cmd_run)

    p_report = sub.add_parser("report", help="Generate benchmark report")
    p_report.add_argument("identity", nargs="?", default=None, help="Identity ID (omit for all)")
    p_report.add_argument("--markdown", action="store_true", help="Output Markdown")
    p_report.add_argument("-o", "--output", help="Write report to file")
    p_report.add_argument("--compare", help="Comma-separated identity IDs to compare")
    p_report.set_defaults(func=cmd_report)

    p_history = sub.add_parser("history", help="Show benchmark history")
    p_history.add_argument("identity", help="Identity ID")
    p_history.set_defaults(func=cmd_history)

    p_compare = sub.add_parser("compare", help="Compare identities or historical runs")
    p_compare.add_argument("--identities", nargs="+", default=[], help="Identity IDs to compare across identities")
    p_compare.add_argument("--id", dest="identity_id", default=None, help="Identity ID (for --last)")
    p_compare.add_argument("--last", type=int, default=0, help="Compare last N runs of identity")
    p_compare.add_argument(
        "--baseline",
        choices=["champion", "previous"],
        default="champion",
        help="Use the verified high-water champion (default) or immediately previous run",
    )
    p_compare.set_defaults(func=cmd_compare)

    p_weekly = sub.add_parser("weekly", help="Generate weekly engineering report")
    p_weekly.add_argument("identity", help="Identity ID")
    p_weekly.add_argument("-o", "--output", help="Write report to file")
    p_weekly.set_defaults(func=cmd_weekly)

    p_roi = sub.add_parser("roi", help="Show capability ROI analysis")
    p_roi.add_argument("identity", help="Identity ID")
    p_roi.set_defaults(func=cmd_roi)

    p_timeline = sub.add_parser("timeline", help="Show evolution timeline")
    p_timeline.add_argument("identity", help="Identity ID")
    p_timeline.add_argument("--max-entries", type=int, default=30, help="Max timeline entries")
    p_timeline.set_defaults(func=cmd_timeline)

    p_prometheus = sub.add_parser("prometheus", help="Show Prometheus health evaluation")
    p_prometheus.add_argument("identity", help="Identity ID")
    p_prometheus.set_defaults(func=cmd_prometheus)

    p_regressions = sub.add_parser("regressions", help="Detect regression signals")
    p_regressions.add_argument("identity", help="Identity ID")
    p_regressions.add_argument("--threshold", type=int, default=3, help="Consecutive decreases threshold")
    p_regressions.set_defaults(func=cmd_regressions)

    p_forecast = sub.add_parser("forecast", help="Generate forecast timeline")
    p_forecast.add_argument("identity", help="Identity ID")
    p_forecast.add_argument("--weeks", type=int, default=8, help="Number of weeks to forecast")
    p_forecast.set_defaults(func=cmd_forecast)

    p_atlas = sub.add_parser("atlas", help="Full Atlas strategic analysis (health + predictions + recs + ranking + strategies)")
    p_atlas.add_argument("identity", help="Identity ID")
    p_atlas.add_argument("-o", "--output", help="Write report to file")
    p_atlas.set_defaults(func=cmd_forecast)

    p_endurance = sub.add_parser("endurance", help="Record or report durable long-running health")
    p_endurance.add_argument("action", choices=["record", "report"])
    p_endurance.add_argument("identity", help="Identity ID")
    p_endurance.add_argument("--identity-store", default=".identity_store", help="Identity persistence directory")
    p_endurance.add_argument("-o", "--output", help="Write Markdown report to file")
    p_endurance.set_defaults(func=cmd_endurance)

    p_integrity = sub.add_parser(
        "integrity",
        help="Generate and enforce independently scored paired benchmark gates",
    )
    integrity_sub = p_integrity.add_subparsers(dest="integrity_command", required=True)

    p_integrity_plan = integrity_sub.add_parser(
        "plan", help="Commit post-SHA randomized trial seeds before execution"
    )
    p_integrity_plan.add_argument("--base-sha", required=True)
    p_integrity_plan.add_argument("--candidate-sha", required=True)
    p_integrity_plan.add_argument("--window-id", required=True)
    p_integrity_plan.add_argument("--beacon", required=True)
    p_integrity_plan.add_argument("--evaluator-digest")
    p_integrity_plan.add_argument("--trials", type=int, default=3)
    p_integrity_plan.add_argument("--commitments", required=True)
    p_integrity_plan.add_argument("--reveal", required=True)
    p_integrity_plan.set_defaults(func=cmd_integrity_plan)

    p_integrity_trial = integrity_sub.add_parser(
        "trial", help="Verify and expose one committed trial to a runner"
    )
    p_integrity_trial.add_argument("--commitments", required=True)
    p_integrity_trial.add_argument("--reveal", required=True)
    p_integrity_trial.add_argument("--trial-index", type=int, required=True)
    p_integrity_trial.add_argument("--github-output")
    p_integrity_trial.set_defaults(func=cmd_integrity_trial)

    p_integrity_gate = integrity_sub.add_parser(
        "gate", help="Independently rescore paired evidence and emit a promotion decision"
    )
    p_integrity_gate.add_argument("--commitments", required=True)
    p_integrity_gate.add_argument("--reveal", required=True)
    p_integrity_gate.add_argument("--pairs-dir", required=True)
    p_integrity_gate.add_argument("--output", required=True)
    p_integrity_gate.add_argument("--summary")
    p_integrity_gate.add_argument("--ledger")
    p_integrity_gate.add_argument("--diff-scan")
    p_integrity_gate.add_argument("--protected", action="store_true")
    p_integrity_gate.add_argument("--evidence-attestations-verified", action="store_true")
    p_integrity_gate.add_argument("--provider-receipts-verified", action="store_true")
    p_integrity_gate.add_argument(
        "--baseline-reset-approved",
        action="store_true",
        help="Allow a reviewed evaluator/schema change to establish a new champion chain",
    )
    p_integrity_gate.add_argument("--enforce", action="store_true")
    p_integrity_gate.add_argument("--minimum-delta", type=float, default=3.0)
    p_integrity_gate.add_argument("--max-world-regression", type=float, default=5.0)
    p_integrity_gate.add_argument("--max-latency-regression-pct", type=float, default=10.0)
    p_integrity_gate.add_argument("--max-prompt-growth-pct", type=float, default=10.0)
    p_integrity_gate.set_defaults(func=cmd_integrity_gate)

    p_integrity_verify = integrity_sub.add_parser(
        "verify-ledger", help="Verify the append-only integrity ledger hash chain"
    )
    p_integrity_verify.add_argument("--ledger", required=True)
    p_integrity_verify.set_defaults(func=cmd_integrity_verify_ledger)

    p_integrity_scan = integrity_sub.add_parser(
        "scan", help="Reject benchmark-aware production branches in a candidate diff"
    )
    p_integrity_scan.add_argument("--diff-file", required=True)
    p_integrity_scan.add_argument("--output", required=True)
    p_integrity_scan.add_argument("--enforce", action="store_true")
    p_integrity_scan.set_defaults(func=cmd_integrity_scan)

    return parser


def main(argv: Optional[List[str]] = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
