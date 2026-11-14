"""Real UI Automation (helper process) against a test window of this process.

The window has a text field, a password field, a multi-line text and a button. The
inspector is asked directly, then the whole chain (hook -> recorder -> inspector) runs
with synthetic clicks that go only into this window.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(sys.platform != "win32", reason="Windows UI Automation"),
]

os.environ.setdefault("QT_QPA_PLATFORM", "windows")

FIELD = "hello field"
SECRET = "secret-123"
PARAGRAPHS = "First paragraph of the notes.\nSecond paragraph, the one under the mouse."


@pytest.fixture
def window(qtbot):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QLineEdit, QPlainTextEdit, QPushButton, QVBoxLayout, QWidget

    w = QWidget()
    w.setWindowTitle("Video Story elements test")
    w.setWindowFlags(w.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)
    layout = QVBoxLayout(w)
    w.field = QLineEdit(FIELD)
    w.field.setAccessibleName("Search")
    w.password = QLineEdit(SECRET)
    w.password.setEchoMode(QLineEdit.EchoMode.Password)
    w.password.setAccessibleName("Password")
    w.notes = QPlainTextEdit(PARAGRAPHS)
    w.notes.setAccessibleName("Notes")
    w.save = QPushButton("Save")
    for widget in (w.field, w.password, w.notes, w.save):
        layout.addWidget(widget)
    w.resize(420, 320)
    qtbot.addWidget(w)
    w.show()
    qtbot.waitExposed(w)
    qtbot.wait(300)
    return w


def _center(widget) -> tuple[int, int]:
    import ctypes
    from ctypes import wintypes

    from PySide6.QtCore import QPoint

    local = QPoint(widget.width() // 2, widget.height() // 2)
    top = widget.window()
    rect = wintypes.RECT()
    ctypes.windll.user32.GetClientRect(int(top.winId()), ctypes.byref(rect))
    pt = wintypes.POINT(0, 0)
    ctypes.windll.user32.ClientToScreen(int(top.winId()), ctypes.byref(pt))
    ratio = top.devicePixelRatioF()
    pos = widget.mapTo(top, local)
    return pt.x + round(pos.x() * ratio), pt.y + round(pos.y() * ratio)


def _ask(qtbot, inspector, point, want):
    """Query on a thread while this (GUI) thread keeps answering UI Automation."""
    result: dict = {}

    def run():
        try:
            result["info"] = inspector.element_at(*point, want, 3000)
        except Exception as exc:  # noqa: BLE001
            result["error"] = exc

    thread = threading.Thread(target=run)
    thread.start()
    while thread.is_alive():
        qtbot.wait(20)
    if "error" in result:
        raise result["error"]
    return result["info"]


def test_inspector_reads_role_name_value_text_and_never_a_password(qtbot, window):
    from video_story.core.input_events import ContentRequest
    from video_story.platform.windows_uia import WindowsElementInspector

    inspector = WindowsElementInspector()
    started = time.monotonic()
    inspector.prepare()
    prepare_s = time.monotonic() - started
    try:
        field = _ask(qtbot, inspector, _center(window.field), ContentRequest.VALUE_AND_TEXT)
        assert field is not None and field.role == "edit" and field.name == "Search"
        assert field.content is not None and field.content.text == FIELD
        assert field.content.kind == "value" and field.pid == os.getpid()

        password = _ask(qtbot, inspector, _center(window.password), ContentRequest.VALUE_AND_TEXT)
        assert password is not None and password.is_password and password.content is None

        notes = _ask(qtbot, inspector, _center(window.notes), ContentRequest.VALUE_AND_TEXT)
        assert notes is not None and notes.content is not None
        assert "paragraph" in notes.content.text

        save = _ask(qtbot, inspector, _center(window.save), ContentRequest.NONE)
        assert save is not None and save.role == "button" and save.name == "Save"
        assert save.content is None and save.bounds is not None
        assert any(a.name == "Video Story elements test" for a in save.ancestors)

        window.notes.selectAll()
        qtbot.wait(100)
        selection = _ask(qtbot, inspector, _center(window.notes), ContentRequest.SELECTION)
        assert selection is not None
        if selection.content is not None:  # Qt exposes the selection through TextPattern
            assert selection.content.kind == "selection"
            assert "Second paragraph" in selection.content.text

        title = inspector.window_info(int(window.winId()))
        assert title is not None and title.title == "Video Story elements test"
        assert title.process.lower().startswith("python")
    finally:
        inspector.close()
    assert prepare_s < 30


def test_clicks_are_stored_with_their_elements(qtbot, window, tmp_path: Path):
    import ctypes
    from ctypes import wintypes

    from test_mouse_hook import LDOWN, LUP, MOVE, _send

    from video_story.capture.input_recorder import InputRecorder
    from video_story.core.input_events import read_mouse_log
    from video_story.core.screens import assign_screen_ids
    from video_story.core.timeline import RecordingClock
    from video_story.platform.windows import WindowsPlatform

    platform = WindowsPlatform()
    platform.prepare_process()
    clock = RecordingClock()
    clock.start()
    rec = InputRecorder(
        tmp_path / "input" / "mouse.jsonl",
        assign_screen_ids(platform.list_screens()),
        platform.mouse_source(),
        clock,
        own_pid=0,  # treat the test window like another application's
        inspector=platform.element_inspector(),
    )
    old = wintypes.POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(old))
    rec.start()
    try:
        deadline = time.monotonic() + 30
        while not rec._elements_ok:
            assert time.monotonic() < deadline
            qtbot.wait(50)
        field, password, save = (
            _center(window.field),
            _center(window.password),
            _center(window.save),
        )
        _send([(MOVE, 0, *field), (LDOWN, 0, *field), (LUP, 0, *field)])
        qtbot.wait(600)
        _send([(MOVE, 0, *password), (LDOWN, 0, *password), (LUP, 0, *password)])
        qtbot.wait(600)
        _send([(MOVE, 0, *save), (LDOWN, 0, *save), (LUP, 0, *save)])
        qtbot.wait(1500)  # also a rest point over the button
    finally:
        ctypes.windll.user32.SetCursorPos(old.x, old.y)
        info = rec.stop()

    log = read_mouse_log(rec.path)
    downs = [b for b in log.buttons if b.kind == "down"]
    assert len(downs) == 3
    names = [getattr(log.element_for(d.seq), "name", None) for d in downs]
    assert names == ["Search", "(password field)", "Save"]
    assert log.content_of(log.element_for(downs[0].seq)) == FIELD
    assert SECRET not in rec.path.read_text(encoding="utf-8")
    assert log.windows[downs[0].win].title == "Video Story elements test"
    assert info.capabilities.elements and info.elements >= 3 and info.element_timeouts == 0
