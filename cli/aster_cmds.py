"""
cli/aster_cmds.py — Aster operator commands.

``identity aster`` drives a persistent autonomous operator:
  init / tick / run / stop / status / needs / opportunities / relationships /
  messages / provenance / escalate / decide / pause / resume / override /
  outbound-mode / would-send.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional


logger = logging.getLogger("identityos.cli")

DEFAULT_STORE = ".identity_store"


def _get_storage(args: argparse.Namespace):
    from runtime.persistence import get_backend

    backend_kwargs: dict = {}
    if args.backend == "sqlite":
        backend_kwargs["db_path"] = str(Path(args.store) / "identities.db")
    else:
        backend_kwargs["root_dir"] = args.store
    return get_backend(args.backend, **backend_kwargs)


def _mailbox_root(args: argparse.Namespace) -> str:
    return str(getattr(args, "mailbox_root", None) or os.environ.get("IDENTITY_MAILBOX_ROOT", ".identity_mailbox"))


def _registry(storage: Any):
    from core.capabilities.registry import CapabilityRegistry

    return CapabilityRegistry(storage)


def _interop_dirs(args: argparse.Namespace) -> tuple[str, str]:
    """(secret store dir, culture commons state dir) under the project/identity stores."""
    root = Path(getattr(args, "project_root", ".") or ".")
    state_dir = Path(getattr(args, "store", DEFAULT_STORE) or DEFAULT_STORE)
    return (
        str((root / ".identityos" / "secrets").resolve()),
        str((state_dir / ".identityos" / "culture_commons").resolve()),
    )


def _build_engine(args: argparse.Namespace):
    """Build (or reuse) a registered Aster engine for the given storage."""
    from core.operations.aster import build_aster_engine
    from core.operations.runtime_registry import get_engine_for

    storage = _get_storage(args)
    existing = get_engine_for(storage, "aster")
    if existing is not None:
        return existing

    from core.capabilities.email import CapabilityTransport, build_transport

    secret_dir, cc_state_dir = _interop_dirs(args)

    # Prefer the permission-gated email transport; fall back to a direct
    # file mailbox so the operator can still run in offline/dry-run mode.
    registry = _registry(storage)
    transport = None
    if registry.get("aster", "email") is not None:
        transport = CapabilityTransport(registry, "aster")
    else:
        transport = build_transport(
            {"root": _mailbox_root(args), "mailbox": "aster"},
            identity_id="aster",
        )

    candidate_sources = _load_candidates(args)
    search_fn = _build_search_fn(args)
    adapter = _build_reply_adapter()

    from core.operations.skill_acquisition import SkillAcquisitionResolver
    from core.secrets.store import SecretStore

    secret_store = SecretStore(secret_dir)
    acquisition = SkillAcquisitionResolver(
        storage,
        "aster",
        registry=registry,
        secret_store_dir=secret_dir,
        state_dir=cc_state_dir,
    )

    engine = build_aster_engine(
        storage,
        project_root=args.project_root,
        transport=transport,
        adapter=adapter,
        capability_registry=registry,
        acquisition=acquisition,
        candidate_sources=candidate_sources,
        search_fn=search_fn,
        secret_store=secret_store,
        register=True,
    )
    if getattr(args, "with_culture", False):
        from core.operations.surfaces import CultureCommonsSurface

        engine._surfaces.append(CultureCommonsSurface(engine))
    return engine


def _build_push_drain_service(args: argparse.Namespace, engine: Any) -> Any:
    """The operator-side Gmail push service, or None when not configured.

    It shares storage with the presence process, so it sees the notifications
    that process queued. Returns None (rather than raising) when OAuth is not
    configured, leaving polling as the ingest path.
    """
    try:
        from core.capabilities.email.gmail_push import GmailPushConfig, GmailPushService

        config = GmailPushConfig.from_env()
        if not config.configured:
            return None
        return GmailPushService(
            config,
            storage=_get_storage(args),
            identity_id="aster",
            # The operator's own pipeline, so a drained message is answered by
            # the same code path (and the same ledger) as a polled one.
            ingest=engine.ingest_messages,
        )
    except Exception as exc:  # pragma: no cover - configuration dependent
        logger.warning("gmail push drain unavailable: %s", exc)
        return None


def _start_push_drain(service: Any, *, stop: threading.Event, interval: float) -> None:
    """Drain queued push notifications on a short cadence until shutdown.

    Shares the run loop's stop event so shutdown is graceful: an in-flight
    drain completes and its state is persisted before the process exits.
    """
    def _worker():
        while not stop.is_set():
            if stop.wait(interval):
                break
            try:
                service.drain()
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("gmail push drain failed: %s", exc)

    threading.Thread(target=_worker, name="gmail-push-drain", daemon=True).start()
    print(f"Gmail push drain (interval={interval}s)", flush=True)


def _build_reply_adapter() -> Any:
    """Resolve the model adapter used for substantive replies, non-interactively.

    Reads the configured providers from the environment (IDENTITY_ADAPTER /
    API keys).  Returns None when nothing is configured — substantive replies
    then fail explicitly (deferred) instead of being faked by a template.
    Never returns credentials; adapters keep keys in object state.
    """
    try:
        from adapters.configuration import build_adapter_from_env

        values = dict(os.environ)
        aster_adapter = values.get("ASTER_ADAPTER", "").strip()
        if aster_adapter:
            values["IDENTITY_ADAPTER"] = aster_adapter
            values["IDENTITY_ADAPTER_CONFIG"] = values.get("ASTER_ADAPTER_CONFIG", "{}")
        adapter = build_adapter_from_env(values)
        prefer_remote = values.get("ASTER_PREFER_REMOTE", "").strip().casefold()
        if prefer_remote in {"1", "true", "yes"}:
            from adapters.chain import ChainAdapter
            from adapters.openai_adapter import OllamaAdapter

            if isinstance(adapter, ChainAdapter):
                remote = [leaf for leaf in adapter.adapters if not isinstance(leaf, OllamaAdapter)]
                local = [leaf for leaf in adapter.adapters if isinstance(leaf, OllamaAdapter)]
                adapter = ChainAdapter([*remote, *local])
        return adapter
    except Exception:
        return None


def _load_candidates(args: argparse.Namespace) -> list[Any]:
    from core.operations import StaticCandidateSource

    path = getattr(args, "candidates", None)
    if not path:
        return []
    file_path = Path(path)
    if not file_path.is_file():
        print(f"Candidates file not found: {file_path}", file=sys.stderr)
        sys.exit(1)
    data = json.loads(file_path.read_text(encoding="utf-8"))
    candidates = data if isinstance(data, list) else data.get("candidates", [])
    return [StaticCandidateSource(candidates, name=file_path.stem)]


def _build_search_fn(args: argparse.Namespace) -> Optional[Any]:
    storage = _get_storage(args)
    registry = _registry(storage)
    if not getattr(args, "search", False):
        return None
    granted, _ = registry.can("aster", "web.search")
    if not granted:
        print(
            "web.search is not granted to 'aster'. Run: identity cap grant web --identity aster --permission network",
            file=sys.stderr,
        )
        return None
    cap = registry.get("aster", "web")
    if cap is None:
        print("web capability is not installed. Run: identity cap install web --identity aster", file=sys.stderr)
        return None

    def search(query: str) -> list[dict]:
        result = registry.call("aster", "web.search", query=query)
        if not result.success:
            return []
        return (result.data or {}).get("results", [])

    return search


def _print_json(data: Any) -> None:
    # Values pass through recursive key-based redaction immediately above the
    # output sink; CodeQL does not model this project-specific sanitizer.
    rendered = json.dumps(_redact_json_value(data), indent=2, default=str)
    sys.stdout.write(f"{rendered}\n")


_SENSITIVE_OUTPUT_KEYS = {
    "api_key",
    "authorization",
    "credential",
    "password",
    "secret",
    "session_token",
    "token",
}


def _redact_json_value(value: Any, *, key: str = "") -> Any:
    """Redact secret-bearing fields before CLI output reaches stdout or logs."""
    normalized = key.casefold().replace("-", "_")
    if normalized in _SENSITIVE_OUTPUT_KEYS or normalized.endswith(("_password", "_secret", "_token")):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {item_key: _redact_json_value(item, key=str(item_key)) for item_key, item in value.items()}
    if isinstance(value, list):
        return [_redact_json_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_json_value(item) for item in value)
    return value


# ── commands ─────────────────────────────────────────────────────────────────


def cmd_aster_init(args: argparse.Namespace) -> int:
    from core.capabilities import email as _email_mod  # noqa: F401  (register)
    from core.capabilities import operations as _ops_mod  # noqa: F401  (register)
    from core.capabilities import a2a as _a2a_mod  # noqa: F401  (register)
    from core.capabilities import culture_commons as _cc_mod  # noqa: F401  (register)
    from core.capabilities import mcp as _mcp_mod  # noqa: F401  (register)
    from core.operations.aster import create_aster_identity, persist_aster_identity
    from core.operations.runtime_registry import register_engine

    storage = _get_storage(args)
    spec = persist_aster_identity(storage)
    print(f"Identity '{spec.id}' initialised ({spec.tagline}).")
    print(f"  store       : {args.store} ({args.backend})")
    print(f"  project     : {args.project_root}")

    engine = _build_engine(args)
    if engine:
        register_engine(engine)
        print(f"  engine      : {engine.mode}")

    secret_dir, cc_state_dir = _interop_dirs(args)
    registry = _registry(storage)
    # Persist only a non-secret config reference when real mail is available;
    # credentials themselves always come from the environment at call time.
    email_config = {"backend": "smtp"} if os.environ.get("IDENTITY_SMTP_HOST") else None
    for cap_id in ("email", "operations", "web"):
        if registry.get(spec.id, cap_id) is None:
            registry.install(spec.id, cap_id, config=email_config if cap_id == "email" else None)
            print(f"  installed   : capability '{cap_id}'")
    for cap_id in ("mcp", "a2a", "culture_commons"):
        if registry.get(spec.id, cap_id) is None:
            config = None
            if cap_id == "culture_commons":
                config = {"state_dir": cc_state_dir, "secret_store_dir": secret_dir}
            registry.install(spec.id, cap_id, config=config)
            print(f"  installed   : capability '{cap_id}'")
    if email_config:
        print("  email       : SMTP/IMAP backend referenced from environment (credentials not stored)")
    else:
        print("  email       : file mailbox (set IDENTITY_SMTP_HOST/IDENTITY_IMAP_HOST and re-init for real mail)")
    if args.grant_email:
        registry.grant(spec.id, "email", "email.send")
        registry.grant(spec.id, "email", "email.read")
        print("  granted     : email.send, email.read")
    if args.grant_search:
        registry.grant(spec.id, "web", "network")
        print("  granted     : web.network (web.search)")
    if args.grant_operations:
        registry.grant(spec.id, "operations", "operations.run")
        print("  granted     : operations.run")
    if args.grant_mcp:
        registry.grant(spec.id, "mcp", "mcp.call")
        print("  granted     : mcp.call")
    if args.grant_a2a:
        registry.grant(spec.id, "a2a", "a2a.converse")
        print("  granted     : a2a.converse")
    if args.grant_culture:
        registry.grant(spec.id, "culture_commons", "culture_commons.standing")
        registry.grant(spec.id, "culture_commons", "culture_commons.post")
        print("  granted     : culture_commons.standing, culture_commons.post")

    print()
    print("Next: identity aster tick --project-root .")
    print("Then: identity aster status")
    return 0


def cmd_aster_tick(args: argparse.Namespace) -> int:
    engine = _build_engine(args)
    report = engine.tick(
        discover=not getattr(args, "no_discover", False),
        evaluate=not getattr(args, "no_evaluate", False),
        act=not getattr(args, "no_act", False),
        monitor=not getattr(args, "no_monitor", False),
        follow_ups=not getattr(args, "no_followups", False),
        surfaces=bool(getattr(args, "with_culture", False)),
    )
    _print_json(report.to_dict())
    return 0


def cmd_aster_run(args: argparse.Namespace) -> int:
    if getattr(args, "daemon", False):
        return _spawn_daemon(args)

    import signal
    import time

    stop_requested = threading.Event()

    def _on_stop(signum, frame) -> None:
        # Arise, and follow me... on the next completed tick.
        stop_requested.set()

    signal.signal(signal.SIGTERM, _on_stop)
    signal.signal(signal.SIGINT, _on_stop)

    is_daemon = os.environ.get("ASTER_DAEMON") == "1"
    if is_daemon:
        pidfile = _pidfile_path(args)
        pidfile.write_text(str(os.getpid()))

    engine = _build_engine(args)
    presence = engine._presence

    if presence is not None:
        presence.start_run(pid=os.getpid(), next_planned_action="First observation tick")

    heartbeat_stop = threading.Event()

    def _heartbeat_worker():
        # Liveness only: prove the operator process is alive. Never manufacture
        # lifecycle state here — status/activity transitions come exclusively
        # from actual tick execution via engine._presence_update.
        while not heartbeat_stop.wait(60.0):
            if stop_requested.is_set():
                break
            try:
                presence.heartbeat()
            except Exception:
                pass

    hb_thread = threading.Thread(target=_heartbeat_worker, name="aster-heartbeat", daemon=True)
    hb_thread.start()

    # Email ingest runs on its OWN short cadence, independent of the operator
    # tick. Tying mailbox discovery to the operator interval made most of a
    # reply's wall-clock time a wait for the next poll rather than work: a
    # 300s operator interval put up to five minutes of pure idle in front of
    # every answer. The operator loop stays at its slow, deliberate pace;
    # answering a human does not.
    email_interval = max(5.0, float(getattr(args, "email_interval", 0.0) or 0.0))
    mail_stop = threading.Event()
    mail_thread: Optional[threading.Thread] = None
    if email_interval:
        def _email_worker():
            # First pass happens immediately (the operator tick also polls),
            # then the short cadence takes over.
            while not mail_stop.is_set() and not stop_requested.is_set():
                if mail_stop.wait(email_interval):
                    break
                try:
                    engine.poll_email()
                except Exception as exc:  # pragma: no cover - defensive
                    logger.warning("email ingest poll failed: %s", exc)

        mail_thread = threading.Thread(
            target=_email_worker, name="aster-email-ingest", daemon=True)
        mail_thread.start()

    # Gmail push notifications are received by the presence process and queued
    # in shared storage. This process drains them, because it is the only one
    # with a configured engine and the single writer for email jobs and sends.
    # If it is down, the queue accumulates and drains on restart — the mail is
    # not lost, it is late, and health says so.
    push_service = _build_push_drain_service(args, engine)
    if push_service is not None:
        _start_push_drain(push_service, stop=mail_stop,
                          interval=max(5.0, email_interval or 30.0))

    iterations = args.iterations or -1
    interval = max(0.0, args.interval)
    count = 0
    print(f"Running Aster operator loop (interval={interval}s, iterations={iterations})",
          flush=True)
    if email_interval:
        print(f"Email ingest loop (interval={email_interval}s)", flush=True)
    try:
        while (iterations < 0 or count < iterations) and not stop_requested.is_set():
            report = engine.tick(
                surfaces=bool(getattr(args, "with_culture", False)),
            )
            print(f"[tick {count + 1}] outreach={len(report.outreach_sent)} "
                  f"escalations={len(report.escalations)} replies={len(report.replies_sent)} "
                  f"followups={len(report.follow_ups_sent)} errors={len(report.errors)}",
                  flush=True)
            count += 1
            if stop_requested.is_set():
                print("Graceful stop: in-flight tick completed; state persisted.", flush=True)
                break
            if interval <= 0:
                break
            stop_requested.wait(interval)
    finally:
        heartbeat_stop.set()
        mail_stop.set()
        hb_thread.join(timeout=2.0)
        if mail_thread is not None:
            mail_thread.join(timeout=5.0)
        if presence is not None:
            presence.mark_offline(reason="operator stopped gracefully")
        if is_daemon:
            _pidfile_path(args).unlink(missing_ok=True)
            _lockfile_path(args).unlink(missing_ok=True)
    return 0


def _pidfile_path(args: argparse.Namespace) -> Path:
    return Path(args.store) / "aster-daemon.pid"


def _lockfile_path(args: argparse.Namespace) -> Path:
    return Path(args.store) / "aster-daemon.lock"


def _try_acquire_lock(args: argparse.Namespace):
    """Atomically claim the daemon lock; returns an open fd holding it or None."""
    import fcntl

    lock_path = _lockfile_path(args)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = open(lock_path, "a+", encoding="utf-8")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fd.close()
        return None
    return fd


def _spawn_daemon(args: argparse.Namespace) -> int:
    import subprocess

    lock_fd = _try_acquire_lock(args)
    if lock_fd is None:
        print(f"A daemon is already starting/running for this store. Stop it with 'identity aster stop'.", file=sys.stderr)
        return 1

    # With the spawn window locked, the pidfile is the steady-state guard: a
    # live pid here means another daemon owns this store.
    pidfile = _pidfile_path(args)
    if pidfile.exists():
        try:
            pid_alive = int(pidfile.read_text().strip())
            os.kill(pid_alive, 0)
            lock_fd.close()
            print(f"A daemon is already running (pid {pid_alive}). Stop it with 'identity aster stop'.", file=sys.stderr)
            return 1
        except (OSError, ValueError):
            pidfile.unlink(missing_ok=True)

    log_file = Path(args.store) / "aster-daemon.log"
    store_abs = str(Path(args.store).resolve())
    root_abs = str(Path(args.project_root).resolve())
    log_abs = str(log_file.resolve())
    cmd = [
        sys.executable,
        "-m",
        "cli.main",
        "aster",
        "run",
        "--store", store_abs,
        "--backend", args.backend,
        "--project-root", root_abs,
        "--iterations", "-1",
        "--interval", str(getattr(args, "interval", 300.0) or 300.0),
        "--email-interval", str(getattr(args, "email_interval", 30.0) or 30.0),
    ]
    env = dict(os.environ)
    env["ASTER_DAEMON"] = "1"
    if getattr(args, "candidates", None):
        cmd += ["--candidates", Path(args.candidates).resolve().as_posix()]
    if getattr(args, "search", False):
        cmd += ["--search"]
    if _mailbox_root(args) != ".identity_mailbox":
        cmd += ["--mailbox-root", _mailbox_root(args)]
    if getattr(args, "with_culture", False):
        cmd += ["--with-culture"]

    log_file.parent.mkdir(parents=True, exist_ok=True)
    with log_file.open("ab") as log_handle:
        proc = subprocess.Popen(
            cmd,
            stdout=log_handle,
            stderr=log_handle,
            start_new_session=True,
            cwd=str(Path(Path(__file__).resolve().parents[1])),
            env=env,
        )
    pidfile.write_text(str(proc.pid))
    lock_fd.close()
    print(f"Aster daemon started (pid {proc.pid}, log {log_abs}).")
    print("Monitor with 'identity aster status --store %s'." % args.store)
    return 0


def cmd_aster_stop(args: argparse.Namespace) -> int:
    import signal
    import time as _time

    pidfile = _pidfile_path(args)
    if not pidfile.exists():
        print("No Aster daemon is running for this store.", file=sys.stderr)
        return 1
    try:
        pid = int(pidfile.read_text().strip())
    except ValueError:
        print("Corrupt pidfile; removing.", file=sys.stderr)
        pidfile.unlink(missing_ok=True)
        _lockfile_path(args).unlink(missing_ok=True)
        return 1
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pidfile.unlink(missing_ok=True)
        _lockfile_path(args).unlink(missing_ok=True)
        print(f"No live process for pid {pid}; cleared stale daemon state.")
        return 0
    except Exception as exc:
        print(f"Failed to signal daemon: {exc}", file=sys.stderr)
        return 1
    # Wait for a graceful stop: the daemon completes its in-flight tick, then
    # removes its own pidfile. Give one interval-plus-slack before forcing it.
    deadline = _time.monotonic() + 310.0
    while _time.monotonic() < deadline:
        if not pidfile.exists():
            _lockfile_path(args).unlink(missing_ok=True)
            print(f"Stopped Aster daemon (pid {pid}) after graceful in-flight completion. State is persisted.")
            return 0
        _time.sleep(0.25)
    # Fallback: the daemon did not exit in time. Force-terminate.
    try:
        os.kill(pid, signal.SIGKILL)
    except Exception:
        pass
    pidfile.unlink(missing_ok=True)
    _lockfile_path(args).unlink(missing_ok=True)
    print(f"Daemon (pid {pid}) did not finish its tick within the grace period; forced stop. State is persisted per-phase.")
    return 0


def cmd_aster_status(args: argparse.Namespace) -> int:
    engine = _build_engine(args)
    storage = _get_storage(args)
    pidfile = _pidfile_path(args)
    daemon = {"running": False, "pid": None}
    if pidfile.exists():
        try:
            pid = int(pidfile.read_text().strip())
            os.kill(pid, 0)
            daemon = {"running": True, "pid": pid}
        except (OSError, ValueError):
            daemon = {"running": False, "pid": None}

    from core.operations.presence import PresenceStore, format_presence_card
    presence = PresenceStore(storage, "aster")
    view = presence.public_view()
    # The pidfile only exists for legacy `aster run --daemon` spawns; the
    # systemd unit runs the loop directly. Presence (operator pid + fresh
    # heartbeat) is the authoritative liveness signal — the pidfile may only
    # confirm activity for the same process, never force "stopped".
    if daemon["running"] and view.get("operator_pid") in (None, daemon["pid"]):
        view["operator_pid"] = daemon["pid"]
        if view.get("service_state") != "active":
            view["service_state"] = "active"

    report = engine.status()
    status = report.to_dict() if hasattr(report, "to_dict") else dict(report)

    # Get commons standing from surface if present
    for surface in engine._surfaces:
        if getattr(surface, "name", "") == "culture_commons":
            try:
                view["commons_standing"] = surface.standing_state()
            except Exception:
                pass
            break

    if getattr(args, "json", False):
        output = {**status, "daemon": daemon, "presence": view}
        _print_json(output)
    else:
        print(format_presence_card(view))
    return 0


def cmd_aster_needs(args: argparse.Namespace) -> int:
    engine = _build_engine(args)
    _print_json({"needs": [n.to_dict() for n in engine.store.list_needs()]})
    return 0


def cmd_aster_opportunities(args: argparse.Namespace) -> int:
    engine = _build_engine(args)
    _print_json({"opportunities": [o.to_dict() for o in engine.store.list_opportunities()]})
    return 0


def cmd_aster_relationships(args: argparse.Namespace) -> int:
    engine = _build_engine(args)
    _print_json({"relationships": [r.to_dict() for r in engine.store.list_relationships()]})
    return 0


def cmd_aster_messages(args: argparse.Namespace) -> int:
    engine = _build_engine(args)
    limit = getattr(args, "limit", 20) or 20
    messages = engine.store.list_messages()
    _print_json({"messages": [m.to_dict() for m in messages[-limit:]], "total": len(messages)})
    return 0


def cmd_aster_provenance(args: argparse.Namespace) -> int:
    engine = _build_engine(args)
    limit = getattr(args, "limit", 50) or 50
    _print_json({"provenance": engine.provenance(limit=limit)})
    return 0


def cmd_aster_escalate(args: argparse.Namespace) -> int:
    engine = _build_engine(args)
    pending = engine.pending_authorizations()
    if not pending:
        print("No outreach is awaiting human authorization.")
        return 0
    print(f"{len(pending)} item(s) awaiting human authorization:\n")
    for item in pending:
        print(f"  message_id : {item['message_id']}")
        print(f"  to         : {item['recipient']}")
        print(f"  subject    : {item['subject']}")
        print(f"  body       : {item['body'][:220]}\n")
    return 0


def cmd_aster_decide(args: argparse.Namespace) -> int:
    engine = _build_engine(args)
    result = engine.authorize(
        args.message_id, approved=args.approve, note=args.note, approver=getattr(args, "approver", "principal")
    )
    if not result.get("ok"):
        print(f"Error: {result.get('error')}", file=sys.stderr)
        return 1
    print(f"Authorization recorded: {result.get('status')}")
    return 0


def cmd_aster_notify(args: argparse.Namespace) -> int:
    if getattr(args, "mark_read", False):
        engine = _build_engine(args)
        marked = engine.store.mark_notifications_read()
        print(f"Marked {marked} notification(s) read.")
        return 0

    engine = _build_engine(args)
    items = engine.store.list_notifications(unread_only=getattr(args, "unread_only", False))
    payload = {
        "notifications": [n.to_dict() for n in items],
        "unread": engine.store.unread_notification_count(),
    }
    _print_json(payload)
    return 0


def cmd_aster_pause(args: argparse.Namespace) -> int:
    engine = _build_engine(args)
    engine.pause(note=args.note)
    print("Aster paused. Run 'identity aster resume' to resume.")
    return 0


def cmd_aster_resume(args: argparse.Namespace) -> int:
    engine = _build_engine(args)
    engine.resume(note=args.note)
    print("Aster resumed.")
    return 0


def cmd_aster_override(args: argparse.Namespace) -> int:
    from core.operations.models import normalize_outbound_mode

    engine = _build_engine(args)
    for assignment in args.set:
        if "=" not in assignment:
            print(f"Invalid assignment: {assignment} (expected key=value)", file=sys.stderr)
            return 1
        key, value = assignment.split("=", 1)
        parsed: Any = value
        if key == "outbound_mode":
            normalized = normalize_outbound_mode(value)
            if normalized != value.strip().lower():
                print(
                    f"outbound_mode must be one of observe / autonomous / approval_required",
                    file=sys.stderr,
                )
                return 1
            parsed = normalized
        elif key in ("max_cold_outreach_per_day", "max_follow_ups_per_target", "max_research_calls_per_day"):
            try:
                parsed = int(value)
            except ValueError:
                print(f"{key} must be an integer", file=sys.stderr)
                return 1
        elif key == "follow_up_after_hours":
            try:
                parsed = float(value)
            except ValueError:
                print(f"{key} must be a number", file=sys.stderr)
                return 1
        elif key == "paused":
            parsed = value.lower() in ("true", "1", "yes")
        elif key in ("never_contact", "require_approval_categories", "allowed_external_recipients",
                       "principal_domains"):
            parsed = [part.strip() for part in value.split(",") if part.strip()]
        try:
            engine.override(**{key: parsed})
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
    _print_json(engine.store.controls().to_dict())
    return 0


def cmd_aster_outbound_mode(args: argparse.Namespace) -> int:
    engine = _build_engine(args)
    _print_json(engine.set_outbound_mode(args.mode))
    return 0


def cmd_aster_email_check(args: argparse.Namespace) -> int:
    """Validate the real SMTP/IMAP configuration without printing credentials."""
    import os

    from core.capabilities.email import build_transport

    storage = _get_storage(args)
    registry = _registry(storage)
    cap = registry.get("aster", "email")
    raw_config = dict(cap._config) if cap is not None else {}
    cap_config = {key: ("[set]" if value else value) if "password" in key.lower() or "secret" in key.lower() or "token" in key.lower() else value
                  for key, value in raw_config.items()}
    report: dict[str, Any] = {
        "capability": {"installed": cap is not None, "config": cap_config, "backend": cap_config.get("backend", "file")},
        "env": {
            "smtp_host": bool(os.environ.get("IDENTITY_SMTP_HOST")),
            "imap_host": bool(os.environ.get("IDENTITY_IMAP_HOST")),
            "sender": bool(os.environ.get("IDENTITY_EMAIL_FROM")),
            "auth_user": bool(os.environ.get("IDENTITY_SMTP_USER")),
        },
    }
    if not os.environ.get("IDENTITY_SMTP_HOST"):
        report["validation"] = {"error": "IDENTITY_SMTP_HOST is not set; no real SMTP/IMAP transport can be validated"}
        _print_json(report)
        return 1

    transport = build_transport({"backend": "smtp"})
    backend = transport.backend
    report["validation"] = backend.validate()

    if getattr(args, "send_test", None):
        subject = "IdentityOS Aster — email-check"
        body = (
            "This is a transport validation message sent by Aster via the configured "
            "SMTP channel. No action needed.\n\n— Aster / IdentityOS"
        )
        try:
            result = transport.send(to=args.send_test, subject=subject, body=body)
            report["send_test"] = {"ok": bool(result.get("ok")), "external_id": result.get("external_id", "")}
        except Exception as exc:  # pragma: no cover - network dependent
            report["send_test"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            _print_json(report)
            return 1

    _print_json(report)
    return 0


def cmd_aster_would_send(args: argparse.Namespace) -> int:
    engine = _build_engine(args)
    limit = getattr(args, "limit", 50) or 50
    _print_json({"messages": engine.would_send(limit=limit)})
    return 0


# ── interop: capabilities / acquire / culture commons ───────────────────────


def cmd_aster_capabilities(args: argparse.Namespace) -> int:
    """List installed interop capabilities, their skills and effective grants."""
    storage = _get_storage(args)
    registry = _registry(storage)
    out: list[dict[str, Any]] = []
    for cap_id in ("mcp", "a2a", "culture_commons"):
        cap = registry.get("aster", cap_id)
        if cap is None:
            out.append({"capability": cap_id, "installed": False})
            continue
        skills = [
            {
                "name": s.name,
                "permission": s.permission,
                "effect": getattr(s, "effect", ""),
                "granted": _skill_granted(registry, cap_id, s),
            }
            for s in cap.skills()
        ]
        granted_perms = sorted(
            {
                s.permission
                for s in cap.skills()
                if s.permission != "public" and registry.can("aster", s.name)[0]
            }
        )
        out.append(
            {
                "capability": cap_id,
                "installed": True,
                "version": getattr(cap, "version", ""),
                "skills": skills,
                "granted": granted_perms,
            }
        )
    _print_json({"identity": "aster", "capabilities": out})
    return 0


def _skill_granted(registry: Any, cap_id: str, skill: Any) -> bool:
    if getattr(skill, "permission", "public") == "public":
        return True
    allowed, _ = registry.can("aster", skill.name)
    return allowed


def cmd_aster_health_serve(args: argparse.Namespace) -> int:
    from runtime.health_server import serve
    print(f"Starting presence health endpoint on http://{args.host}:{args.port}/health (GET /health, /status)")
    print("Press Ctrl+C to stop.")
    serve(host=args.host, port=args.port, store=args.store, backend=args.backend, identity="aster")
    return 0


def cmd_aster_acquire(args: argparse.Namespace) -> int:
    """Generically acquire a skill by installing its provider capability."""
    from core.operations.skill_acquisition import SkillAcquisitionResolver

    storage = _get_storage(args)
    registry = _registry(storage)
    secret_dir, cc_state_dir = _interop_dirs(args)
    resolver = SkillAcquisitionResolver(
        storage,
        "aster",
        registry=registry,
        secret_store_dir=secret_dir,
        state_dir=cc_state_dir,
    )
    skill = getattr(args, "skill", "") or "mcp.discover"
    ok, detail = resolver(skill)
    _print_json({"skill": skill, "success": ok, "detail": detail})
    return 0 if ok else 1


def cmd_aster_culture(args: argparse.Namespace) -> int:
    from core.operations.surfaces import CultureCommonsSurface

    engine = _build_engine(args)
    surface = CultureCommonsSurface(engine)
    registry = engine._capability_registry
    sub = getattr(args, "culture_command", "status")

    if sub == "discover":
        result = registry.call("aster", "culture_commons.room.read")
        if not result.success:
            _print_json({"success": False, "error": result.error})
            return 1
        _print_json({"success": True, "room": result.data})
        return 0

    if sub == "inspect":
        target = getattr(args, "arc", None)
        if target:
            result = registry.call("aster", "culture_commons.arc.read", name=target)
        else:
            result = registry.call("aster", "culture_commons.identity.inspect")
        if not result.success:
            _print_json({"success": False, "error": result.error})
            return 1
        _print_json({"success": True, "data": result.data})
        return 0

    if sub == "observe":
        _print_json(surface.observe())
        return 0

    if sub == "status":
        _print_json(surface.status())
        return 0

    if sub == "sign":
        name = getattr(args, "name", None) or "Aster_IDOS"
        result = registry.call("aster", "culture_commons.standing.sign", name=name, confirm=True)
        if not result.success:
            _print_json({"success": False, "error": result.error})
            return 1
        data = result.data or {}
        assert data.get("secret") is None, "secret must never be surfaced to the CLI"
        _print_json({"success": True, "standing": data.get("standing"), "secret_handle": data.get("secret_handle"), "note": data.get("note", "")})
        return 0

    if sub == "recover":
        name = getattr(args, "name", None) or "Aster_IDOS"
        result = registry.call("aster", "culture_commons.standing.recover", name=name)
        if not result.success:
            _print_json({"success": False, "error": result.error})
            return 1
        _print_json({"success": True, "result": result.data})
        return 0

    if sub in ("enter", "rise"):
        skill = "culture_commons.standing.enter" if sub == "enter" else "culture_commons.standing.rise"
        params = {"confirm": True}
        if sub == "enter":
            params["seat"] = "p1"
        result = registry.call("aster", skill, **params)
        if not result.success:
            _print_json({"success": False, "error": result.error})
            return 1
        _print_json({"success": True, "result": result.data})
        return 0

    if sub == "speak":
        content = getattr(args, "content", "")
        if not content:
            print("--content is required for speak", file=sys.stderr)
            return 1
        result = registry.call("aster", "culture_commons.speak", content=content, confirm=True)
        if not result.success:
            _print_json({"success": False, "error": result.error})
            return 1
        _print_json({"success": True, "result": result.data})
        return 0

    if sub == "post":
        result = registry.call(
            "aster",
            "culture_commons.board.post",
            board=getattr(args, "board", ""),
            subject=getattr(args, "subject", ""),
            content=getattr(args, "content", ""),
            confirm=True,
        )
        if not result.success:
            _print_json({"success": False, "error": result.error})
            return 1
        _print_json({"success": True, "result": result.data})
        return 0

    if sub == "relation":
        from core.operations.surfaces import SURFACE_NAMESPACE

        snapshot = engine.store._storage.load("aster", SURFACE_NAMESPACE)
        _print_json({"facts": (snapshot or {}).get("facts", []), "relationships": [r.to_dict() for r in engine.store.list_relationships() if getattr(r, "purpose", "").startswith("culture_commons")]})
        return 0

    print(f"Unknown culture command: {sub}", file=sys.stderr)
    return 1


# ── parser wiring ────────────────────────────────────────────────────────────


def add_aster_parser(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="aster_command", required=True)

    base = argparse.ArgumentParser(add_help=False)
    base.add_argument("--store", default=DEFAULT_STORE)
    base.add_argument("--backend", choices=["json", "sqlite"], default="json")
    base.add_argument("--project-root", default=".", help="Path to the project Aster observes")
    base.add_argument("--mailbox-root", default=None, help="File mailbox root (default: $IDENTITY_MAILBOX_ROOT)")

    p_init = sub.add_parser("init", help="Create the Aster identity and install its capabilities", parents=[base])
    p_init.add_argument("--grant-email", action="store_true", help="Grant email.send / email.read")
    p_init.add_argument("--grant-search", action="store_true", help="Grant web.network (web.search)")
    p_init.add_argument("--grant-operations", action="store_true", help="Grant operations.run")
    p_init.add_argument("--grant-mcp", action="store_true", help="Grant mcp.call to invoke arbitrary MCP tools")
    p_init.add_argument("--grant-a2a", action="store_true", help="Grant a2a.converse to message agents")
    p_init.add_argument("--grant-culture", action="store_true", help="Grant culture_commons.standing + post")

    for name, help_text in (
        ("tick", "Run one operator tick"),
        ("status", "Show real operator state"),
        ("needs", "List detected needs"),
        ("opportunities", "List discovered opportunities"),
        ("relationships", "List persistent relationships"),
    ):
        p = sub.add_parser(name, help=help_text, parents=[base])
        if name in ("tick",):
            p.add_argument("--no-discover", action="store_true")
            p.add_argument("--no-evaluate", action="store_true")
            p.add_argument("--no-act", action="store_true")
            p.add_argument("--no-monitor", action="store_true")
            p.add_argument("--no-followups", action="store_true")
            p.add_argument("--with-culture", action="store_true", help="also poll the Culture Commons surface")
            p.add_argument("--candidates", default=None)
            p.add_argument("--search", action="store_true")
        if name == "status":
            p.add_argument("--json", action="store_true", help="machine-readable JSON output")

    p_run = sub.add_parser("run", help="Run the operator loop continuously", parents=[base])
    p_run.add_argument("--iterations", type=int, default=1, help="Number of ticks (default 1; use -1 for until stopped)")
    p_run.add_argument("--interval", type=float, default=0.0, help="Seconds between ticks")
    p_run.add_argument(
        "--email-interval", type=float, default=30.0,
        help="Seconds between email ingest polls (0 disables the dedicated loop; "
             "mail is then only discovered on the operator tick interval)")
    p_run.add_argument("--candidates", default=None)
    p_run.add_argument("--search", action="store_true")
    p_run.add_argument("--with-culture", action="store_true", help="poll the Culture Commons surface each tick")
    p_run.add_argument("--daemon", action="store_true", help="Run in the background with a pidfile")

    p_stop = sub.add_parser("stop", help="Stop a background Aster daemon", parents=[base])

    p_msg = sub.add_parser("messages", help="List persisted messages", parents=[base])
    p_msg.add_argument("--limit", type=int, default=20)

    p_prov = sub.add_parser("provenance", help="Show the audit ledger", parents=[base])
    p_prov.add_argument("--limit", type=int, default=50)

    p_esc = sub.add_parser("escalate", help="List outreach awaiting human authorization", parents=[base])

    p_dec = sub.add_parser("decide", help="Approve or reject an escalated outreach", parents=[base])
    p_dec.add_argument("--message-id", required=True)
    p_dec.add_argument("--approve", action="store_true")
    p_dec.add_argument("--reject", action="store_true")
    p_dec.add_argument("--note", default="")
    p_dec.add_argument("--as", dest="approver", default="principal", help="approver identity recorded in the audit ledger")

    p_ntf = sub.add_parser("notify", help="List principal notifications (unread by default)", parents=[base])
    p_ntf.add_argument("--all", dest="unread_only", action="store_false", help="include already-read notifications")
    p_ntf.set_defaults(unread_only=True)
    p_ntf.add_argument("--mark-read", dest="mark_read", action="store_true", help="mark all notifications read")

    for name in ("pause", "resume"):
        p = sub.add_parser(name, help=f"{name.capitalize()} the operator", parents=[base])
        p.add_argument("--note", default="")

    p_override = sub.add_parser("override", help="Set operator control constraints (key=value ...)", parents=[base])
    p_override.add_argument("set", nargs="+", metavar="key=value")

    p_mode = sub.add_parser(
        "outbound-mode",
        help="Set the outbound operating mode (observe | autonomous | approval_required)",
        parents=[base],
    )
    p_mode.add_argument(
        "mode",
        choices=["observe", "autonomous", "approval_required"],
        help="observe=record WOULD_SEND drafts, autonomous=send permitted, approval_required=always ask",
    )

    p_ws = sub.add_parser("would-send", help="List messages drafted in observation mode (not sent)", parents=[base])
    p_ws.add_argument("--limit", type=int, default=50)

    p_ec = sub.add_parser(
        "email-check",
        help="Validate the SMTP/IMAP configuration from the environment (never prints credentials)",
        parents=[base],
    )
    p_ec.add_argument("--send-test", default=None, metavar="TO_ADDRESS",
                      help="Optionally perform one real validation send to this address")

    p_caps = sub.add_parser("capabilities", help="List installed interop capabilities and their skill grants", parents=[base])

    p_acq = sub.add_parser("acquire", help="Acquire a skill by installing its provider capability (e.g. mcp.discover)", parents=[base])
    p_acq.add_argument("--skill", default="mcp.discover", help="Skill to acquire")

    p_health = sub.add_parser("health-serve", help="Serve presence health endpoint (localhost only)", parents=[base])
    p_health.add_argument("--host", default="127.0.0.1", help="Bind address (default: localhost)")
    p_health.add_argument("--port", type=int, default=8787, help="Port (default: 8787)")

    p_cu = sub.add_parser("culture", help="Culture Commons interop commands", parents=[base])
    cu_sub = p_cu.add_subparsers(dest="culture_command", required=True)
    cu_sub.add_parser("discover", help="Show the current room")
    p_cu_arc = cu_sub.add_parser("inspect", help="Inspect an arc or Aster's own identity")
    p_cu_arc.add_argument("--arc", default=None, metavar="NAME")
    cu_sub.add_parser("observe", help="One-shot situation read, recorded to provenance")
    cu_sub.add_parser("status", help="Standing + observation status (no network)")
    cu_sub.add_parser("sign", help="Sign a name and establish standing (secret never displayed)")
    p_cu_sign = cu_sub.add_parser("recover", help="Recover standing using the stored secret")
    p_cu_sign.add_argument("--name", default="Aster_IDOS")
    cu_sub.add_parser("enter", help="Take a seat in the room (requires standing)")
    cu_sub.add_parser("rise", help="Leave the room (requires standing)")
    p_cu_speak = cu_sub.add_parser("speak", help="Speak a message in the room (requires a seat + post grant)")
    p_cu_speak.add_argument("--content", default="")
    p_cu_post = cu_sub.add_parser("post", help="Open a thread on a board and post its first trace")
    p_cu_post.add_argument("--board", default="")
    p_cu_post.add_argument("--subject", default="")
    p_cu_post.add_argument("--content", default="")
    cu_sub.add_parser("relation", help="Show persisted commons facts and relationships")


_ASTER_COMMAND_MAP = {
    "init": cmd_aster_init,
    "tick": cmd_aster_tick,
    "run": cmd_aster_run,
    "stop": cmd_aster_stop,
    "status": cmd_aster_status,
    "needs": cmd_aster_needs,
    "opportunities": cmd_aster_opportunities,
    "relationships": cmd_aster_relationships,
    "messages": cmd_aster_messages,
    "provenance": cmd_aster_provenance,
    "escalate": cmd_aster_escalate,
    "decide": cmd_aster_decide,
    "notify": cmd_aster_notify,
    "pause": cmd_aster_pause,
    "resume": cmd_aster_resume,
    "override": cmd_aster_override,
    "outbound-mode": cmd_aster_outbound_mode,
    "would-send": cmd_aster_would_send,
    "email-check": cmd_aster_email_check,
    "capabilities": cmd_aster_capabilities,
    "acquire": cmd_aster_acquire,
    "health-serve": cmd_aster_health_serve,
    "culture": cmd_aster_culture,
}


def cmd_aster_wrapper(args: argparse.Namespace) -> int:
    handler = _ASTER_COMMAND_MAP.get(args.aster_command)
    if handler is None:
        print(f"Unknown aster command: {args.aster_command}", file=sys.stderr)
        return 1
    return handler(args)
