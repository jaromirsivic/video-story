"""A real recording with the mouse, end to end: hook, UI Automation, file, timeline.

The target window runs in a process of its own, so it counts as another application
(clicks into Video Story's own windows are left out of the timeline). Synthetic input
goes only into that window: every input carries its position.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(sys.platform != "win32", reason="Windows mouse and UI Automation"),
]

TARGET = textwrap.dedent(
    """
    import sys
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtWidgets import QApplication, QLineEdit, QPushButton, QVBoxLayout, QWidget
    app = QApplication(sys.argv)
    w = QWidget()
    w.setWindowTitle("Video Story e2e target")
    w.setWindowFlags(w.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)
    layout = QVBoxLayout(w)
    field = QLineEdit("hello e2e")
    field.setAccessibleName("Search")
    button = QPushButton("Save")
    layout.addWidget(field)
    layout.addWidget(button)
    w.resize(360, 160)
    w.show()
    def report():
        def center(widget):
            g = widget.mapToGlobal(widget.rect().center())
            r = widget.devicePixelRatioF()
            return [round(g.x() * r), round(g.y() * r)]
        (fx, fy), (bx, by) = center(field), center(button)
        print("READY", fx, fy, bx, by, flush=True)
    QTimer.singleShot(500, report)
    QTimer.singleShot(60000, app.quit)
    app.exec()
    """
)


def test_a_recording_lists_the_clicks_with_their_elements(tmp_path: Path):
    import ctypes
    from ctypes import wintypes

    from test_mouse_hook import LDOWN, LUP, MOVE, WHEEL, _send

    from video_story.capture.recorder import Recorder
    from video_story.core.config import Config
    from video_story.core.manifest import write_manifest
    from video_story.core.session import SessionPaths
    from video_story.host.events import build_mouse_detail, build_timeline
    from video_story.platform.windows import WindowsPlatform

    target = subprocess.Popen(
        [sys.executable, "-c", TARGET],
        stdout=subprocess.PIPE,
        text=True,
        creationflags=0x08000000,
    )
    old = wintypes.POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(old))
    try:
        ready = target.stdout.readline().split()
        assert ready and ready[0] == "READY", ready
        field = (int(ready[1]), int(ready[2]))
        button = (int(ready[3]), int(ready[4]))

        cfg = Config(recording_folder=tmp_path, capture_interval_ms=250, image_format="jpeg")
        platform = WindowsPlatform()
        paths = SessionPaths(tmp_path, "20261110-200000")
        paths.create_work_dirs()
        recorder = Recorder(cfg, platform, paths, record_audio=False)
        recorder.start()
        time.sleep(3)  # hook and UI Automation helpers start in the background
        _send([(MOVE, 0, *field), (LDOWN, 0, *field), (LUP, 0, *field)])
        time.sleep(0.8)
        _send([(MOVE, 0, *button), (LDOWN, 0, *button), (LUP, 0, *button)])
        time.sleep(0.8)
        _send([(WHEEL, -120, *button), (WHEEL, -120, *button)])
        time.sleep(1.5)
        summary = recorder.stop()
        write_manifest(paths.manifest_path(paths.work_dir), summary.to_manifest(cfg, platform.name))
        paths.finalize()
    finally:
        ctypes.windll.user32.SetCursorPos(old.x, old.y)
        target.kill()

    assert summary.mouse is not None and summary.mouse.error is None
    assert summary.mouse.elements >= 2 and summary.mouse.capabilities.elements
    result = build_timeline(paths)
    mouse = [
        line for line in result["timeline"].splitlines() if "CLICK" in line or "SCROLL" in line
    ]
    assert len(mouse) == 3, mouse
    assert ' on edit "Search"' in mouse[0] and 'value "hello e2e"' in mouse[0]
    assert 'in "Video Story e2e target" [python' in mouse[0] and "(injected)" in mouse[0]
    assert ' on button "Save"' in mouse[1] and "frames before" in mouse[1]
    assert not any('in window "' in line for line in mouse)  # the window part names it once
    assert "SCROLL down 2 notches" in mouse[2]
    detail = build_mouse_detail(paths, at_ms=int(mouse[0].split()[0]))
    element = detail["actions"][0]["element"]
    assert element["content"]["text"] == "hello e2e" and element["bounds"]["width"] > 0
    print(json.dumps(mouse, ensure_ascii=False, indent=1))
