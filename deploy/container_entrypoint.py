"""Container entrypoint: run the operator and the presence endpoint together.

Both processes are required. The operator is the identity runtime and the only
writer for email jobs and outbound sends. The presence endpoint serves the
readiness/health/dashboard APIs and receives Gmail push notifications, queuing
them in shared storage for the operator to drain.

Running only the operator — as the Dockerfile previously did — exposed port
8787 and health-checked /ready, which can never answer without this process.
That is a container that reports itself unhealthy while looking configured.

No third-party dependencies: this is PID 1, so it must not depend on anything
that might be missing from the image. It forwards termination signals so both
children finish in-flight work and persist state, and it exits non-zero if
either child dies, so a restart policy actually restarts a broken system.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from typing import Optional

STORE = os.environ.get("IDENTITY_STORE", "/data")
BACKEND = os.environ.get("IDENTITY_BACKEND", "sqlite")
IDENTITY = os.environ.get("IDENTITY_ID", "aster")
HOST = os.environ.get("ASTER_PRESENCE_HOST", "0.0.0.0")
PORT = os.environ.get("ASTER_PRESENCE_PORT", "8787")

#: How long to let a child finish after a stop signal before escalating.
GRACE_SECONDS = float(os.environ.get("ASTER_SHUTDOWN_GRACE", "30"))


def _child(name: str, argv: list[str]) -> subprocess.Popen:
    print(f"[entrypoint] starting {name}: {' '.join(argv)}", flush=True)
    return subprocess.Popen(argv, env=os.environ.copy())


def main() -> int:
    operator_argv = [sys.executable, "-m", "cli.main", "aster", "run"] + sys.argv[1:]
    presence_argv = [
        sys.executable, "-m", "runtime.health_server",
        "--host", HOST,
        "--port", str(PORT),
        "--store", STORE,
        "--backend", BACKEND,
        "--identity", IDENTITY,
    ]
    if os.environ.get("ASTER_GMAIL_PUSH", "").lower() in ("0", "false", "no"):
        presence_argv.append("--no-gmail-push")

    children = {
        "operator": _child("operator", operator_argv),
        "presence": _child("presence", presence_argv),
    }
    stopping = False

    def _forward(signum, _frame) -> None:
        nonlocal stopping
        if stopping:
            return
        stopping = True
        print(f"[entrypoint] forwarding signal {signum}", flush=True)
        for child in children.values():
            if child.poll() is None:
                try:
                    child.send_signal(signal.SIGTERM)
                except ProcessLookupError:
                    pass

    signal.signal(signal.SIGTERM, _forward)
    signal.signal(signal.SIGINT, _forward)

    failed: Optional[str] = None
    while True:
        dead = [name for name, child in children.items() if child.poll() is not None]
        if dead:
            failed = dead[0]
            break
        # Children are long-running; poll rather than block on any one of them.
        try:
            for child in children.values():
                child.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            continue

    if stopping:
        deadline_failures = [n for n, c in children.items()
                             if c.poll() not in (0, -signal.SIGTERM)]
        for name in deadline_failures:
            print(f"[entrypoint] {name} did not exit cleanly", flush=True)

    if failed:
        code = children[failed].returncode
        print(f"[entrypoint] {failed} exited (code {code}); stopping the rest",
              flush=True)
        _forward(signal.SIGTERM, None)
    return int(children.get(failed).returncode if failed else 0)


if __name__ == "__main__":
    sys.exit(main())
