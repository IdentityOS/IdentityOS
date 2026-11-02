"""Concurrency contract for synchronous adapter wall-clock deadlines."""

from __future__ import annotations

import threading
import time

import pytest

from adapters.deadline import require_time_remaining, run_with_wall_clock_deadline


def _generation_threads() -> list[threading.Thread]:
    return [
        thread for thread in threading.enumerate()
        if thread.name.startswith("identityos-generation")
    ]


def test_normal_completion_returns_value_and_joins_worker():
    before = len(_generation_threads())

    assert run_with_wall_clock_deadline(lambda: "done", 1) == "done"

    assert len(_generation_threads()) == before


def test_original_exception_type_and_instance_propagate():
    expected = ValueError("provider rejected request")

    def fail():
        raise expected

    with pytest.raises(ValueError) as observed:
        run_with_wall_clock_deadline(fail, 1)

    assert observed.value is expected


def test_timeout_returns_on_budget_and_cleanup_releases_daemon_worker():
    release = threading.Event()
    cleanup_called = threading.Event()
    finished = threading.Event()

    def blocked():
        release.wait()
        finished.set()

    def cleanup():
        cleanup_called.set()
        release.set()

    started = time.monotonic()
    with pytest.raises(TimeoutError, match="generation deadline reached after 0.1s"):
        run_with_wall_clock_deadline(blocked, 0.1, on_timeout=cleanup)
    elapsed = time.monotonic() - started

    assert 0.08 <= elapsed < 0.35
    assert cleanup_called.wait(0.2)
    assert finished.wait(0.2)
    for _ in range(20):
        if not _generation_threads():
            break
        time.sleep(0.01)
    assert not _generation_threads()


def test_deadline_guard_blocks_late_tool_boundary():
    with pytest.raises(TimeoutError, match="generation deadline reached"):
        require_time_remaining(time.monotonic() - 0.001)

    require_time_remaining(time.monotonic() + 1)
