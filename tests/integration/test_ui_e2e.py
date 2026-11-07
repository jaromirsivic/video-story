"""End-to-end test of the real control window on the real desktop.

Runs the window in-process (no detached process), presses Record, draws a rectangle on
the primary screen through the overlay, stops with "Process Recording" and waits for
the real transcription. Run it alone (not with the offscreen unit tests):

    VIDEO_STORY_INTEGRATION=1 uv run pytest tests/integration/test_ui_e2e.py -q -s
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def qapp_real():
    os.environ.pop("QT_QPA_PLATFORM", None)
    from video_story.platform import get_platform

    get_platform().prepare_process()
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


def _wait(app, ms: int) -> None:
    end = time.monotonic() + ms / 1000
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)


def test_record_draw_process(qapp_real, tmp_path: Path, monkeypatch):
    from PySide6.QtCore import QPointF
    from PySide6.QtGui import QColor, QGuiApplication
    from PySide6.QtWidgets import QDialog

    from video_story.core.config import Config
    from video_story.core.manifest import read_manifest
    from video_story.core.session import SessionPaths, Status, new_session_id, read_state
    from video_story.host.controller import SessionController
    from video_story.host.runner import frames_at
    from video_story.platform import get_platform
    from video_story.ui import control_window as cw
    from video_story.ui.overlay import DrawingOverlay, Shape, Tool
    from video_story.ui.stop_dialog import StopChoice

    config = Config(recording_folder=tmp_path, capture_interval_ms=250)
    paths = SessionPaths(tmp_path, new_session_id(tmp_path))
    controller = SessionController(config, get_platform(), paths)
    finished: list[str] = []
    overlays: list[DrawingOverlay] = []

    def overlay_factory():
        overlay = DrawingOverlay()
        overlays.append(overlay)
        return overlay

    window = cw.ControlWindow(
        controller, on_finished=finished.append, overlay_factory=overlay_factory
    )
    window.show()
    _wait(qapp_real, 300)
    assert read_state(paths.state_path).status is Status.READY

    # Record for a moment, then draw a red rectangle on the primary screen.
    window.record_button.click()
    assert controller.recording
    _wait(qapp_real, 1200)
    window.draw_button.setChecked(True)
    _wait(qapp_real, 300)
    assert overlays and overlays[0].is_visible()
    primary = QGuiApplication.primaryScreen()
    rect_logical = (200, 300, 500, 450)
    overlays[0].add_shape(
        Shape(
            tool=Tool.RECTANGLE,
            color=QColor("#FF0000"),
            points=[
                QPointF(rect_logical[0], rect_logical[1]),
                QPointF(rect_logical[2], rect_logical[3]),
            ],
            screen_index=0,
        )
    )
    draw_time_ms = controller.elapsed_ms
    _wait(qapp_real, 900)
    end_of_drawing_ms = controller.elapsed_ms

    # Stop -> Process Recording (dialog answered programmatically).
    def fake_exec(self):
        self.choice = StopChoice.PROCESS
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(cw.StopDialog, "exec", fake_exec)
    window.record_button.click()
    deadline = time.monotonic() + 180
    while not finished and time.monotonic() < deadline:
        _wait(qapp_real, 100)
    assert finished == ["processed"], finished

    state = read_state(paths.state_path)
    assert state.status is Status.PROCESSED and state.recording_dir == str(paths.final_dir)
    final = paths.final_dir
    manifest = read_manifest(paths.manifest_path(final))
    assert manifest.transcript is not None and manifest.frames.count > 0
    assert paths.transcript_txt_path(final).exists() and paths.transcript_json_path(final).exists()
    assert (
        json.loads(paths.transcript_json_path(final).read_text(encoding="utf-8"))["segments"]
        is not None
    )

    # The rectangle drawn through the overlay is visible in a frame taken while it was shown.
    probe_ms = (draw_time_ms + end_of_drawing_ms) // 2 + 200
    result = frames_at(paths, probe_ms, screen="screen1")
    frame = result["frames"][0]
    assert draw_time_ms <= frame["time_ms"] <= end_of_drawing_ms + 300
    from PIL import Image

    image = Image.open(frame["image"]).convert("RGB")
    dpr = primary.devicePixelRatio()
    x = int((rect_logical[0] + rect_logical[2]) / 2 * dpr)
    y_top = int(rect_logical[1] * dpr)
    reds = 0
    for dy in range(-6, 7):
        r, g, b = image.getpixel((x, y_top + dy))
        if r > 180 and g < 90 and b < 90:
            reds += 1
    assert reds >= 2, f"expected red rectangle edge near ({x}, {y_top}) in {frame['image']}"
    assert frame["cursor"] is None or frame["cursor"]["screen"].startswith("screen")

    # The same frame's sidecar describes the rectangle in pixels of the screen1 image.
    rects = [d for d in frame["drawings"] if d["tool"] == "rectangle"]
    assert len(rects) == 1, frame["drawings"]
    rect = rects[0]
    assert rect["color"] == "#FF0000" and rect["screen"] == "screen1"
    assert rect["from"] == {"screen": "screen1", "x": round(200 * dpr), "y": round(300 * dpr)}
    assert rect["to"] == {"screen": "screen1", "x": round(500 * dpr), "y": round(450 * dpr)}
    # Every screen's sidecar of that tick carries the drawings, not only screen1.
    for other in frames_at(paths, frame["time_ms"], screen="all")["frames"]:
        assert any(d["tool"] == "rectangle" for d in other["drawings"]), other
