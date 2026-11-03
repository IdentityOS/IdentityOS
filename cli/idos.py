"""Developer-first IdentityOS installation and local lifecycle command."""

from __future__ import annotations

import argparse
import importlib.metadata
import importlib.util
import json
import os
import platform
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from identityos.adoption import (
    AdoptionConfigError,
    build_configured_adapter,
    public_status,
)
from identityos.adoption import initialize as initialize_adoption
from identityos.adoption import load_config as load_adoption_config
from identityos.paths import IdentityOSPaths, environment_for_paths, get_paths
from identityos.portability import PortabilityError, export_identity, import_identity

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
VERSION_RE = re.compile(r"^[0-9]+(?:\.[0-9]+){1,3}(?:[A-Za-z0-9._+-]*)?$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _default_config(paths: IdentityOSPaths) -> dict[str, Any]:
    del paths
    return {"dashboard": {"host": DEFAULT_HOST, "port": DEFAULT_PORT}}


def _load_config(paths: IdentityOSPaths) -> dict[str, Any]:
    if not paths.config_file.exists():
        return _default_config(paths)
    try:
        return load_adoption_config(paths, required=True).to_dict()
    except AdoptionConfigError as exc:
        raise ValueError(f"Invalid configuration at {paths.config_file}: {exc}") from exc


def _emit(payload: dict[str, Any], *, as_json: bool, human: str) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True) if as_json else human)


def cmd_init(args: argparse.Namespace) -> int:
    paths = get_paths().ensure()
    created = not paths.config_file.exists()
    try:
        initialize_adoption(paths)
    except AdoptionConfigError as exc:
        message = f"Invalid configuration at {paths.config_file}: {exc}"
        _emit({"ok": False, "error": message}, as_json=args.json, human=message)
        return 2
    payload = {
        "ok": True,
        "created": created,
        "config": str(paths.config_file),
        "data": str(paths.data_dir),
        "identity_store": str(paths.identity_store),
        "next": "idos start",
    }
    _emit(
        payload,
        as_json=args.json,
        human=(
            f"IdentityOS is {'initialized' if created else 'already initialized'}.\n"
            f"Data: {paths.data_dir}\nStart onboarding: idos start"
        ),
    )
    return 0


def _read_pid(paths: IdentityOSPaths) -> dict[str, Any] | None:
    try:
        value = json.loads(paths.pid_file.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) and isinstance(value.get("pid"), int) else None
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None


def _pid_alive(pid: int) -> bool:
    if pid <= 1:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _pid_is_idos(pid: int) -> bool:
    command = Path(f"/proc/{pid}/cmdline")
    if not command.exists():
        return True
    try:
        text = command.read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
    except OSError:
        return False
    return "cli.idos" in text and "_serve" in text


def _dashboard_status(paths: IdentityOSPaths) -> dict[str, Any]:
    record = _read_pid(paths)
    if not record:
        return {"running": False}
    pid = int(record["pid"])
    running = _pid_alive(pid) and _pid_is_idos(pid)
    result = dict(record)
    result["running"] = running
    if not running:
        paths.pid_file.unlink(missing_ok=True)
    return result


def _port_available(host: str, port: int) -> bool:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind((host, port))
        return True
    except OSError:
        return False


def _loopback(host: str) -> bool:
    return host in {"localhost", "127.0.0.1", "::1"}


def _resolve_dashboard(args: argparse.Namespace, paths: IdentityOSPaths) -> tuple[str, int]:
    dashboard = _load_config(paths).get("dashboard", {})
    host = args.host or str(dashboard.get("host", DEFAULT_HOST))
    port = args.port or int(dashboard.get("port", DEFAULT_PORT))
    if not 1 <= port <= 65535:
        raise ValueError("port must be between 1 and 65535")
    if not _loopback(host):
        raise ValueError(
            "the onboarding dashboard is local-only; use an authenticated reverse proxy "
            "for deliberate remote access"
        )
    return host, port


def _serve(host: str, port: int, paths: IdentityOSPaths) -> int:
    os.environ.update(environment_for_paths(paths))
    try:
        import uvicorn
    except ImportError:
        print("IdentityOS is missing its dashboard dependency. Reinstall the package.", file=sys.stderr)
        return 2
    uvicorn.run("runtime.playground.app:app", host=host, port=port, log_level="info")
    return 0


def cmd_start(args: argparse.Namespace) -> int:
    paths = get_paths().ensure()
    try:
        initialize_adoption(paths)
    except AdoptionConfigError as exc:
        print(f"IdentityOS configuration is invalid: {exc}", file=sys.stderr)
        return 2
    try:
        host, port = _resolve_dashboard(args, paths)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    status = _dashboard_status(paths)
    if status.get("running"):
        print(f"IdentityOS is already running at {status.get('url')}.")
        return 0
    if not _port_available(host, port):
        print(f"Port {host}:{port} is already in use. Choose another with --port.", file=sys.stderr)
        return 2
    url_host = "[::1]" if host == "::1" else host
    url = f"http://{url_host}:{port}/onboarding"
    if not args.detach:
        print(f"IdentityOS is starting at {url}. Press Ctrl+C to stop.")
        if args.open:
            webbrowser.open(url)
        return _serve(host, port, paths)

    log_path = paths.logs_dir / "dashboard.log"
    env = dict(os.environ)
    env.update(environment_for_paths(paths))
    with log_path.open("ab") as log:
        process = subprocess.Popen(
            [sys.executable, "-m", "cli.idos", "_serve", "--host", host, "--port", str(port)],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
            env=env,
        )
    record = {"pid": process.pid, "host": host, "port": port, "url": url, "started_at": _now()}
    _atomic_json(paths.pid_file, record)
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            paths.pid_file.unlink(missing_ok=True)
            print(f"IdentityOS failed to start. See {log_path}.", file=sys.stderr)
            return 1
        if not _port_available(host, port):
            if args.open:
                webbrowser.open(url)
            print(f"IdentityOS is running at {url} (PID {process.pid}).")
            return 0
        time.sleep(0.1)
    process.terminate()
    paths.pid_file.unlink(missing_ok=True)
    print(f"IdentityOS did not become ready within {args.timeout:g}s. See {log_path}.", file=sys.stderr)
    return 1


def cmd_status(args: argparse.Namespace) -> int:
    paths = get_paths()
    status = _dashboard_status(paths)
    payload = {"ok": True, **status, "pid_file": str(paths.pid_file)}
    human = (
        f"IdentityOS is running at {status.get('url')} (PID {status.get('pid')})."
        if status.get("running")
        else "IdentityOS is not running."
    )
    _emit(payload, as_json=args.json, human=human)
    return 0 if status.get("running") else 3


def cmd_stop(args: argparse.Namespace) -> int:
    paths = get_paths()
    status = _dashboard_status(paths)
    if not status.get("running"):
        _emit({"ok": True, "stopped": False, "running": False}, as_json=args.json, human="IdentityOS is not running.")
        return 0
    pid = int(status["pid"])
    if not _pid_is_idos(pid):
        print(f"Refusing to stop PID {pid}: it is not an IdentityOS dashboard process.", file=sys.stderr)
        return 2
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline and _pid_alive(pid):
        time.sleep(0.1)
    if _pid_alive(pid):
        print(f"IdentityOS did not stop within {args.timeout:g}s.", file=sys.stderr)
        return 1
    paths.pid_file.unlink(missing_ok=True)
    _emit({"ok": True, "stopped": True, "running": False}, as_json=args.json, human="IdentityOS stopped.")
    return 0


def _writable_check(path: Path) -> tuple[bool, str]:
    try:
        path.mkdir(parents=True, exist_ok=True)
        fd, probe = tempfile.mkstemp(prefix=".doctor-", dir=str(path))
        os.close(fd)
        os.unlink(probe)
        return True, "writable"
    except OSError as exc:
        return False, str(exc)


def _provider_summary(connectivity: bool) -> dict[str, Any]:
    paths = get_paths()
    try:
        config = load_adoption_config(paths)
        if config.provider:
            public = dict(public_status(paths).get("provider") or {})
            result: dict[str, Any] = {
                "configured": True,
                "source": "identityos_config",
                "providers": [public],
                "verified": bool(public.get("verified_at")),
            }
            if connectivity:
                try:
                    from adapters.base import BaseAdapter

                    adapter = build_configured_adapter(paths)
                    if type(adapter).health_check is BaseAdapter.health_check:
                        result["connectivity"] = None
                        result["connectivity_detail"] = (
                            "this adapter has no network-backed health check; use onboarding verification"
                        )
                    else:
                        result["connectivity"] = bool(adapter.health_check())
                except Exception as exc:
                    result["connectivity"] = False
                    result["error"] = type(exc).__name__
            return result
    except AdoptionConfigError as exc:
        return {"configured": False, "providers": [], "error": type(exc).__name__}
    try:
        from adapters.configuration import build_adapter_from_env, describe_adapter

        adapter = build_adapter_from_env()
        result = describe_adapter(adapter)
        if connectivity and adapter is not None:
            try:
                result["connectivity"] = bool(adapter.health_check())
            except Exception as exc:
                # Exception type is actionable without leaking a provider message
                # that could contain request details.
                result["connectivity"] = False
                result["error"] = type(exc).__name__
        return result
    except Exception as exc:
        return {"configured": False, "providers": [], "error": type(exc).__name__}


def _installed_version() -> str:
    try:
        from identityos import __version__

        return __version__
    except (ImportError, AttributeError):
        pass
    try:
        return importlib.metadata.version("identityos")
    except importlib.metadata.PackageNotFoundError:
        return "source checkout"


def _module_available(name: str) -> bool:
    """Handle test doubles and embedded modules whose ``__spec__`` is absent."""
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return sys.modules.get(name) is not None


def cmd_doctor(args: argparse.Namespace) -> int:
    paths = get_paths()
    checks: list[dict[str, Any]] = []
    supported = sys.platform.startswith("linux") or sys.platform == "darwin"
    checks.append({
        "name": "platform",
        "status": "pass" if supported else "warning",
        "detail": f"{platform.system()} {platform.release()} ({platform.machine()})",
    })
    python_ok = sys.version_info >= (3, 11)
    checks.append({"name": "python", "status": "pass" if python_ok else "fail", "detail": platform.python_version()})
    required_modules = ("fastapi", "httpx", "jinja2", "openai", "uvicorn")
    missing_modules = [name for name in required_modules if not _module_available(name)]
    checks.append({
        "name": "dependencies",
        "status": "fail" if missing_modules else "pass",
        "detail": (
            {"missing": missing_modules, "recovery": "reinstall the IdentityOS application environment"}
            if missing_modules else {"required_modules": list(required_modules)}
        ),
    })
    for name, path in (
        ("data", paths.data_dir),
        ("config", paths.config_dir),
        ("state", paths.state_dir),
        ("identity_store", paths.identity_store),
    ):
        ok, detail = _writable_check(path)
        checks.append({"name": name, "status": "pass" if ok else "fail", "path": str(path), "detail": detail})
    try:
        config = _load_config(paths)
        checks.append({"name": "configuration", "status": "pass", "path": str(paths.config_file)})
    except ValueError as exc:
        config = _default_config(paths)
        checks.append({"name": "configuration", "status": "fail", "detail": str(exc)})
    dashboard = config.get("dashboard", {})
    host = str(dashboard.get("host", DEFAULT_HOST))
    try:
        port = int(dashboard.get("port", DEFAULT_PORT))
    except (TypeError, ValueError):
        port = DEFAULT_PORT
        checks.append({"name": "dashboard_config", "status": "fail", "detail": "dashboard port must be an integer"})
    running = _dashboard_status(paths)
    port_ok = bool(running.get("running")) or _port_available(host, port)
    checks.append({
        "name": "dashboard_port",
        "status": "pass" if port_ok else "fail",
        "detail": "dashboard running" if running.get("running") else ("available" if port_ok else "already in use"),
        "host": host,
        "port": port,
    })
    providers = _provider_summary(args.connectivity)
    checks.append({
        "name": "inference_provider",
        "status": "pass" if providers.get("configured") else "warning",
        "detail": providers,
    })
    try:
        adoption = public_status(paths)
        identity_id = adoption.get("identity_id")
        identity_initialized = bool(identity_id)
        identity_count = 0
        if paths.identity_store.exists():
            from runtime.persistence import JSONFileBackend

            identity_count = len(JSONFileBackend(str(paths.identity_store)).list_identities())
        checks.append({
            "name": "identity_initialization",
            "status": "pass" if identity_initialized else "warning",
            "detail": {
                "selected_identity": identity_id,
                "stored_identity_count": identity_count,
            },
        })
    except Exception as exc:
        checks.append({
            "name": "identity_initialization",
            "status": "fail",
            "detail": type(exc).__name__,
        })
    checks.append({
        "name": "dashboard_security",
        "status": "pass" if _loopback(host) else "fail",
        "detail": "loopback only" if _loopback(host) else "non-loopback binding configured",
    })
    failures = [item for item in checks if item["status"] == "fail"]
    payload = {
        "ok": not failures,
        "timestamp": _now(),
        "version": _installed_version(),
        "checks": checks,
        "dashboard": running,
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        for item in checks:
            print(f"[{item['status'].upper():7}] {item['name']}: {item.get('detail', '')}")
        print("No credential values are included in this report.")
    return 1 if failures else 0


def _install_root() -> Path:
    default = Path.home() / ".local" / "share" / "identityos-runtime"
    return Path(os.environ.get("IDENTITYOS_INSTALL_ROOT", default)).expanduser().resolve(strict=False)


def cmd_update(args: argparse.Namespace) -> int:
    if bool(args.artifact) == bool(args.version):
        print("Choose exactly one of --artifact or --version. No update was performed.", file=sys.stderr)
        return 2
    installer = _install_root() / "install.sh"
    if not installer.exists():
        print(
            "This installation is not managed by the IdentityOS installer. "
            "No update was performed; use the same package manager that installed it.",
            file=sys.stderr,
        )
        return 2
    if args.version and not VERSION_RE.fullmatch(args.version):
        print("--version must be an exact version such as 2.1.0", file=sys.stderr)
        return 2
    command = [str(installer)]
    if args.artifact:
        artifact = Path(args.artifact).expanduser().resolve()
        if not artifact.exists():
            print(f"Artifact does not exist: {artifact}", file=sys.stderr)
            return 2
        command += ["--artifact", str(artifact)]
    else:
        command += ["--version", args.version]
    if args.dry_run:
        print(json.dumps({"updated": False, "command": command, "current_version": _installed_version()}))
        return 0
    return subprocess.run(command, check=False).returncode


def _safe_remove_root(path: Path) -> None:
    resolved = path.expanduser().resolve()
    home = Path.home().resolve()
    if resolved in {Path("/"), home} or len(resolved.parts) < 4:
        raise ValueError(f"refusing to remove unsafe path: {resolved}")
    shutil.rmtree(resolved)


def cmd_uninstall(args: argparse.Namespace) -> int:
    root = _install_root()
    marker = root / ".identityos-install"
    if root.exists() and not marker.exists():
        print(f"Refusing to remove unrecognized installation directory: {root}", file=sys.stderr)
        return 2
    if args.purge_data and not args.yes:
        print("--purge-data permanently deletes identities and requires --yes.", file=sys.stderr)
        return 2
    bin_dir = Path(os.environ.get("IDENTITYOS_BIN_DIR", Path.home() / ".local" / "bin"))
    resolved_bin_dir = bin_dir.expanduser().resolve(strict=False)
    for command_name in ("idos", "identity"):
        bin_path = resolved_bin_dir / command_name
        if bin_path.is_symlink():
            bin_path.unlink()
    if root.exists():
        _safe_remove_root(root)
    purged: list[str] = []
    if args.purge_data:
        paths = get_paths()
        for target in {paths.data_dir, paths.config_dir, paths.state_dir}:
            if target.exists():
                _safe_remove_root(target)
                purged.append(str(target))
    payload = {"uninstalled": True, "data_preserved": not args.purge_data, "purged": sorted(purged)}
    human = (
        "IdentityOS code was removed. Your identity data was preserved."
        if not args.purge_data
        else "IdentityOS and its data were removed."
    )
    _emit(payload, as_json=args.json, human=human)
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    try:
        result = export_identity(args.identity, args.output, paths=get_paths())
    except (PortabilityError, OSError) as exc:
        print(f"Export failed: {exc}", file=sys.stderr)
        return 1
    _emit(
        {"ok": True, **result},
        as_json=args.json,
        human=(
            f"Exported {result['identity_id']} to {result['path']}\n"
            f"Archive SHA-256: {result['sha256']}"
        ),
    )
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    try:
        result = import_identity(args.archive, paths=get_paths(), replace=args.replace)
    except (PortabilityError, OSError) as exc:
        print(f"Import failed: {exc}", file=sys.stderr)
        return 1
    _emit(
        {"ok": True, **result},
        as_json=args.json,
        human=(
            f"Imported {result['identity_id']} and verified it in a fresh runtime.\n"
            f"Memories: {result['memory_count']}; namespaces: {result['namespace_count']}"
        ),
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="idos", description="Install, configure, and run IdentityOS locally")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="initialize user-owned configuration and storage")
    init.add_argument("--json", action="store_true")
    init.set_defaults(func=cmd_init)

    start = sub.add_parser("start", help="start the local onboarding dashboard")
    start.add_argument("--host")
    start.add_argument("--port", type=int)
    start.add_argument("--detach", action="store_true", help="run in the background until `idos stop`")
    start.add_argument("--open", action="store_true", help="open the local URL in a browser")
    start.add_argument("--timeout", type=float, default=10.0)
    start.set_defaults(func=cmd_start)

    status = sub.add_parser("status", help="report dashboard state")
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=cmd_status)

    stop = sub.add_parser("stop", help="stop a dashboard started with --detach")
    stop.add_argument("--timeout", type=float, default=10.0)
    stop.add_argument("--json", action="store_true")
    stop.set_defaults(func=cmd_stop)

    doctor = sub.add_parser("doctor", help="diagnose installation, storage, provider, and dashboard health")
    doctor.add_argument("--json", action="store_true")
    doctor.add_argument("--connectivity", action="store_true", help="perform provider network health checks")
    doctor.set_defaults(func=cmd_doctor)

    update = sub.add_parser("update", help="update from an exact version or local artifact")
    source = update.add_mutually_exclusive_group()
    source.add_argument("--artifact")
    source.add_argument("--version")
    update.add_argument("--dry-run", action="store_true")
    update.set_defaults(func=cmd_update)

    uninstall = sub.add_parser("uninstall", help="remove code while preserving identity data")
    uninstall.add_argument("--purge-data", action="store_true")
    uninstall.add_argument("--yes", action="store_true")
    uninstall.add_argument("--json", action="store_true")
    uninstall.set_defaults(func=cmd_uninstall)

    portable_export = sub.add_parser("export", help="create a scrubbed portable identity archive")
    portable_export.add_argument("--identity", required=True)
    portable_export.add_argument("--output", required=True)
    portable_export.add_argument("--json", action="store_true")
    portable_export.set_defaults(func=cmd_export)

    portable_import = sub.add_parser("import", help="transactionally import a portable identity archive")
    portable_import.add_argument("archive")
    portable_import.add_argument("--replace", action="store_true", help="replace an existing identity atomically")
    portable_import.add_argument("--json", action="store_true")
    portable_import.set_defaults(func=cmd_import)

    serve = sub.add_parser("_serve", help=argparse.SUPPRESS)
    serve.add_argument("--host", required=True)
    serve.add_argument("--port", required=True, type=int)
    serve.set_defaults(func=lambda args: _serve(args.host, args.port, get_paths().ensure()))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
