#!/usr/bin/env python3
"""Autonomous ratchet loop: LLM proposes runtime fixes → ratchet judges.

Unattended overnight (until plateau):
  nohup python benchmarks/autopilot.py --loop --until-plateau --provider deepseek \\
    >> /tmp/autopilot.log 2>&1 &

Stops when:
  - N consecutive REVERTs (default 4), or
  - success rate reaches target (default 85%), or
  - --max-iters reached (ignored when --until-plateau)

Coder order: AUTOPILOT_CODER_ORDER or --provider (gemini|groq|deepseek).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / "benchmarks" / ".env")
    load_dotenv(ROOT / ".env", override=False)
except ImportError:
    pass

from benchmarks.autopilot_context import (  # noqa: E402
    build_coder_prompt,
    load_results,
    parse_recent_experiments,
)
from benchmarks.coder_llm import CoderError, propose_edits  # noqa: E402
from benchmarks.invariants import (  # noqa: E402
    ALLOWED_PREFIXES,
    ROOT as REPO_ROOT,
    classify_paths,
)
from benchmarks.plateau import should_stop  # noqa: E402
from benchmarks.ratchet import next_exp_id  # noqa: E402

PROPOSALS_DIR = ROOT / "benchmarks" / "experiments" / "proposals"
DEFAULT_PYTEST = [
    "tests/test_user_profile_remember.py",
    "tests/test_ratchet.py",
    "tests/test_smollm_benchmark.py",
]
RUNTIME_PREFIXES = ("adapters/", "core/", "runtime/", "tests/")


def _is_allowed_path(rel: str) -> bool:
    return bool(classify_paths([rel])["allowed"])


def _git(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=ROOT, text=True, capture_output=True)


def dirty_allowlisted_runtime() -> list[str]:
    """Tracked allowlisted runtime files that differ from HEAD (experiment leftovers)."""
    named = (_git(["diff", "--name-only", "HEAD"]).stdout or "").splitlines()
    out: list[str] = []
    for rel in named:
        if not rel:
            continue
        if not any(rel == p or rel.startswith(p) for p in RUNTIME_PREFIXES):
            continue
        if _is_allowed_path(rel):
            out.append(rel)
    return sorted(set(out))


def reset_runtime_to_head() -> list[str]:
    """Restore dirty runtime experiment files so each loop starts clean."""
    dirty = dirty_allowlisted_runtime()
    if not dirty:
        return []
    _git(["restore", "--worktree", "--staged", "--", *dirty])
    return dirty


def apply_edits(edits: list[dict[str, Any]]) -> list[str]:
    touched: list[str] = []
    for edit in edits:
        rel = str(edit.get("path") or "").strip()
        if not rel or not _is_allowed_path(rel):
            raise CoderError(f"edit path not allowlisted: {rel!r}")
        path = REPO_ROOT / rel
        if not path.exists():
            raise CoderError(f"edit target missing: {rel}")
        old, new = edit.get("search"), edit.get("replace")
        if not isinstance(old, str) or not isinstance(new, str):
            raise CoderError(f"edit for {rel} needs string search/replace")
        text = path.read_text(encoding="utf-8")
        count = text.count(old)
        if count != 1:
            raise CoderError(f"edit for {rel}: search matched {count} times (need exactly 1)")
    for edit in edits:
        rel = str(edit["path"]).strip()
        path = REPO_ROOT / rel
        path.write_text(
            path.read_text(encoding="utf-8").replace(edit["search"], edit["replace"], 1),
            encoding="utf-8",
        )
        touched.append(rel)
    return touched


def run_pytest(targets: list[str]) -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", *targets, "-q"],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    if proc.returncode != 0:
        raise CoderError(proc.stdout + proc.stderr or "pytest failed")


def run_ratchet(*, exp_id: str, hypothesis: str, change: str, pytest_targets: list[str]) -> int:
    cmd = [
        sys.executable,
        str(ROOT / "benchmarks" / "ratchet.py"),
        "--exp",
        exp_id,
        "--hypothesis",
        hypothesis,
        "--change",
        change,
    ]
    for target in pytest_targets:
        cmd.extend(["--pytest-target", target])
    return subprocess.run(cmd, cwd=ROOT, text=True).returncode


def save_proposal(exp_id: str, proposal: dict[str, Any]) -> Path:
    PROPOSALS_DIR.mkdir(parents=True, exist_ok=True)
    path = PROPOSALS_DIR / f"{exp_id}.json"
    path.write_text(json.dumps(proposal, indent=2), encoding="utf-8")
    return path


def latest_proposal_path() -> Path | None:
    if not PROPOSALS_DIR.exists():
        return None
    paths = sorted(PROPOSALS_DIR.glob("EXP-*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    return paths[0] if paths else None


def plan(
    *,
    model: str | None = None,
    provider: str | None = None,
    last_failure: str | None = None,
) -> dict[str, Any]:
    prompt = build_coder_prompt(
        results=load_results(),
        recent_experiments=parse_recent_experiments(),
        compact=True,
        last_failure=last_failure,
    )
    return propose_edits(prompt, model=model, provider=provider)


def _iter_count(max_iters: int, until_plateau: bool) -> Iterator[int]:
    if until_plateau or max_iters <= 0:
        n = 0
        while True:
            n += 1
            yield n
    else:
        yield from range(1, max_iters + 1)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Multi-provider ratchet autopilot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Overnight (no intervention until plateau):\n"
            "  nohup python benchmarks/autopilot.py --loop --until-plateau --provider deepseek "
            ">> /tmp/autopilot.log 2>&1 &\n"
            "  tail -f /tmp/autopilot.log\n\n"
            "Providers: gemini | groq | deepseek\n"
            "Models: GEMINI_MODEL, GROQ_CODER_MODEL, DEEPSEEK_CODER_MODEL "
            "(default deepseek-v4-flash)"
        ),
    )
    p.add_argument("--plan-only", action="store_true")
    p.add_argument("--once", action="store_true")
    p.add_argument("--loop", action="store_true", help="Repeat experiments until stop condition")
    p.add_argument(
        "--until-plateau",
        action="store_true",
        help="With --loop: ignore --max-iters; run until consecutive REVERTs or target score",
    )
    p.add_argument(
        "--max-iters",
        type=int,
        default=3,
        help="Max loop iterations (default 3). Use 0 or --until-plateau for unbounded",
    )
    p.add_argument(
        "--max-consecutive-reverts",
        type=int,
        default=4,
        help="Plateau: stop after this many REVERTs in a row (default 4)",
    )
    p.add_argument(
        "--target-success",
        type=float,
        default=0.85,
        help="Plateau: stop when KEEP success rate >= this (default 0.85)",
    )
    p.add_argument("--retry-sleep", type=int, default=45, help="Seconds to sleep after a failed plan/apply")
    p.add_argument("--model", default=None, help="Override model for --provider")
    p.add_argument(
        "--provider",
        choices=("gemini", "groq", "deepseek", "ollama"),
        default=None,
        help="Force one coder (overnight: deepseek)",
    )
    p.add_argument("--from-proposal", type=Path)
    p.add_argument("--reuse-latest", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not (args.plan_only or args.once or args.loop):
        print("Pass --plan-only, --once, or --loop", file=sys.stderr)
        return 2

    if args.until_plateau and not args.loop:
        print("[autopilot] --until-plateau requires --loop", file=sys.stderr)
        return 2

    last_failure: str | None = None
    for iteration in _iter_count(args.max_iters, args.until_plateau and args.loop):
        if args.loop:
            stop, reason = should_stop(
                max_consecutive_reverts=args.max_consecutive_reverts,
                target_success_rate=args.target_success,
            )
            if stop:
                print(f"[autopilot] plateau reached: {reason}")
                return 0

        exp_id = next_exp_id()
        bound = "∞" if (args.until_plateau or args.max_iters <= 0) else str(args.max_iters)
        print(f"[autopilot] iteration {iteration}/{bound} → {exp_id}")

        # Each overnight iteration must start from HEAD so locked/harness dirt
        # and leftover REVERT-adjacent edits do not abort the ratchet.
        if args.loop and not args.from_proposal and not args.reuse_latest:
            restored = reset_runtime_to_head()
            if restored:
                print(f"[autopilot] reset runtime files to HEAD: {', '.join(restored)}")

        proposal: dict[str, Any]
        if args.from_proposal:
            proposal = json.loads(Path(args.from_proposal).read_text(encoding="utf-8"))
            print(f"[autopilot] using proposal file: {args.from_proposal}")
        elif args.reuse_latest:
            latest = latest_proposal_path()
            if latest is None:
                print("[autopilot] no proposals/EXP-*.json found", file=sys.stderr)
                return 1
            proposal = json.loads(latest.read_text(encoding="utf-8"))
            print(f"[autopilot] reusing latest proposal: {latest}")
        else:
            try:
                proposal = plan(
                    model=args.model,
                    provider=args.provider,
                    last_failure=last_failure,
                )
                print(f"[autopilot] coder provider: {proposal.pop('_coder_provider', args.provider or 'auto')}")
                print(f"[autopilot] proposal: {save_proposal(exp_id, proposal)}")
            except CoderError as exc:
                last_failure = str(exc)
                # Never reuse a proposal that just failed apply — that loops forever.
                if args.loop:
                    print(
                        f"[autopilot] coder failed; sleep {args.retry_sleep}s and continue: {exc}",
                        file=sys.stderr,
                    )
                    time.sleep(args.retry_sleep)
                    continue
                print(f"[autopilot] coder failed: {exc}", file=sys.stderr)
                return 1

        if args.plan_only or args.dry_run:
            print(json.dumps(proposal, indent=2))
            return 0

        edits = proposal.get("edits") or []
        if not edits:
            last_failure = "empty edits array"
            print("[autopilot] empty edits", file=sys.stderr)
            if args.loop:
                time.sleep(min(args.retry_sleep, 20))
                continue
            return 2

        try:
            print(f"[autopilot] applied edits: {', '.join(apply_edits(edits))}")
            targets = proposal.get("tests_to_run") or DEFAULT_PYTEST
            run_pytest(targets)
        except CoderError as exc:
            last_failure = str(exc)
            print(f"[autopilot] apply/pytest failed: {exc}", file=sys.stderr)
            reset_runtime_to_head()
            if args.loop:
                time.sleep(min(args.retry_sleep, 20))
                continue
            return 1

        last_failure = None
        rc = run_ratchet(
            exp_id=exp_id,
            hypothesis=str(proposal.get("hypothesis") or ""),
            change=str(proposal.get("change") or ""),
            pytest_targets=proposal.get("tests_to_run") or DEFAULT_PYTEST,
        )
        print(f"[autopilot] {'KEEP' if rc == 0 else f'returned {rc}'} {exp_id}")

        if not args.loop:
            return rc

        # Brief pause so Ollama/API can settle between full-suite runs.
        time.sleep(5)

    print("[autopilot] max-iters reached without plateau")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
