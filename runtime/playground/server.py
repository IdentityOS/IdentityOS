"""
IdentityOS Playground Server.

Launches the Playground at http://localhost:<port>/playground

Usage:
    python -m runtime.playground              # port 8000
    python -m runtime.playground --port 8001  # custom port
    python -m runtime.playground --host 127.0.0.1 --port 8000
"""

from __future__ import annotations

import sys

import uvicorn


def main() -> None:
    host = "127.0.0.1"
    port = 8000

    args = iter(sys.argv[1:])
    for a in args:
        if a == "--port":
            port = int(next(args, "8000"))
        elif a == "--host":
            host = next(args, "127.0.0.1")

    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit(
            "The playground is local-only by default. Use a separately authenticated "
            "reverse proxy for remote access."
        )

    print("  \u25B6 IdentityOS Playground")
    print("  \u2500" * 40)
    print(f"  Open http://localhost:{port}/onboarding")
    print()
    uvicorn.run(
        "runtime.playground.app:app",
        host=host,
        port=port,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
