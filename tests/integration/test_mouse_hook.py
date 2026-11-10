"""The real low-level mouse hook (helper process) on this Windows machine.

Synthetic input (``SendInput``) goes only into a test window of this process: the
pointer is moved over it first and put back afterwards, so nothing else is clicked.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(sys.platform != "win32", reason="Windows low-level hook"),
]

os.environ.setdefault("QT_QPA_PLATFORM", "windows")


def _send(events: list[tuple[int, int, int, int]]) -> None:
    """``(flags, data, x, y)``: every input also moves the pointer to x/y (virtual desktop
    pixels), so a press always lands on the test window even if the real mouse moved."""
    import ctypes
    from ctypes import wintypes

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [
            ("dx", wintypes.LONG),
            ("dy", wintypes.LONG),
            ("mouseData", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.c_size_t),
        ]

    class INPUT(ctypes.Structure):
        class _U(ctypes.Union):
            _fields_ = [("mi", MOUSEINPUT), ("pad", ctypes.c_byte * 32)]

        _anonymous_ = ("u",)
        _fields_ = [("type", wintypes.DWORD), ("u", _U)]

    user32 = ctypes.windll.user32
    vx, vy = user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)
    vw, vh = user32.GetSystemMetrics(78), user32.GetSystemMetrics(79)
    for flags, data, x, y in events:
        inp = INPUT(type=0)
        inp.mi.dx = round((x - vx) * 65535 / max(1, vw - 1))
        inp.mi.dy = round((y - vy) * 65535 / max(1, vh - 1))
        inp.mi.dwFlags = flags | MOVE | 0x8000 | 0x4000  # absolute, virtual desktop
        inp.mi.mouseData = data & 0xFFFFFFFF
        user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
        time.sleep(0.015)


MOVE, LDOWN, LUP, RDOWN, RUP, XDOWN, XUP, WHEEL, HWHEEL = (
    0x0001,
    0x0002,
    0x0004,
    0x0008,
    0x0010,
    0x0080,
    0x0100,
    0x0800,
    0x1000,
)


def test_real_hook_records_clicks_drag_wheel_and_x_buttons(qtbot, tmp_path: Path):
    import ctypes
    from ctypes import wintypes

    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QWidget

    from video_story.capture.input_recorder import InputRecorder
    from video_story.core.input_events import read_mouse_log
    from video_story.core.mouse_actions import derive_actions
    from video_story.core.screens import assign_screen_ids
    from video_story.core.timeline import RecordingClock
    from video_story.platform.windows import WindowsPlatform

    platform = WindowsPlatform()
    platform.prepare_process()
    screens = assign_screen_ids(platform.list_screens())
    source = platform.mouse_source()
    assert source is not None

    window = QWidget()
    window.setWindowTitle("Video Story mouse hook test")
    window.setWindowFlags(window.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)
    window.resize(500, 300)
    qtbot.addWidget(window)
    window.show()
    qtbot.waitExposed(window)
    rect = wintypes.RECT()
    ctypes.windll.user32.GetWindowRect(int(window.winId()), ctypes.byref(rect))
    cx, cy = rect.left + 150, rect.top + 150

    old = wintypes.POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(old))
    clock = RecordingClock()
    clock.start()
    rec = InputRecorder(tmp_path / "input" / "mouse.jsonl", screens, source, clock)
    started = time.monotonic()
    rec.start()
    startup_s = time.monotonic() - started
    try:
        at = (cx, cy)
        _send([(MOVE, 0, *at)])
        qtbot.wait(100)
        _send([(LDOWN, 0, *at), (LUP, 0, *at)])  # click
        qtbot.wait(700)
        _send([(LDOWN, 0, *at), (LUP, 0, *at), (LDOWN, 0, *at), (LUP, 0, *at)])  # double
        qtbot.wait(700)
        path = [(MOVE, 0, cx + i * 20, cy) for i in range(1, 11)]
        _send([(LDOWN, 0, *at), *path, (LUP, 0, cx + 200, cy)])  # drag
        qtbot.wait(700)
        _send([(RDOWN, 0, *at), (RUP, 0, *at)])
        qtbot.wait(700)
        _send([(WHEEL, -120, *at), (WHEEL, -120, *at), (HWHEEL, 60, *at)])
        qtbot.wait(700)
        _send([(XDOWN, 1, *at), (XUP, 1, *at)])
        qtbot.wait(700)
        _send([(XDOWN, 2, *at), (XUP, 2, *at)])
        qtbot.wait(500)
    finally:
        ctypes.windll.user32.SetCursorPos(old.x, old.y)
        info = rec.stop()

    log = read_mouse_log(rec.path)
    actions = derive_actions(log, info.params)
    kinds = [(a.kind, a.button or a.axis) for a in actions]
    assert kinds == [
        ("click", "left"),
        ("double_click", "left"),
        ("drag", "left"),
        ("click", "right"),
        ("scroll", "v"),
        ("scroll", "h"),
        ("click", "x1"),
        ("click", "x2"),
    ], kinds
    assert all(a.injected for a in actions)
    assert all(a.target == "video_story" for a in actions)  # the test window is ours
    first = actions[0]
    assert (first.at.gx, first.at.gy) == (cx, cy)
    drag = actions[2]
    assert abs(drag.end.gx - (cx + 200)) <= 2 and drag.length_px >= 190
    assert actions[4].notches == 2.0 and actions[5].direction == "right"
    assert info.events >= 16 and info.hook_restarts == 0 and info.error is None
    assert startup_s < 10
