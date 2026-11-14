"""WindowsMouseSource with stand-in helper processes (no hook is installed here)."""

from __future__ import annotations

import sys
import textwrap
import threading
import time

import pytest

from video_story.core.timeline import RecordingClock
from video_story.platform.base import PlatformError

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows mouse source")


def _script(body: str) -> list[str]:
    return [sys.executable, "-c", textwrap.dedent(body)]


ECHO = """
import os, sys, time
print("READY", os.getpid(), flush=True)
now = time.perf_counter_ns()
print(now, 513, 10, 20, 0, 0, 1, flush=True)            # left down, Ctrl
print(now + 5_000_000, 514, 10, 20, 0, 1, 0, flush=True)  # left up, injected
print(now + 6_000_000, 522, 10, 20, -120, 0, 0, flush=True)
print(now + 7_000_000, 524, 10, 20, 2, 0, 0, flush=True)  # X2 up
sys.stdin.read()                                          # until the parent closes stdin
"""


def _collect(source, clock=None):
    from video_story.platform.windows_input import WindowsMouseSource  # noqa: F401

    events, statuses = [], []
    clock = clock or RecordingClock()
    if not clock.running:
        clock.start()
    source.start(events.append, clock, lambda s, r: statuses.append((s, r)))
    return events, statuses


def _wait(condition, timeout=10.0):
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "condition not met in time"
        time.sleep(0.02)


def test_helper_lines_become_events_on_the_recording_clock():
    from video_story.platform.windows_input import WindowsMouseSource

    source = WindowsMouseSource(command=lambda: _script(ECHO), cursor=lambda: (0, 0))
    events, statuses = _collect(source)
    _wait(lambda: len(events) == 4)
    assert source.healthy()
    source.stop()
    assert not source.healthy()
    down, up, wheel, x2 = events
    assert (down.kind, down.button, down.modifiers, down.gx, down.gy) == (
        "down",
        "left",
        ("ctrl",),
        10,
        20,
    )
    assert up.injected and up.t_ms - down.t_ms == 5
    assert (wheel.kind, wheel.delta, wheel.axis) == ("wheel", -120, "v")
    assert (x2.kind, x2.button) == ("up", "x2")
    assert down.t_ms >= 0 and statuses == []
    assert source.params.double_click_ms > 0 and source.params.drag_px[0] > 0


def test_a_helper_that_cannot_hook_is_reported():
    from video_story.platform.windows_input import WindowsMouseSource

    source = WindowsMouseSource(
        command=lambda: _script('print("ERROR SetWindowsHookExW failed (error 5)", flush=True)')
    )
    with pytest.raises(PlatformError, match="error 5"):
        _collect(source)
    assert source.probe() is not None and "error 5" in source.probe()


def test_a_helper_that_does_not_answer_times_out(monkeypatch):
    from video_story.platform import windows_input

    monkeypatch.setattr(windows_input, "READY_TIMEOUT_S", 0.5)
    source = windows_input.WindowsMouseSource(
        command=lambda: _script("import sys; sys.stdin.read()")
    )
    with pytest.raises(PlatformError, match="no answer"):
        _collect(source)


def test_a_helper_that_ends_is_restarted_then_given_up(monkeypatch):
    from video_story.platform import windows_input

    monkeypatch.setattr(windows_input, "WATCH_INTERVAL_S", 0.05)
    starts = []

    def command():
        starts.append(1)
        return _script('import os; print("READY", os.getpid(), flush=True)')  # ends at once

    source = windows_input.WindowsMouseSource(
        command=command, max_restarts=2, cursor=lambda: (0, 0)
    )
    _, statuses = _collect(source)
    _wait(lambda: any(s == "gave_up" for s, _ in statuses))
    source.stop()
    assert [s for s, _ in statuses] == ["restarted", "restarted", "gave_up"]
    assert len(starts) == 3 and source.restarts == 2 and not source.healthy()
    assert "exit code" in statuses[0][1]


def test_a_silent_hook_while_the_pointer_moves_is_restarted(monkeypatch):
    from video_story.platform import windows_input

    monkeypatch.setattr(windows_input, "WATCH_INTERVAL_S", 0.05)
    pos = {"x": 0}
    lock = threading.Lock()

    def moving_cursor():
        with lock:
            pos["x"] += 1
            return pos["x"], 0

    silent = 'import os, sys; print("READY", os.getpid(), flush=True); sys.stdin.read()'
    source = windows_input.WindowsMouseSource(
        command=lambda: _script(silent), stall_s=0.3, max_restarts=1, cursor=moving_cursor
    )
    _, statuses = _collect(source)
    _wait(lambda: len(statuses) >= 1)
    source.stop()
    assert statuses[0][0] == "restarted" and "pointer moved" in statuses[0][1]


def test_the_helper_command_uses_this_python_and_no_fixed_path():
    from video_story.platform.windows_input import hook_command

    command = hook_command()
    assert command[-2:] == ["-m", "video_story.platform.windows_mouse_hook"]
    assert command[0].lower().endswith(("python.exe", "pythonw.exe", "python"))
