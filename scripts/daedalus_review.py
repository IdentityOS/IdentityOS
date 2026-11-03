#!/usr/bin/env python3
"""
Daedalus PR Review -- automated architectural review for IdentityOS pull requests.

Daedalus analyzes each PR against IdentityOS architectural principles:
- Layer separation (Runtime / Prometheus / IdentityBench / Atlas)
- Separation of concerns between packages
- Test coverage for changes
- Benchmark regression potential
- Code quality and patterns
- Documentation completeness
- Alignment with long-term goals

Usage:
    python scripts/daedalus_review.py \
        --diff /tmp/pr.diff \
        --repo owner/repo \
        --pr-number 42 \
        --head-ref feat/my-branch \
        --base-ref main \
        --title "feat: my change" \
        --output /tmp/review.md \
        --token ghp_xxx
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


ARCHITECTURE_LAYERS = {
    "runtime": "runtime/",
    "prometheus": "core/prometheus/",
    "identitybench": "identitybench/",
    "atlas": "identitybench/atlas/",
    "core": "core/",
    "cli": "cli/",
    "registry": "registry/",
    "spec": "spec/",
}

LAYER_PURPOSE = {
    "runtime/": "execution layer",
    "core/prometheus/": "autonomous evolution layer",
    "identitybench/": "measurement & explanation layer",
    "identitybench/atlas/": "strategic decision layer",
    "core/": "core framework",
    "cli/": "command-line interface",
    "registry/": "capability registry",
}

IMPORT_RESTRICTIONS: List[Tuple[str, str, str]] = [
    ("identitybench/atlas/", "runtime/", "Atlas must not import Runtime internals"),
    ("identitybench/atlas/", "core/prometheus/", "Atlas must not import Prometheus internals"),
    ("identitybench/", "runtime/", "IdentityBench should not import Runtime internals directly"),
    ("core/prometheus/", "identitybench/atlas/", "Prometheus should not depend on Atlas"),
]

SEPARATION_CHECK_PATTERNS = [
    (r"from\s+runtime\.", "identitybench/", "IdentityBench imports Runtime module"),
    (r"from\s+identitybench\.atlas", "core/", "Core imports from Atlas (reverse dependency)"),
    (r"from\s+core\.prometheus", "identitybench/", "IdentityBench imports Prometheus directly"),
]

CRITICAL_DIRECTORIES = [
    "core/identity.py",
    "runtime/orchestrator.py",
    "core/prometheus/engine.py",
    "identitybench/engine.py",
    "identitybench/atlas/health.py",
]

CATEGORY_MAP = {
    "feat": "feature",
    "fix": "bugfix",
    "refactor": "refactor",
    "docs": "documentation",
    "test": "testing",
    "chore": "maintenance",
    "ci": "ci/cd",
    "revert": "revert",
}

DAEDALUS_SIGNATURE = "-- Daedalus\nLace's Engineering Partner\nIdentityOS"


def load_daedalus_config() -> Dict[str, Any]:
    config: Dict[str, Any] = {}
    identity_path = Path(".daedalus/identity.json")
    goals_path = Path(".daedalus/goals.json")
    if identity_path.exists():
        try:
            config["identity"] = json.loads(identity_path.read_text())
        except Exception:
            config["identity"] = {}
    if goals_path.exists():
        try:
            config["goals"] = json.loads(goals_path.read_text())
        except Exception:
            config["goals"] = {"primary_goals": []}
    return config


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Daedalus PR Review")
    parser.add_argument("--diff", required=True, help="Path to PR diff file")
    parser.add_argument("--repo", required=True, help="Repository (owner/name)")
    parser.add_argument("--pr-number", type=int, required=True, help="PR number")
    parser.add_argument("--head-ref", required=True, help="Head branch ref")
    parser.add_argument("--base-ref", required=True, help="Base branch ref")
    parser.add_argument("--title", required=True, help="PR title")
    parser.add_argument("--output", required=True, help="Output markdown file")
    parser.add_argument("--token", default="", help="GitHub token")
    parser.add_argument("--test-results", default="", help="Path to test results file for AI interpretation")
    parser.add_argument("--benchmark-results", default="", help="Path to benchmark results file for AI interpretation")
    parser.add_argument("--expected-files", type=int, help="Authoritative changed-file count from GitHub")
    parser.add_argument("--expected-additions", type=int, help="Authoritative addition count from GitHub")
    parser.add_argument("--expected-deletions", type=int, help="Authoritative deletion count from GitHub")
    parser.add_argument("--base-sha", default="", help="Exact pull request base commit")
    parser.add_argument("--head-sha", default="", help="Exact pull request head commit")
    return parser.parse_args()


def read_diff(path: str) -> str:
    with open(path) as f:
        return f.read()


def parse_diff_files(diff: str) -> List[Dict[str, Any]]:
    files: List[Dict[str, Any]] = []
    current_file: Dict[str, Any] = {}
    for line in diff.split("\n"):
        if line.startswith("+++ b/"):
            current_file["path"] = line[6:]
        elif line.startswith("diff --git"):
            if current_file:
                files.append(current_file)
            path = ""
            try:
                tokens = shlex.split(line)
                if len(tokens) >= 4 and tokens[3].startswith("b/"):
                    path = tokens[3][2:]
            except ValueError:
                pass
            current_file = {"path": path, "additions": 0, "deletions": 0, "lines": []}
        elif current_file:
            if line.startswith("+") and not line.startswith("+++"):
                current_file["additions"] += 1
                current_file["lines"].append(line)
            elif line.startswith("-") and not line.startswith("---"):
                current_file["deletions"] += 1
    if current_file:
        files.append(current_file)
    return files


def build_diff_evidence(
    files: List[Dict[str, Any]],
    *,
    expected_files: Optional[int],
    expected_additions: Optional[int],
    expected_deletions: Optional[int],
    base_sha: str = "",
    head_sha: str = "",
) -> Dict[str, Any]:
    """Compare parsed diff facts with GitHub's independent PR metadata."""
    observed = {
        "files": len(files),
        "additions": sum(item.get("additions", 0) for item in files),
        "deletions": sum(item.get("deletions", 0) for item in files),
    }
    expected = {
        "files": expected_files,
        "additions": expected_additions,
        "deletions": expected_deletions,
    }
    missing = [name for name, value in expected.items() if value is None]
    if not base_sha:
        missing.append("base_sha")
    if not head_sha:
        missing.append("head_sha")
    mismatches = [
        {
            "metric": name,
            "expected": expected[name],
            "observed": observed[name],
        }
        for name in expected
        if expected[name] is not None and expected[name] != observed[name]
    ]
    return {
        "verified": not missing and not mismatches,
        "observed": observed,
        "expected": expected,
        "missing": missing,
        "mismatches": mismatches,
        "base_sha": base_sha,
        "head_sha": head_sha,
        "paths": [item.get("path", "") for item in files],
    }


def diff_evidence_failure_reasons(evidence: Dict[str, Any]) -> List[str]:
    reasons: List[str] = []
    if evidence.get("missing"):
        missing = ", ".join(evidence["missing"])
        reasons.append(
            "- **Review input could not be verified.** Missing authoritative metadata: "
            f"`{missing}`. Required action: rerun the review with GitHub PR totals and exact base/head SHAs."
        )
    for mismatch in evidence.get("mismatches", []):
        reasons.append(
            "- **Diff integrity mismatch.** GitHub reports "
            f"`{mismatch['expected']}` {mismatch['metric']}, but the downloaded diff parsed as "
            f"`{mismatch['observed']}`. Required action: redownload the diff for the exact PR base/head "
            "and rerun Daedalus. No code-quality conclusion is made from this inconsistent input."
        )
    return reasons


def detect_layer(fpath: str) -> Optional[str]:
    for name, prefix in ARCHITECTURE_LAYERS.items():
        if fpath.startswith(prefix):
            return name
    return None


def analyze_separation(files: List[Dict[str, Any]], diff: str) -> List[str]:
    findings: List[str] = []
    for pattern, file_prefix, message in SEPARATION_CHECK_PATTERNS:
        for f in files:
            if f["path"].startswith(file_prefix):
                for line in f["lines"]:
                    stripped = line.lstrip("+").strip()
                    if re.search(pattern, stripped):
                        findings.append(
                            f"- \u26a0\ufe0f **{message}** in `{f['path']}`: `{stripped[:80]}`"
                        )
    for src_prefix, forbidden_prefix, message in IMPORT_RESTRICTIONS:
        if any(f["path"].startswith(src_prefix) for f in files):
            for f in files:
                if f["path"].startswith(src_prefix):
                    restricted_imports = []
                    for line in f["lines"]:
                        stripped = line.lstrip("+").strip()
                        if stripped.startswith("from ") or stripped.startswith("import "):
                            if forbidden_prefix.replace("/", ".") in stripped:
                                restricted_imports.append(stripped)
                    if restricted_imports:
                        findings.append(
                            f"- \u274c **{message}** in `{f['path']}`:\n"
                            + "\n".join(f"  - `{imp}`" for imp in restricted_imports[:3])
                        )
    if not findings:
        findings.append("- \u2705 Layer separation maintained \u2014 no violations detected")
    return findings


def analyze_test_coverage(files: List[Dict[str, Any]]) -> List[str]:
    findings: List[str] = []
    source_files = [
        f for f in files
        if not f["path"].startswith(("tests/", "examples/", "demo/"))
        and f["path"].endswith(".py")
    ]
    test_files = [f for f in files if f["path"].startswith("tests/")]
    if source_files and not test_files:
        changed = ", ".join(f"`{item['path']}`" for item in source_files[:5])
        if len(source_files) > 5:
            changed += f", and {len(source_files) - 5} more"
        findings.append(
            f"- \u2139\ufe0f **{len(source_files)} source files changed but no test files updated** \u2014 "
            f"evidence: {changed}. This is not a blocker by itself. Verify that existing tests exercise "
            "the changed behavior, or add focused tests when coverage is missing."
        )
    elif source_files:
        findings.append(
            f"- \u2139\ufe0f **{len(test_files)} test file(s)** changed alongside **{len(source_files)} source "
            "file(s)**. This is file-level evidence only; passing checks are reported separately."
        )
    for sf in source_files:
        if not _source_has_test_evidence(sf["path"], test_files):
            base = os.path.basename(sf["path"]).replace(".py", "")
            findings.append(
                f"  - `{sf['path']}` \u2014 no direct test evidence found "
                f"(for example `tests/test_{base}.py` or a test importing the module)"
            )
    return findings


def _normalized_path_tokens(path: str) -> set[str]:
    """Return useful singularized tokens for relating source and test paths."""
    tokens = set(re.findall(r"[a-z0-9]+", path.lower().removesuffix(".py")))
    normalized = set(tokens)
    for token in tokens:
        if token.endswith("ies") and len(token) > 4:
            normalized.add(token[:-3] + "y")
        elif token.endswith("s") and len(token) > 3:
            normalized.add(token[:-1])
    return normalized - {"py", "test", "tests", "core", "init"}


def _source_has_test_evidence(
    source_path: str,
    test_files: List[Dict[str, Any]],
) -> bool:
    module = source_path.removesuffix(".py").replace("/", ".").removesuffix(".__init__")
    source_tokens = _normalized_path_tokens(source_path)
    for test_file in test_files:
        added_test_text = "\n".join(test_file.get("lines", [])).lower()
        imports_module = bool(re.search(
            rf"\b(?:from|import)\s+{re.escape(module.lower())}(?:\b|\.)",
            added_test_text,
        ))
        shared_path_tokens = source_tokens & _normalized_path_tokens(test_file["path"])
        if imports_module or shared_path_tokens:
            return True
    return False


def analyze_diff_quality(files: List[Dict[str, Any]], title: str) -> List[str]:
    findings: List[str] = []
    total_additions = sum(f["additions"] for f in files)
    total_deletions = sum(f["deletions"] for f in files)
    if total_additions > 5000:
        findings.append(
            f"- Note: {total_additions} additions, {total_deletions} deletions. "
            "This is large but structure matters more than size."
        )
    else:
        findings.append(f"- **{total_additions} additions / {total_deletions} deletions** -- proportionate change")
    cat = None
    for prefix, category in CATEGORY_MAP.items():
        if title.lower().startswith(prefix):
            cat = category
            break
    if cat:
        findings.append(f"- Categorized as **{cat}** (from title prefix)")
    else:
        findings.append(f"- Could not detect change category from title")
    return findings


def analyze_architectural_impact(files: List[Dict[str, Any]]) -> List[str]:
    findings: List[str] = []
    impacted_layers = set()
    for f in files:
        layer = detect_layer(f["path"])
        if layer:
            impacted_layers.add(layer)
    for f in files:
        for critical in CRITICAL_DIRECTORIES:
            if f["path"] == critical:
                findings.append(
                    f"- \u2139\ufe0f **Critical file modified**: `{f['path']}` \u2014 "
                    "review with additional care because this file is central to IdentityOS architecture"
                )
    if impacted_layers:
        layer_details = []
        for layer in sorted(impacted_layers):
            count = sum(1 for f in files if detect_layer(f["path"]) == layer)
            purpose = LAYER_PURPOSE.get(ARCHITECTURE_LAYERS.get(layer, ""), "unknown")
            layer_details.append(f"  - **{layer}**: {count} file(s) ({purpose})")
        findings.append(f"- \U0001f3d7\ufe0f **{len(impacted_layers)} layer(s) impacted**:\n" + "\n".join(layer_details))
    return findings


def analyze_documentation_impact(files: List[Dict[str, Any]]) -> List[str]:
    findings: List[str] = []
    has_doc_changes = any(f["path"].startswith("docs/") for f in files)
    has_arch_doc = any(f["path"].startswith("docs/architecture/") for f in files)
    has_source = any(
        f["path"].endswith(".py") and not f["path"].startswith("tests/")
        for f in files
    )
    if has_source and not has_doc_changes:
        findings.append(
            "- Source code changed without documentation updates. "
            "Daedalus will generate documentation for the changed modules."
        )
    if has_arch_doc:
        findings.append("- Architecture documentation updated")
    return findings


def analyze_goals_alignment(
    files: List[Dict[str, Any]],
    title: str,
    daedalus_config: Dict[str, Any],
) -> List[str]:
    findings: List[str] = []
    goals = daedalus_config.get("goals", {}).get("primary_goals", [])
    if not goals:
        return []
    all_paths = " ".join(f["path"] for f in files)
    title_lower = title.lower()
    for goal in goals:
        gid = goal.get("id", "")
        gname = goal.get("goal", "")
        priority = goal.get("priority", 0)
        if priority < 7:
            continue
        keywords = gname.lower().split()
        match_count = sum(1 for kw in keywords if kw in all_paths.lower() or kw in title_lower)
        if match_count == 0 and priority >= 8:
            findings.append(
                f"- \U0001f3af Goal **\"{gname[:60]}\"** (priority {priority}) \u2014 "
                f"this PR doesn't appear to address it directly"
            )
    return findings


def analyze_technical_debt_introduced(files: List[Dict[str, Any]]) -> List[str]:
    findings: List[str] = []
    marker_count = 0
    for f in files:
        for line in f["lines"]:
            stripped = line.lstrip("+").strip()
            if _technical_debt_marker(stripped):
                marker_count += 1
    if marker_count > 0:
        findings.append(
            f"- \U0001f4a1 **{marker_count} technical debt marker(s) introduced** \u2014 "
            "consider addressing before merge"
        )
    else:
        findings.append("- \u2705 No new technical debt introduced")
    return findings


def _technical_debt_marker(line: str) -> Optional[str]:
    """Find a standalone debt marker in comment text outside quoted strings."""
    comment = _comment_text(line)
    if comment is None:
        return None
    marker = re.search(
        r"\b(TODO|FIXME|HACK|XXX|TEMP|TEMPORARY|WORKAROUND)\b",
        comment,
        re.IGNORECASE,
    )
    return marker.group(1).upper() if marker else None


def _comment_text(line: str) -> Optional[str]:
    quote = ""
    escaped = False
    for index, char in enumerate(line):
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            continue
        if char in {"'", '"', "`"}:
            quote = char
            continue
        for delimiter in ("#", "//", "/*", "--"):
            if line.startswith(delimiter, index):
                return line[index + len(delimiter):]
    return None


def assess_readiness(findings: Dict[str, List[str]]) -> Tuple[str, List[str]]:
    issues = []
    warnings = []
    for section, items in findings.items():
        for item in items:
            if "\u274c" in item:
                issues.append(item)
            elif "\u26a0\ufe0f" in item:
                warnings.append(item)
    if issues:
        return "NOT_READY", issues + warnings
    elif warnings:
        return "NEEDS_WORK", warnings
    else:
        return "READY", []


def assess_check_evidence(check_evidence: str) -> Tuple[str, List[str]]:
    """Turn observed failing check conclusions into deterministic blockers."""
    failure_states = {"failure", "cancelled", "timed_out", "action_required", "startup_failure"}
    blockers: List[str] = []
    for line in check_evidence.splitlines():
        name, separator, conclusion = line.rpartition(":")
        if not separator or conclusion.strip().lower() not in failure_states:
            continue
        blockers.append(
            f"- \u274c **Check `{name.strip()}` reported `{conclusion.strip()}`.** "
            "This is observed GitHub check evidence. Required change: inspect and correct the failing "
            "job or implementation, then rerun it to a successful conclusion."
        )
    return ("NOT_READY", blockers) if blockers else ("READY", [])


def reconcile_readiness(
    static_readiness: Tuple[str, List[str]],
    ai_analysis: Optional[Dict[str, Any]],
    *,
    changed_paths: Optional[List[str]] = None,
    check_evidence: str = "",
) -> Tuple[str, List[str]]:
    """Accept AI downgrades only when each blocker is tied to supplied evidence."""
    severity = {"READY": 0, "NEEDS_WORK": 1, "NOT_READY": 2}
    ai_verdict = ai_analysis.get("verdict") if isinstance(ai_analysis, dict) else None
    if ai_verdict not in severity:
        return static_readiness
    static_status, reasons = static_readiness
    if severity[ai_verdict] <= severity.get(static_status, 0):
        return static_readiness
    blockers = validate_ai_blockers(
        ai_analysis.get("blockers", []),
        changed_paths=changed_paths or [],
        check_evidence=check_evidence,
    )
    if not blockers:
        return static_readiness
    rendered = reasons + [render_blocker(blocker) for blocker in blockers]
    strongest = "NOT_READY" if any(item["severity"] == "blocking" for item in blockers) else "NEEDS_WORK"
    return strongest, rendered


def validate_ai_blockers(
    blockers: Any,
    *,
    changed_paths: List[str],
    check_evidence: str,
) -> List[Dict[str, str]]:
    """Reject vague or ungrounded model blockers before they affect readiness."""
    required = {
        "title",
        "severity",
        "evidence_source",
        "evidence_location",
        "evidence",
        "impact",
        "required_change",
        "verification",
    }
    valid: List[Dict[str, str]] = []
    for candidate in blockers if isinstance(blockers, list) else []:
        if not isinstance(candidate, dict) or not required.issubset(candidate):
            continue
        normalized = {key: str(candidate.get(key, "")).strip() for key in required}
        if not all(normalized.values()) or normalized["severity"] not in {"blocking", "warning"}:
            continue
        source = normalized["evidence_source"]
        location = normalized["evidence_location"]
        if source == "diff":
            if not any(location == path or location.startswith(f"{path}:") for path in changed_paths):
                continue
        elif source == "check":
            if not _check_reports_failure(location, check_evidence):
                continue
        else:
            continue
        valid.append(normalized)
    return valid


def _check_reports_failure(check_name: str, check_evidence: str) -> bool:
    failure_states = {"failure", "cancelled", "timed_out", "action_required", "startup_failure"}
    for line in check_evidence.splitlines():
        name, separator, conclusion = line.rpartition(":")
        if separator and name.strip().lower() == check_name.lower():
            return conclusion.strip().lower() in failure_states
    return False


def render_blocker(blocker: Dict[str, str]) -> str:
    evidence_label = (
        "Observed check evidence"
        if blocker["evidence_source"] == "check"
        else "Model-derived finding at a runtime-verified changed path"
    )
    return (
        f"- **{blocker['title']}**\n"
        f"  - {evidence_label}: `{blocker['evidence_location']}` \u2014 {blocker['evidence']}\n"
        f"  - Why it matters: {blocker['impact']}\n"
        f"  - Required change: {blocker['required_change']}\n"
        f"  - How to verify: {blocker['verification']}"
    )


def generate_markdown_review(
    title: str,
    pr_number: int,
    head_ref: str,
    base_ref: str,
    findings: Dict[str, List[str]],
    readiness: Tuple[str, List[str]],
    daedalus_config: Dict[str, Any],
    evidence: Optional[Dict[str, Any]] = None,
    check_evidence: str = "",
) -> str:
    status = readiness[0]
    status_emoji = {
        "READY": "\u2705",
        "NEEDS_WORK": "\u26a0\ufe0f",
        "NOT_READY": "\u274c",
        "INCONCLUSIVE": "\u2753",
    }
    identity = daedalus_config.get("identity", {})
    personality = identity.get("personality", {})
    style = personality.get("style", "evidence-driven")
    lines = [
        f"# \U0001f3db\ufe0f Daedalus Architectural Review",
        f"**PR:** {title} (#{pr_number})",
        f"**Branch:** `{head_ref}` \u2192 `{base_ref}`",
        f"**Reviewer:** Daedalus \u2014 {identity.get('role', 'Engineering Partner')} for {identity.get('project', 'IdentityOS')}",
        f"**Style:** {style}, {personality.get('obsessed_with_architectural_simplicity', False) and 'architecturally rigorous' or 'analytical'}",
        "",
        f"## Merge Readiness: {status_emoji.get(status, chr(10067))} {status.replace('_', ' ')}",
        "",
    ]
    decision_text = {
        "READY": "No evidence-backed merge blocker was found. Required repository checks and reviews still apply.",
        "NEEDS_WORK": "Not ready to merge until the evidence-backed items below are resolved.",
        "NOT_READY": "Do not merge until the blocking evidence below is resolved and re-verified.",
        "INCONCLUSIVE": "Daedalus could not safely determine readiness because its review input was not verified. This is an automation failure, not a claim that the PR code is defective.",
    }
    lines.append(decision_text.get(status, "Readiness could not be determined."))
    lines.append("")
    if readiness[1]:
        lines.append("### What prevents merge")
        lines.append("")
        for item in readiness[1][:12]:
            lines.append(item)
        lines.append("")

    section_order = [
        ("Layer Separation", "separation"),
        ("Architectural Impact", "architecture"),
        ("Goal Alignment", "goals"),
        ("Technical Debt", "tech_debt"),
        ("Diff Quality", "diff_quality"),
        ("Test Coverage", "test_coverage"),
        ("Documentation", "documentation"),
    ]
    for section_label, section_key in section_order:
        items = findings.get(section_key, [])
        if items:
            lines.append(f"## {section_label}")
            lines.append("")
            lines.extend(items)
            lines.append("")

    if evidence:
        observed = evidence.get("observed", {})
        expected = evidence.get("expected", {})
        lines.extend([
            "## Verified Review Inputs",
            "",
            f"- Diff integrity: **{'verified' if evidence.get('verified') else 'not verified'}**",
            f"- Parsed diff: **{observed.get('files', 0)} files, +{observed.get('additions', 0)} / -{observed.get('deletions', 0)}**",
            f"- GitHub metadata: **{expected.get('files', 'unknown')} files, +{expected.get('additions', 'unknown')} / -{expected.get('deletions', 'unknown')}**",
            f"- Exact revision: `{evidence.get('base_sha') or 'unknown'}` → `{evidence.get('head_sha') or 'unknown'}`",
            "- Changed files: " + (", ".join(f"`{path}`" for path in evidence.get("paths", [])[:30]) or "none"),
            "",
            "### Independent check evidence",
            "",
            "```text",
            check_evidence.strip() or "No independent check results were supplied to Daedalus.",
            "```",
            "",
        ])

    lines.extend([
        "## Methodology",
        "",
        "Daedalus reviewed this PR by:",
        f"- Analyzing the diff against {len(ARCHITECTURE_LAYERS)} architectural layers",
        "- Checking import patterns for separation of concerns",
        "- Verifying layer boundaries (Runtime \u2192 Prometheus \u2192 IdentityBench \u2192 Atlas)",
        "- Identifying impacted critical modules",
        "- Checking test coverage for source code changes",
        "- Evaluating alignment with primary goals",
        "- Detecting introduced technical debt",
        "",
        "---",
        "",
        DAEDALUS_SIGNATURE,
    ])
    return "\n".join(lines)


def generate_documentation(findings: Dict[str, List[str]], args: argparse.Namespace) -> str:
    needs_docs = any("Source code changed without documentation" in r for r in findings.get("documentation", []))
    if not needs_docs:
        return ""
    try:
        from core.capabilities.daedalus.thinking_engine import ThinkingEngine
    except ImportError:
        return ""
    diff_text = ""
    if args.diff and os.path.exists(args.diff):
        diff_text = open(args.diff).read()[:3000]
    engine = ThinkingEngine()
    thought = engine.think(
        system_prompt="""
You are Daedalus, Engineering Partner for IdentityOS. Generate markdown documentation
for the changed modules in this diff. Include: purpose, public API, dependencies,
architectural impact. Output ONLY the markdown documentation. No em dashes.
""",
        user_prompt=f"Generate docs for this diff:\n```\n{diff_text}\n```",
        max_tokens=2048,
        temperature=0.3,
    )
    if thought.content and thought.finish_reason != "error" and not thought.content.startswith("{"):
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        doc_path = f"docs/architecture/pr-{args.pr_number}-changes.md"
        Path(doc_path).write_text(f"# PR #{args.pr_number} Changes\n\n_Generated by Daedalus on {ts}_\n\n{thought.content}")
        return f"\n- Documentation generated: `{doc_path}`"
    return ""


def create_proactive_issue(args: argparse.Namespace, findings: Dict[str, List[str]]) -> None:
    issues = []
    if any("Source code changed without documentation" in r for r in findings.get("documentation", [])):
        issues.append("Documentation gap: source changed without docs update")
    for finding in findings.get("tech_debt", []):
        if "marker" in finding.lower() and "no new" not in finding.lower():
            issues.append(finding.strip("- "))
    for finding in findings.get("separation", []):
        if "violation" in finding.lower():
            issues.append(finding.strip("- "))
    if not issues:
        return
    try:
        from core.capabilities.daedalus.thinking_engine import load_memory, save_memory, ThinkingEngine
    except ImportError:
        return
    memory = load_memory()
    engine = ThinkingEngine()
    for issue in issues[:2]:
        thought = engine.think(
            system_prompt="You are Daedalus. Generate a concise GitHub issue body for this problem. No em dashes.",
            user_prompt=f"Problem: {issue}\nPR: #{args.pr_number} - {args.title}\nBranch: {args.head_ref}",
            max_tokens=512,
            temperature=0.3,
        )
        if thought.content and thought.finish_reason != "error" and not thought.content.startswith("{"):
            memory.setdefault("pending_issues", []).append({
                "title": f"[Daedalus] {issue[:60]}",
                "body": thought.content[:500],
                "source_pr": args.pr_number,
                "created": datetime.now(timezone.utc).isoformat(),
            })
    save_memory(memory)
    recorded = len(memory.get("pending_issues", []))
    print(f"Daedalus queued {recorded} issue(s) for proactive follow-up")


def generate_llm_review_section(
    args: argparse.Namespace,
    findings: Dict[str, List[str]],
    readiness: Tuple[str, List[str]],
    evidence: Dict[str, Any],
) -> Tuple[str, Optional[Dict[str, Any]]]:
    try:
        from core.capabilities.daedalus.thinking_engine import ThinkingEngine, record_recommendation, summarize_recommendation_follow_through
    except ImportError:
        return "", None

    memory_context = summarize_recommendation_follow_through()
    diff_text = read_diff(args.diff)
    diff_preview = "\n".join(diff_text.splitlines()[:100])
    if len(diff_text.splitlines()) > 100:
        diff_preview += f"\n... ({len(diff_text.splitlines()) - 100} more lines)"

    test_results = ""
    bench_results = ""
    if args.test_results and os.path.exists(args.test_results):
        with open(args.test_results) as f:
            test_results = f.read()[:2000]
        test_results = f"\n### Test Results\n```\n{test_results}\n```\n"
    if args.benchmark_results and os.path.exists(args.benchmark_results):
        with open(args.benchmark_results) as f:
            bench_results = f.read()[:2000]
        bench_results = f"\n### Benchmark Results\n```\n{bench_results}\n```\n"

    user_prompt = f"""## PR: {args.title} (#{args.pr_number})
**Branch:** {args.head_ref} -> {args.base_ref}

### Authoritative Review Scope
- Base SHA: {evidence.get('base_sha')}
- Head SHA: {evidence.get('head_sha')}
- Files: {evidence.get('observed', {}).get('files')}
- Additions: {evidence.get('observed', {}).get('additions')}
- Deletions: {evidence.get('observed', {}).get('deletions')}
- Changed paths: {json.dumps(evidence.get('paths', []))}
- Paths with patch content in the model preview: {json.dumps(evidence.get('model_diff_paths', []))}

### Static Analysis Results

**Layer Separation:**
{' '.join(findings.get('separation', ['No violations detected']))}

**Architectural Impact:**
{' '.join(findings.get('architecture', ['No significant impact']))}

**Test Coverage:**
{' '.join(findings.get('test_coverage', ['No tests changed']))}

**Technical Debt:**
{' '.join(findings.get('tech_debt', ['None detected']))}

**Diff Quality:**
{' '.join(findings.get('diff_quality', ['Small, focused change']))}

**Documentation:**
{' '.join(findings.get('documentation', ['No docs impact']))}

**Goal Alignment:**
{' '.join(findings.get('goals', ['N/A']))}

### Diff Preview (first 100 lines)
```
{diff_preview}
```
{test_results}
{bench_results}
Analyze this PR and respond in the specified JSON format."""

    engine = ThinkingEngine()
    from core.capabilities.daedalus.thinking_engine import ARCHITECTURAL_REVIEW_SYSTEM_PROMPT
    thought = engine.think(
        system_prompt=ARCHITECTURAL_REVIEW_SYSTEM_PROMPT.format(memory_context=memory_context),
        user_prompt=user_prompt,
        max_tokens=2048,
        temperature=0.3,
    )

    ai_analysis: Optional[Dict[str, Any]] = None

    if thought.content and thought.finish_reason != "error":
        try:
            parsed = json.loads(thought.content)
            candidate = parsed.get("verdict")
            if candidate in {"READY", "NEEDS_WORK", "NOT_READY"}:
                ai_analysis = parsed
            recs = parsed.get("recommendations", [])
            for r in recs[:3]:
                recommendation = r if isinstance(r, str) else str(r)
                record_recommendation(
                    recommendation=recommendation,
                    context=f"PR #{args.pr_number}: {args.title}",
                    pr_number=args.pr_number,
                    severity="warning" if "must" in recommendation.lower() or "critical" in recommendation.lower() else "info",
                )
        except (json.JSONDecodeError, Exception):
            pass

        lines = [
            "",
            "## Daedalus AI Analysis",
            "",
            f"({thought.provider}/{thought.model})",
            "",
        ]
        if ai_analysis:
            lines.append("**Model interpretation (not execution evidence):** " + str(
                ai_analysis.get("summary", "No summary supplied.")
            ))
            recommendations = ai_analysis.get("recommendations", [])
            if recommendations:
                lines.extend(["", "### Advisory model suggestions (not merge blockers)", ""])
                lines.extend(f"- {item}" for item in recommendations[:5])
        else:
            lines.append("Model analysis was not used because it did not return valid structured JSON.")
        lines.append("")
        return "\n".join(lines), ai_analysis
    elif thought.content:
        return (
            f"\n\n## Daedalus AI Analysis\n\n"
            f"Analysis unavailable: {thought.content[:200]}\n",
            None,
        )
    return "", None


def main() -> None:
    args = parse_args()
    daedalus_config = load_daedalus_config()
    diff_text = read_diff(args.diff)
    files = parse_diff_files(diff_text)
    evidence = build_diff_evidence(
        files,
        expected_files=args.expected_files,
        expected_additions=args.expected_additions,
        expected_deletions=args.expected_deletions,
        base_sha=args.base_sha,
        head_sha=args.head_sha,
    )
    diff_preview = "\n".join(diff_text.splitlines()[:100])
    evidence["model_diff_paths"] = [
        item["path"] for item in parse_diff_files(diff_preview) if item.get("path")
    ]
    check_evidence = ""
    if args.test_results and os.path.exists(args.test_results):
        check_evidence = Path(args.test_results).read_text()[:4000]
    findings: Dict[str, List[str]] = {
        "separation": [],
        "architecture": [],
        "diff_quality": [],
        "test_coverage": [],
        "documentation": [],
        "goals": [],
        "tech_debt": [],
    }
    llm_section = ""
    if evidence["verified"]:
        findings["separation"] = analyze_separation(files, diff_text)
        findings["architecture"] = analyze_architectural_impact(files)
        findings["diff_quality"] = analyze_diff_quality(files, args.title)
        findings["test_coverage"] = analyze_test_coverage(files)
        findings["documentation"] = analyze_documentation_impact(files)
        findings["goals"] = analyze_goals_alignment(files, args.title, daedalus_config)
        findings["tech_debt"] = analyze_technical_debt_introduced(files)
        readiness = assess_readiness(findings)
        check_readiness = assess_check_evidence(check_evidence)
        if check_readiness[0] == "NOT_READY":
            readiness = (
                "NOT_READY",
                readiness[1] + check_readiness[1],
            )
        llm_section, ai_analysis = generate_llm_review_section(
            args,
            findings,
            readiness,
            evidence,
        )
        readiness = reconcile_readiness(
            readiness,
            ai_analysis,
            changed_paths=evidence["model_diff_paths"],
            check_evidence=check_evidence,
        )
    else:
        readiness = ("INCONCLUSIVE", diff_evidence_failure_reasons(evidence))
    try:
        from core.capabilities.daedalus.thinking_engine import record_pr_review
        record_pr_review(
            args.pr_number,
            readiness[0],
            "Runtime-validated final verdict based on verified diff/check evidence."
            if evidence["verified"]
            else "Review input integrity failed; no code-quality verdict was recorded.",
        )
    except ImportError:
        pass
    markdown = generate_markdown_review(
        args.title, args.pr_number, args.head_ref, args.base_ref,
        findings, readiness, daedalus_config, evidence, check_evidence,
    )
    if llm_section:
        markdown += llm_section
    with open(args.output, "w") as f:
        f.write(markdown)
    status = readiness[0]
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a") as f:
            f.write(f"status={status}\n")
    print(f"Daedalus review written to {args.output} - status: {status}")

    if evidence["verified"]:
        create_proactive_issue(args, findings)

if __name__ == "__main__":
    main()
