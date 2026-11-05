"""Caller-visible wall-clock deadlines for synchronous model adapters.

Transport timeouts are phase or inactivity bounds: a provider that trickles
bytes can keep a synchronous request alive indefinitely. This helper makes the
calling thread authoritative without changing adapters that do not request a
generation budget.
"""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable
from typing import Any, Optional


class GenerationDeadlineError(TimeoutError, RuntimeError):
    """A total-generation timeout compatible with existing adapter contracts.

    ``TimeoutError`` lets direct callers distinguish a wall-clock expiry, while
    ``RuntimeError`` preserves the historical adapter/chain failure contract.
    """


def run_with_wall_clock_deadline(
    operation: Callable[[], Any],
    budget: float,
    *,
    on_timeout: Optional[Callable[[], None]] = None,
) -> Any:
    """Run ``operation`` while guaranteeing a bounded wait for its caller.

    Python cannot safely kill a thread blocked inside a synchronous SDK. The
    worker is therefore daemonized, and ``on_timeout`` should detach/close the
    active transport so it exits promptly. Adapter code must also re-check its
    monotonic deadline before invoking any tool callback; this prevents a late
    provider response from producing effects after the caller timed out.

    Successful values and original exceptions are relayed unchanged. A timeout
    callback is itself daemonized so transport cleanup cannot extend the public
    deadline. Workers that complete normally are joined before returning.
    """
    seconds = max(0.1, float(budget))
    result: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)

    def invoke() -> None:
        try:
            result.put((True, operation()), block=False)
        except BaseException as exc:  # preserve the provider exception type
            result.put((False, exc), block=False)

    worker = threading.Thread(
        target=invoke,
        daemon=True,
        name="identityos-generation",
    )
    worker.start()
    try:
        succeeded, value = result.get(timeout=seconds)
    except queue.Empty as exc:
        if on_timeout is not None:
            threading.Thread(
                target=on_timeout,
                daemon=True,
                name="identityos-generation-abort",
            ).start()
        raise GenerationDeadlineError(
            f"generation deadline reached after {seconds:g}s"
        ) from exc

    # The queue handoff occurs immediately before the worker returns. Joining
    # removes even that short-lived thread from deterministic test/process state.
    worker.join(timeout=0.05)
    if succeeded:
        return value
    raise value


def require_time_remaining(deadline: Optional[float]) -> None:
    """Reject late work before it can cross into a tool side-effect boundary."""
    if deadline is not None and time.monotonic() >= deadline:
        raise GenerationDeadlineError("generation deadline reached")
