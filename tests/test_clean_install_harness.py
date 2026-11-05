"""Tests for the clean-install evidence harness."""

from __future__ import annotations

import json
import subprocess
import sys
import zipfile
from pathlib import Path

from scripts import verify_clean_install as harness

ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "scripts" / "verify_clean_install.py"


def _minimal_wheel(directory: Path) -> Path:
    """Create a dependency-free wheel so the integration test needs no network."""

    wheel = directory / "cleanprobe-0.1.0-py3-none-any.whl"
    dist_info = "cleanprobe-0.1.0.dist-info"
    files = {
        "cleanprobe/__init__.py": "SENTINEL = 42\n",
        f"{dist_info}/METADATA": (
            "Metadata-Version: 2.1\n"
            "Name: cleanprobe\n"
            "Version: 0.1.0\n"
            "Summary: clean-install harness fixture\n"
        ),
        f"{dist_info}/WHEEL": (
            "Wheel-Version: 1.0\n"
            "Generator: IdentityOS test\n"
            "Root-Is-Purelib: true\n"
            "Tag: py3-none-any\n"
        ),
        f"{dist_info}/RECORD": "",
    }
    with zipfile.ZipFile(wheel, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, body in files.items():
            archive.writestr(name, body)
    return wheel


def test_sanitizer_redacts_exact_values_headers_assignments_and_urls():
    secret = "sk-test-value-123"
    source = (
        f"Authorization: Bearer {secret}\n"
        f"X-API-Key: {secret}\n"
        f"GROQ_API_KEY={secret}\n"
        f"https://example.test/path?token={secret}&safe=yes\n"
        f"bare value {secret}\n"
    )

    sanitized = harness.sanitize_text(source, [secret])

    assert secret not in sanitized
    assert sanitized.count("<redacted>") >= 5
    assert "safe=yes" in sanitized


def test_argv_sanitizer_redacts_sensitive_option_values():
    sanitized = harness.sanitize_argv(
        ["tool", "--api-key", "visible-secret", "--token=another-secret", "safe"]
    )

    assert sanitized == ["tool", "--api-key", "<redacted>", "--token=<redacted>", "safe"]


def test_generated_run_ids_are_unique_and_path_safe():
    first = harness.make_run_id()
    second = harness.make_run_id()

    assert first != second
    assert harness.validate_run_id(first) == first
    assert "/" not in first


def test_clean_wheel_run_records_auditable_evidence(tmp_path):
    wheel = _minimal_wheel(tmp_path)
    evidence = tmp_path / "evidence"
    run_id = "integration-run"
    command = [
        sys.executable,
        str(HARNESS),
        "--wheel",
        str(wheel),
        "--evidence-root",
        str(evidence),
        "--run-id",
        run_id,
        "--publication-status",
        "unpublished",
        "--network-state",
        "offline",
        "--smoke-command",
        "{python} -c \"import cleanprobe; assert cleanprobe.SENTINEL == 42\"",
        "--inference-mode",
        "mocked",
        "--inference-command",
        "{python} -c \"print('deterministic mocked response')\"",
        "--reinstall-check",
        "--expected-failure-command",
        "intentional::{python} -c \"raise SystemExit(7)\"",
        "--recovery-command",
        "after-failure::{python} -c \"print('recovered')\"",
    ]

    completed = subprocess.run(command, capture_output=True, text=True, timeout=120, check=False)

    assert completed.returncode == 0, completed.stderr + completed.stdout
    run_dir = evidence / run_id
    summary = json.loads((run_dir / "run-summary.json").read_text(encoding="utf-8"))
    artifact = json.loads((run_dir / "artifact.json").read_text(encoding="utf-8"))
    inference = json.loads(
        (run_dir / "inference-verification.json").read_text(encoding="utf-8")
    )
    timings = json.loads((run_dir / "timings.json").read_text(encoding="utf-8"))
    command_events = [
        json.loads(line)
        for line in (run_dir / "commands.jsonl").read_text(encoding="utf-8").splitlines()
    ]

    assert summary["success"] is True
    assert summary["unpublished_artifact_test"] is True
    assert artifact["sha256"] == harness.sha256_file(wheel)
    assert inference["mode"] == "mocked"
    assert inference["verified"] is True
    assert inference["real_inference_acceptance"] is False
    assert timings["clock"] == "time.monotonic_ns"
    assert timings["stages"]["installation_ms"] > 0
    assert timings["stages"]["inference_ms"] > 0
    assert {event["label"] for event in command_events} >= {
        "install",
        "reinstall",
        "smoke-1",
        "expected-failure-intentional",
        "recovery-after-failure",
        "inference",
    }
    assert all(event["monotonic_end_ns"] >= event["monotonic_start_ns"] for event in command_events)
    assert all(event["started_at"].endswith("Z") for event in command_events)
    assert "No failures observed" in (run_dir / "failures.md").read_text(encoding="utf-8")


def test_missing_wheel_produces_structured_failure_evidence(tmp_path):
    evidence = tmp_path / "evidence"
    completed = subprocess.run(
        [
            sys.executable,
            str(HARNESS),
            "--wheel",
            str(tmp_path / "missing.whl"),
            "--evidence-root",
            str(evidence),
            "--run-id",
            "missing-artifact",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 1
    run_dir = evidence / "missing-artifact"
    summary = json.loads((run_dir / "run-summary.json").read_text(encoding="utf-8"))
    assert summary["success"] is False
    assert summary["failure_count"] == 1
    assert "wheel not found" in (run_dir / "failures.md").read_text(encoding="utf-8")


def test_successful_command_is_not_real_inference_without_runtime_provenance(tmp_path):
    wheel = _minimal_wheel(tmp_path)
    evidence = tmp_path / "evidence"
    completed = subprocess.run(
        [
            sys.executable,
            str(HARNESS),
            "--wheel",
            str(wheel),
            "--evidence-root",
            str(evidence),
            "--run-id",
            "false-real-claim",
            "--inference-mode",
            "real",
            "--provider",
            "claimed-provider",
            "--inference-command",
            "{python} -c \"print('model-looking text without provenance')\"",
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert completed.returncode == 1
    result = json.loads(
        (evidence / "false-real-claim" / "inference-verification.json").read_text()
    )
    assert result["verified"] is False
    assert result["real_inference_acceptance"] is False
    assert "JSON evidence object" in result["blocked_reason"]
