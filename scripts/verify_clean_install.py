#!/usr/bin/env python3
"""Install an IdentityOS wheel in a fresh venv and capture auditable evidence.

This harness deliberately treats installation, smoke checks, and inference as
separate claims.  It can exercise an unpublished wheel, but it never turns that
result into a claim that a public package is available.  Likewise, mocked
inference can prove wiring without satisfying real-provider acceptance.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shlex
import subprocess
import sys
import tempfile
import time
import uuid
import venv
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

SECRET_NAME_RE = re.compile(
    r"(?:api[_-]?key|authorization|password|passwd|secret|access[_-]?token|refresh[_-]?token)",
    re.IGNORECASE,
)
ASSIGNMENT_RE = re.compile(
    r"(?i)\b([A-Z0-9_]*(?:API_KEY|TOKEN|PASSWORD|PASSWD|SECRET)[A-Z0-9_]*)"
    r"(\s*[=:]\s*)([^\s,;&]+)"
)
HEADER_RE = re.compile(
    r"(?im)\b(authorization\s*:\s*(?:bearer\s+)?|x-api-key\s*:\s*)([^\s]+)"
)
QUERY_RE = re.compile(
    r"(?i)([?&](?:api[_-]?key|token|access[_-]?token|password|secret)=)([^&#\s]+)"
)
SENSITIVE_OPTIONS = {
    "--api-key",
    "--authorization",
    "--password",
    "--secret",
    "--token",
}


def utc_now() -> str:
    """Return a stable UTC timestamp for audit records."""

    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def linux_resource_snapshot() -> dict[str, int | None]:
    memory_bytes: int | None = None
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemTotal:"):
                memory_bytes = int(line.split()[1]) * 1024
                break
    except (OSError, ValueError, IndexError):
        pass
    disk = os.statvfs(".")
    return {
        "memory_bytes": memory_bytes,
        "disk_free_bytes": disk.f_bavail * disk.f_frsize,
    }


def sanitize_text(text: str, secret_values: Iterable[str] = ()) -> str:
    """Redact credential-shaped material and exact credential values."""

    sanitized = text
    for value in sorted({value for value in secret_values if value}, key=len, reverse=True):
        sanitized = sanitized.replace(value, "<redacted>")
    sanitized = HEADER_RE.sub(lambda match: f"{match.group(1)}<redacted>", sanitized)
    sanitized = ASSIGNMENT_RE.sub(
        lambda match: f"{match.group(1)}{match.group(2)}<redacted>", sanitized
    )
    sanitized = QUERY_RE.sub(lambda match: f"{match.group(1)}<redacted>", sanitized)
    return sanitized


def sanitize_argv(argv: Sequence[str], secret_values: Iterable[str] = ()) -> list[str]:
    result: list[str] = []
    redact_next = False
    for value in argv:
        if redact_next:
            result.append("<redacted>")
            redact_next = False
            continue
        option, separator, _option_value = value.partition("=")
        if option.lower() in SENSITIVE_OPTIONS:
            if separator:
                result.append(f"{option}=<redacted>")
            else:
                result.append(option)
                redact_next = True
            continue
        result.append(sanitize_text(value, secret_values))
    return result


def make_run_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}-{uuid.uuid4().hex[:10]}"


def validate_run_id(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value):
        raise argparse.ArgumentTypeError(
            "run id must contain only letters, numbers, dot, underscore, or hyphen"
        )
    return value


def parse_labeled_command(value: str) -> tuple[str, str]:
    if "::" not in value:
        raise argparse.ArgumentTypeError("commands must use LABEL::COMMAND")
    label, command = value.split("::", 1)
    label = label.strip()
    command = command.strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", label):
        raise argparse.ArgumentTypeError(f"invalid command label: {label!r}")
    if not command:
        raise argparse.ArgumentTypeError("command cannot be empty")
    return label, command


def render_command(command: str, *, python: Path, venv_dir: Path, run_dir: Path) -> list[str]:
    """Parse a command without a shell and substitute documented placeholders."""

    replacements = {
        "{python}": str(python),
        "{venv}": str(venv_dir),
        "{run_dir}": str(run_dir),
    }
    argv = shlex.split(command)
    return [
        _replace_placeholders(value, replacements)
        for value in argv
    ]


def _replace_placeholders(value: str, replacements: dict[str, str]) -> str:
    for placeholder, replacement in replacements.items():
        value = value.replace(placeholder, replacement)
    return value


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


@dataclass
class CommandResult:
    label: str
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str
    started_at: str
    finished_at: str
    monotonic_start_ns: int
    monotonic_end_ns: int
    duration_ms: float
    expected: str
    success: bool


class EvidenceRecorder:
    def __init__(self, run_dir: Path, run_id: str, secret_values: Sequence[str]):
        self.run_dir = run_dir
        self.run_id = run_id
        self.secret_values = tuple(value for value in secret_values if value)
        self.commands_path = run_dir / "commands.jsonl"
        self.log_path = run_dir / "installation.log"

    def run(
        self,
        label: str,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: dict[str, str],
        timeout: float,
        expect_failure: bool = False,
    ) -> CommandResult:
        started_at = utc_now()
        start_ns = time.monotonic_ns()
        timed_out = False
        try:
            process = subprocess.run(
                list(argv),
                cwd=cwd,
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            returncode = process.returncode
            stdout = process.stdout
            stderr = process.stderr
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            returncode = 124
            stdout = _decode_timeout_output(exc.stdout)
            stderr = _decode_timeout_output(exc.stderr) + f"\nTimed out after {timeout:.3f}s"
        end_ns = time.monotonic_ns()
        finished_at = utc_now()
        sanitized_stdout = sanitize_text(stdout, self.secret_values)
        sanitized_stderr = sanitize_text(stderr, self.secret_values)
        success = returncode != 0 if expect_failure else returncode == 0
        expected = "nonzero" if expect_failure else "zero"
        result = CommandResult(
            label=label,
            argv=list(argv),
            returncode=returncode,
            stdout=sanitized_stdout,
            stderr=sanitized_stderr,
            started_at=started_at,
            finished_at=finished_at,
            monotonic_start_ns=start_ns,
            monotonic_end_ns=end_ns,
            duration_ms=round((end_ns - start_ns) / 1_000_000, 3),
            expected=expected,
            success=success and not (expect_failure and timed_out),
        )
        event = {
            "run_id": self.run_id,
            "label": label,
            "argv": sanitize_argv(argv, self.secret_values),
            "cwd": str(cwd),
            "started_at": started_at,
            "finished_at": finished_at,
            "monotonic_start_ns": start_ns,
            "monotonic_end_ns": end_ns,
            "duration_ms": result.duration_ms,
            "exit_code": returncode,
            "expected_exit": expected,
            "timed_out": timed_out,
            "success": result.success,
        }
        with self.commands_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, sort_keys=True) + "\n")
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"[{started_at}] {label}\n")
            handle.write(f"command: {shlex.join(event['argv'])}\n")
            if sanitized_stdout:
                handle.write("stdout:\n" + sanitized_stdout.rstrip() + "\n")
            if sanitized_stderr:
                handle.write("stderr:\n" + sanitized_stderr.rstrip() + "\n")
            handle.write(
                f"exit_code={returncode} expected={expected} "
                f"duration_ms={result.duration_ms} success={str(result.success).lower()}\n\n"
            )
        return result


def _decode_timeout_output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def child_environment(credential_names: Sequence[str]) -> tuple[dict[str, str], list[str]]:
    """Create a deterministic environment, retaining only explicitly allowed secrets."""

    original = dict(os.environ)
    env = {
        name: value
        for name, value in original.items()
        if not SECRET_NAME_RE.search(name)
    }
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    env.pop("VIRTUAL_ENV", None)
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    env["PIP_NO_INPUT"] = "1"
    env["PIP_NO_CACHE_DIR"] = "1"
    missing: list[str] = []
    for name in credential_names:
        if name in original and original[name]:
            env[name] = original[name]
        else:
            missing.append(name)
    return env, missing


def venv_python(venv_dir: Path) -> Path:
    if os.name == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def parse_real_inference_evidence(result: CommandResult | None) -> tuple[bool, dict[str, object], str | None]:
    """Require runtime provenance, not merely a successful model-like command."""
    if result is None or not result.success:
        return False, {}, "Inference command did not complete successfully."
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    if not lines:
        return False, {}, "Inference command produced no structured evidence."
    try:
        value = json.loads(lines[-1])
    except json.JSONDecodeError:
        return False, {}, "Real inference must end with a JSON evidence object."
    if not isinstance(value, dict):
        return False, {}, "Real inference evidence must be a JSON object."
    provenance = value.get("generation_provenance", value.get("provenance", {}))
    if not isinstance(provenance, dict):
        return False, {}, "Real inference evidence is missing generation provenance."
    required = (
        provenance.get("generation_mode") == "identity_model_generation"
        and bool(provenance.get("provider"))
        and bool(provenance.get("model"))
    )
    response_sha256 = value.get("response_sha256")
    if not isinstance(response_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", response_sha256):
        required = False
    if not required:
        return False, {}, (
            "Real inference evidence requires identity_model_generation, provider, model, "
            "and a response_sha256."
        )
    return True, {
        "generation_mode": provenance["generation_mode"],
        "provider": str(provenance["provider"]),
        "model": str(provenance["model"]),
        "provider_latency_ms": provenance.get("latency_ms"),
        "response_sha256": response_sha256,
    }, None


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--wheel", type=Path, required=True, help="wheel artifact to install")
    result.add_argument(
        "--evidence-root",
        type=Path,
        default=Path("evidence/onboarding/runs"),
        help="directory that receives one subdirectory per independent run",
    )
    result.add_argument("--run-id", type=validate_run_id, default=None)
    result.add_argument("--python", default=sys.executable, help="interpreter used to create the venv")
    result.add_argument("--timeout", type=float, default=300.0)
    result.add_argument(
        "--publication-status",
        choices=("unpublished", "published"),
        default="unpublished",
    )
    result.add_argument("--artifact-source", default="local-build")
    result.add_argument("--base-image", default=None, help="container image and digest, when applicable")
    result.add_argument(
        "--network-state",
        choices=("online", "offline", "controlled", "unknown"),
        default="unknown",
    )
    result.add_argument(
        "--smoke-command",
        action="append",
        default=[],
        help="command executed after installation; supports {python}, {venv}, {run_dir}",
    )
    result.add_argument("--onboarding-command", default=None)
    result.add_argument("--restart-command", default=None)
    result.add_argument("--portability-command", default=None)
    result.add_argument(
        "--inference-mode", choices=("none", "mocked", "real"), default="none"
    )
    result.add_argument("--inference-command", default=None)
    result.add_argument("--provider", default=None)
    result.add_argument("--model", default=None)
    result.add_argument(
        "--credential-env",
        action="append",
        default=[],
        help="credential environment variable to pass only to child processes; values are never recorded",
    )
    result.add_argument("--reinstall-check", action="store_true")
    result.add_argument(
        "--expected-failure-command",
        action="append",
        default=[],
        type=parse_labeled_command,
        metavar="LABEL::COMMAND",
    )
    result.add_argument(
        "--recovery-command",
        action="append",
        default=[],
        type=parse_labeled_command,
        metavar="LABEL::COMMAND",
    )
    return result


def _validate_arguments(args: argparse.Namespace) -> list[str]:
    errors: list[str] = []
    if args.timeout <= 0:
        errors.append("timeout must be greater than zero")
    if args.inference_mode == "none" and args.inference_command:
        errors.append("inference-command requires mocked or real inference-mode")
    if args.inference_mode in {"mocked", "real"} and not args.inference_command:
        errors.append(f"{args.inference_mode} inference-mode requires inference-command")
    if args.inference_mode == "real" and not args.provider:
        errors.append("real inference-mode requires provider")
    for name in args.credential_env:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            errors.append(f"invalid credential environment name: {name!r}")
    return errors


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    script_start_ns = time.monotonic_ns()
    script_started_at = utc_now()
    run_id = args.run_id or make_run_id()
    run_dir = args.evidence_root.resolve() / run_id
    if run_dir.exists():
        print(f"Refusing to overwrite existing evidence run: {run_dir}", file=sys.stderr)
        return 2
    run_dir.mkdir(parents=True)
    args.wheel = args.wheel.resolve()

    credential_values = [os.environ.get(name, "") for name in args.credential_env]
    recorder = EvidenceRecorder(run_dir, run_id, credential_values)
    failures = _validate_arguments(args)
    if not args.wheel.is_file():
        failures.append(f"wheel not found: {args.wheel.name}")
    elif args.wheel.suffix != ".whl":
        failures.append(f"artifact is not a wheel: {args.wheel.name}")
    env, missing_credentials = child_environment(args.credential_env)
    if missing_credentials:
        failures.append(
            "missing requested credential environment variable(s): " + ", ".join(missing_credentials)
        )

    environment = {
        "schema_version": "identityos.onboarding.environment.v1",
        "run_id": run_id,
        "captured_at": utc_now(),
        "os": platform.system(),
        "platform": platform.platform(),
        "architecture": platform.machine(),
        "host_python": platform.python_version(),
        "venv_python_source": str(Path(args.python).resolve()),
        "cpu_count": os.cpu_count(),
        **linux_resource_snapshot(),
        "isolation": "fresh-venv",
        "base_image": args.base_image,
        "network_state": args.network_state,
        "pip_cache": "disabled",
        "credential_environment_names": sorted(args.credential_env),
        "credential_values_recorded": False,
    }
    write_json(run_dir / "environment.json", environment)

    artifact = {
        "schema_version": "identityos.onboarding.artifact.v1",
        "filename": args.wheel.name,
        "source": args.artifact_source,
        "publication_status": args.publication_status,
        "unpublished_artifact_test": args.publication_status == "unpublished",
        "sha256": sha256_file(args.wheel) if args.wheel.is_file() else None,
        "size_bytes": args.wheel.stat().st_size if args.wheel.is_file() else None,
    }
    write_json(run_dir / "artifact.json", artifact)

    (run_dir / "baseline.txt").write_text(
        "\n".join(
            [
                f"run_id={run_id}",
                "isolation=fresh-venv",
                "repository_import_path_removed=true",
                "pip_cache=disabled",
                f"publication_status={args.publication_status}",
                "preexisting_identityos=false (fresh venv invariant)",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    results: list[CommandResult] = []
    inference_result: CommandResult | None = None
    stage_timings: dict[str, float | None] = {
        "bootstrap_ms": None,
        "installation_ms": None,
        "launch_ms": None,
        "onboarding_ms": None,
        "inference_ms": None,
        "restart_ms": None,
        "portability_ms": None,
        "total_ms": None,
    }

    if not failures:
        with tempfile.TemporaryDirectory(prefix=f"identityos-clean-{run_id}-") as temp_dir:
            venv_dir = Path(temp_dir) / "venv"
            bootstrap_start_ns = time.monotonic_ns()
            try:
                venv.EnvBuilder(with_pip=True, clear=True).create(venv_dir)
            except Exception as exc:  # capture an actionable harness failure
                failures.append(f"venv bootstrap failed: {type(exc).__name__}: {exc}")
            stage_timings["bootstrap_ms"] = round(
                (time.monotonic_ns() - bootstrap_start_ns) / 1_000_000, 3
            )
            python = venv_python(venv_dir)
            work_dir = Path(temp_dir) / "work"
            work_dir.mkdir()
            isolated_home = work_dir / "home"
            isolated_home.mkdir()
            env = dict(env)
            env["HOME"] = str(isolated_home)
            env["IDENTITYOS_HOME"] = str(work_dir / "identityos-home")
            env["XDG_DATA_HOME"] = str(isolated_home / ".local" / "share")
            env["XDG_CONFIG_HOME"] = str(isolated_home / ".config")
            env["XDG_STATE_HOME"] = str(isolated_home / ".local" / "state")
            env["PATH"] = os.pathsep.join((str(python.parent), env.get("PATH", "")))
            if not failures:
                install = recorder.run(
                    "install",
                    [
                        str(python),
                        "-m",
                        "pip",
                        "install",
                        "--disable-pip-version-check",
                        "--no-input",
                        "--no-cache-dir",
                        str(args.wheel),
                    ],
                    cwd=work_dir,
                    env=env,
                    timeout=args.timeout,
                )
                results.append(install)
                stage_timings["installation_ms"] = install.duration_ms
                if not install.success:
                    failures.append("wheel installation failed")

            if not failures and args.reinstall_check:
                reinstall = recorder.run(
                    "reinstall",
                    [
                        str(python),
                        "-m",
                        "pip",
                        "install",
                        "--disable-pip-version-check",
                        "--no-input",
                        "--no-cache-dir",
                        str(args.wheel),
                    ],
                    cwd=work_dir,
                    env=env,
                    timeout=args.timeout,
                )
                results.append(reinstall)
                if not reinstall.success:
                    failures.append("repeated installation failed")

            if not failures:
                smoke_total = 0.0
                for index, command in enumerate(args.smoke_command, start=1):
                    result = recorder.run(
                        f"smoke-{index}",
                        render_command(command, python=python, venv_dir=venv_dir, run_dir=run_dir),
                        cwd=work_dir,
                        env=env,
                        timeout=args.timeout,
                    )
                    results.append(result)
                    smoke_total += result.duration_ms
                    if not result.success:
                        failures.append(f"smoke command {index} failed")
                stage_timings["launch_ms"] = round(smoke_total, 3) if args.smoke_command else None

            for label, command in args.expected_failure_command:
                result = recorder.run(
                    f"expected-failure-{label}",
                    render_command(command, python=python, venv_dir=venv_dir, run_dir=run_dir),
                    cwd=work_dir,
                    env=env,
                    timeout=args.timeout,
                    expect_failure=True,
                )
                results.append(result)
                if not result.success:
                    failures.append(f"expected failure check {label!r} did not fail as expected")

            for label, command in args.recovery_command:
                result = recorder.run(
                    f"recovery-{label}",
                    render_command(command, python=python, venv_dir=venv_dir, run_dir=run_dir),
                    cwd=work_dir,
                    env=env,
                    timeout=args.timeout,
                )
                results.append(result)
                if not result.success:
                    failures.append(f"recovery check {label!r} failed")

            for label, command, timing_key in (
                ("onboarding", args.onboarding_command, "onboarding_ms"),
                ("inference", args.inference_command, "inference_ms"),
                ("restart", args.restart_command, "restart_ms"),
                ("portability", args.portability_command, "portability_ms"),
            ):
                if command and not failures:
                    result = recorder.run(
                        label,
                        render_command(command, python=python, venv_dir=venv_dir, run_dir=run_dir),
                        cwd=work_dir,
                        env=env,
                        timeout=args.timeout,
                    )
                    results.append(result)
                    stage_timings[timing_key] = result.duration_ms
                    if label == "inference":
                        inference_result = result
                    if not result.success:
                        failures.append(f"{label} command failed")

    if args.inference_mode == "none":
        inference_verification = {
            "schema_version": "identityos.onboarding.inference.v1",
            "mode": "none",
            "verified": False,
            "real_inference_acceptance": False,
            "blocked_reason": "No inference command was supplied for this run.",
        }
    else:
        if args.inference_mode == "real":
            verified, observed, blocked_reason = parse_real_inference_evidence(inference_result)
            if inference_result and inference_result.success and not verified:
                failures.append(blocked_reason or "real inference evidence was invalid")
        else:
            verified = bool(inference_result and inference_result.success)
            observed = {}
            blocked_reason = None if verified else "Inference command did not complete successfully."
        output = inference_result.stdout if inference_result else ""
        inference_verification = {
            "schema_version": "identityos.onboarding.inference.v1",
            "mode": args.inference_mode,
            "verified": verified,
            "real_inference_acceptance": verified and args.inference_mode == "real",
            "provider": observed.get("provider", args.provider),
            "model": observed.get("model", args.model),
            "generation_provenance": observed or None,
            "latency_ms": inference_result.duration_ms if inference_result else None,
            "sanitized_output_sha256": hashlib.sha256(output.encode("utf-8")).hexdigest()
            if inference_result
            else None,
            "credential_values_recorded": False,
            "blocked_reason": blocked_reason,
        }
    write_json(run_dir / "inference-verification.json", inference_verification)

    total_ms = round((time.monotonic_ns() - script_start_ns) / 1_000_000, 3)
    stage_timings["total_ms"] = total_ms
    timings = {
        "schema_version": "identityos.onboarding.timings.v1",
        "run_id": run_id,
        "started_at": script_started_at,
        "finished_at": utc_now(),
        "clock": "time.monotonic_ns",
        "stages": stage_timings,
    }
    write_json(run_dir / "timings.json", timings)

    security_checks = {
        "schema_version": "identityos.onboarding.security.v1",
        "credential_values_recorded": False,
        "child_environment_secret_policy": "deny-by-default with explicit credential-env allowlist",
        "shell_execution": False,
        "logs_sanitized": True,
        "loopback_binding_checked": False,
        "export_secret_canary_checked": False,
    }
    write_json(run_dir / "security-checks.json", security_checks)

    restart_result = next((item for item in results if item.label == "restart"), None)
    write_json(run_dir / "restart-verification.json", {
        "schema_version": "identityos.onboarding.restart.v1",
        "verified": bool(restart_result and restart_result.success),
        "duration_ms": restart_result.duration_ms if restart_result else None,
        "sanitized_output_sha256": (
            hashlib.sha256(restart_result.stdout.encode("utf-8")).hexdigest()
            if restart_result else None
        ),
        "blocked_reason": None if restart_result else "No restart command was supplied for this run.",
    })
    portability_result = next((item for item in results if item.label == "portability"), None)
    write_json(run_dir / "portability-verification.json", {
        "schema_version": "identityos.onboarding.portability.v1",
        "verified": bool(portability_result and portability_result.success),
        "duration_ms": portability_result.duration_ms if portability_result else None,
        "sanitized_output_sha256": (
            hashlib.sha256(portability_result.stdout.encode("utf-8")).hexdigest()
            if portability_result else None
        ),
        "blocked_reason": None if portability_result else "No portability command was supplied for this run.",
    })
    onboarding_result = next((item for item in results if item.label == "onboarding"), None)
    with (run_dir / "onboarding-events.jsonl").open("w", encoding="utf-8") as handle:
        if onboarding_result:
            handle.write(json.dumps({
                "event": "onboarding_command_completed",
                "started_at": onboarding_result.started_at,
                "finished_at": onboarding_result.finished_at,
                "duration_ms": onboarding_result.duration_ms,
                "success": onboarding_result.success,
            }, sort_keys=True) + "\n")

    if failures:
        failure_text = "# Failures\n\n" + "\n".join(f"- {sanitize_text(item)}" for item in failures) + "\n"
    else:
        failure_text = "# Failures\n\nNo failures observed in this run.\n"
    (run_dir / "failures.md").write_text(failure_text, encoding="utf-8")

    summary = {
        "schema_version": "identityos.onboarding.run.v1",
        "run_id": run_id,
        "success": not failures,
        "publication_status": args.publication_status,
        "unpublished_artifact_test": args.publication_status == "unpublished",
        "inference_mode": args.inference_mode,
        "real_inference_verified": inference_verification["real_inference_acceptance"],
        "command_count": len(results),
        "failure_count": len(failures),
        "evidence_directory": str(run_dir),
    }
    write_json(run_dir / "run-summary.json", summary)
    print(json.dumps(summary, sort_keys=True))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
