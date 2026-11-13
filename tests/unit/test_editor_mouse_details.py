"""The Mouse window: everything recorded about the mouse at the playhead."""

from __future__ import annotations

import json
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, Qt  # noqa: E402
from test_editor import NEWEST, make_recording  # noqa: E402

from video_story.core.config import Config  # noqa: E402
from video_story.core.input_events import (  # noqa: E402
    ButtonRecord,
    ContentRecord,
    ElementRecord,
    HoverRecord,
    MouseLog,
    MoveRecord,
    WheelRecord,
    WindowRecord,
)
from video_story.core.naming import sidecar_filename  # noqa: E402
from video_story.core.screens import RawScreen, assign_screen_ids  # noqa: E402
from video_story.editor.mouse_activity import PRESSED_FILL  # noqa: E402
from video_story.editor.mouse_details import (  # noqa: E402
    NOW_MARK,
    ClipboardChange,
    MouseDetails,
    read_clipboard_changes,
)
from video_story.editor.mouse_details_dialog import LOADING, MouseDetailsDialog  # noqa: E402
from video_story.editor.window import EditorWindow  # noqa: E402
from video_story.host.runner import FrameIndex  # noqa: E402

SCREENS = assign_screen_ids([RawScreen(0, 0, 320, 200, primary=True), RawScreen(320, 0, 160, 100)])


def button(seq: int, t: int, kind: str, x: int, y: int, name: str = "left", **kw) -> ButtonRecord:
    return ButtonRecord(
        seq=seq, t=t, kind=kind, button=name, screen="screen1", x=x, y=y, gx=x, gy=y, **kw
    )


def sample_log() -> MouseLog:
    """A drag of report.pdf onto Archive (1000-2000) with Ctrl, a scroll down at 3000,
    a double click at 4000 and a rest over a password field at 5000."""
    log = MouseLog()
    records = [
        WindowRecord(seq=1, t=900, win=1, title="Downloads", process="explorer.exe"),
        button(2, 1000, "down", 100, 50, mods=("ctrl",), win=1),
        ContentRecord(seq=3, t=1000, hash="h1", text="report.pdf"),
        ElementRecord(
            seq=4,
            t=1010,
            ref=2,
            role="list_item",
            name="report.pdf",
            bounds=(330, 10, 120, 20),
            ancestors=({"role": "list", "name": "Items"}, {"role": "pane", "name": "Downloads"}),
            win=1,
            content={"kind": "value", "hash": "h1", "chars": 10},
        ),
        MoveRecord(seq=5, t=1200, screen="screen1", x=130, y=50, gx=130, gy=50),
        MoveRecord(seq=6, t=1500, screen="screen1", x=160, y=50, gx=160, gy=50),
        MoveRecord(seq=7, t=1900, screen="screen1", x=200, y=50, gx=200, gy=50),
        button(8, 2000, "up", 200, 50, mods=("ctrl",), win=1),
        ElementRecord(seq=9, t=2010, ref=8, role="tree_item", name="Archive", win=1),
        WheelRecord(
            seq=10,
            t=3000,
            screen="screen1",
            x=50,
            y=60,
            gx=50,
            gy=60,
            axis="v",
            delta=-240,
            count=2,
            until=3100,
            win=1,
        ),
        button(11, 4000, "down", 20, 20),
        button(12, 4050, "up", 20, 20),
        button(13, 4200, "down", 20, 20),
        button(14, 4250, "up", 20, 20),
        HoverRecord(seq=15, t=5000, screen="screen1", x=20, y=20, gx=20, gy=20),
        ElementRecord(seq=16, t=5000, ref=15, role="edit", name="Password", password=True),
    ]
    for record in records:
        log.add(record)
    return log


def details(log: MouseLog | None = None) -> MouseDetails:
    return MouseDetails(
        log if log is not None else sample_log(),
        duration_ms=6000,
        screens=SCREENS,
        clipboard=[ClipboardChange(2500, "first copy"), ClipboardChange(4500, "second copy")],
    )


def test_a_drag_in_progress_shows_where_it_started_is_and_ends():
    m = details().at(1500)
    assert m.recorded
    assert m.held == {"left": 500}
    assert m.wheel == frozenset()
    assert m.action == "DRAG left"
    assert m.action_time == "00:00:01.000 - 00:00:02.000 (1000 ms)"
    assert m.modifiers == "Ctrl"
    assert m.injected == "no"
    assert m.start == "screen1 100,50"
    assert m.now == "screen1 160,50 (60 px so far)"
    assert m.end == "screen1 200,50 (100 px)"
    assert m.scroll == "-"
    assert m.element_captured == "00:00:01.000 at press (0.5 s ago)"
    assert m.element_role == "list_item"
    assert m.element_name == "report.pdf"
    assert m.element_parents == 'list "Items" > pane "Downloads"'
    assert m.element_bounds == "screen2 10,10 120x20"  # bounds in that screen's image
    assert m.drop_target == 'tree_item "Archive"'
    assert m.content_label == "Content (value, 10 chars)"
    assert m.content == "report.pdf"
    assert m.window_title == "Downloads" and m.window_process == "explorer.exe"
    assert m.window_since == "00:00:01.000 (0.5 s)"
    assert m.recent.splitlines()[0].endswith(NOW_MARK)
    assert "DRAG left" in m.recent.splitlines()[0]
    assert "SCROLL down" in m.recent


def test_a_scroll_lights_its_direction_and_a_double_click_is_one_action():
    d = details()
    m = d.at(3050)
    assert m.wheel == {"down"}
    assert m.held == {}
    assert m.action.startswith("SCROLL")
    assert m.scroll == "down 2 notches"
    assert m.element_captured.startswith("00:00:02.000 at release")  # the latest element
    m = d.at(4210)
    assert m.action == "DOUBLE-CLICK left"
    assert m.held == {"left": 10}
    assert m.action_time.startswith("00:00:04.000 - 00:00:04.250")


def test_idle_moments_keep_the_latest_element_and_mark_now_in_the_events():
    d = details()
    m = d.at(2600)
    assert m.action == "Idle" and m.held == {} and m.start == "-"
    assert m.drop_target == "-"
    assert "--- now 2600 ---" in m.recent
    before = d.at(500)
    assert before.element_captured == "-" and before.window_title == "-"
    password = d.at(5100)
    assert password.element_role == "edit"
    assert password.content_label == "Content (password field, never read)"
    assert password.content == ""


def test_the_clipboard_is_the_latest_copy_before_the_moment():
    d = details()
    assert d.at(1000).clipboard_changed == "Nothing copied yet"
    m = d.at(3000)
    assert m.clipboard_changed == "00:00:02.500 (0.5 s ago)"
    assert m.clipboard_length == "10 chars"
    assert m.clipboard_copy == "1 of 2"
    assert m.clipboard_text == "first copy"
    assert d.at(9000).clipboard_copy == "2 of 2"


def test_without_mouse_data_only_the_clipboard_is_shown():
    m = MouseDetails(None, clipboard=[ClipboardChange(10, "x")]).at(100)
    assert not m.recorded
    assert m.action == "-" and m.recent == ""
    assert m.clipboard_text == "x"


def test_injected_presses_do_not_light_a_button_but_are_named():
    log = MouseLog()
    log.add(button(1, 100, "down", 10, 10, injected=True))
    log.add(button(2, 150, "up", 10, 10, injected=True))
    m = MouseDetails(log).at(120)
    assert m.held == {}
    assert m.action == "CLICK left" and m.injected == "yes"


def test_clipboard_changes_are_read_from_one_sidecar_per_tick(tmp_path: Path):
    info = tmp_path / "frames_info"
    info.mkdir()
    for t, clip in ((0, None), (500, "copied"), (1000, None)):
        for sid in ("screen1", "screen2"):
            data = {"time_ms": t, "screen": sid}
            if clip is not None:
                data["clipboard"] = clip
            (info / sidecar_filename(t, sid)).write_text(json.dumps(data), encoding="utf-8")
    (info / sidecar_filename(1500, "screen1")).write_text('{"clipboard": ', encoding="utf-8")
    changes = read_clipboard_changes(FrameIndex(tmp_path / "frames", info))
    assert changes == [ClipboardChange(500, "copied")]


def test_dialog_fills_every_field_and_colours_held_buttons(qtbot):
    dialog = MouseDetailsDialog()
    qtbot.addWidget(dialog)
    dialog.show()
    dialog.show_moment(details().at(1500), "screen1 320x200 · cursor 160,50")
    assert dialog.title_label.text() == "Mouse at 00:00:01.500"
    assert dialog.fields["action"].text() == "DRAG left"
    assert dialog.fields["drop_target"].text() == 'tree_item "Archive"'
    assert dialog.fields["action"].isReadOnly()
    assert dialog.texts["content"].toPlainText() == "report.pdf"
    assert dialog.texts["content"].isReadOnly()
    assert dialog.labels["content"].text() == "Content (value, 10 chars)"
    assert NOW_MARK in dialog.texts["recent"].toPlainText()
    assert PRESSED_FILL in dialog.button_boxes["left"].styleSheet()
    assert dialog.button_boxes["left"].text() == "Left\n0.5 s"
    assert PRESSED_FILL not in dialog.button_boxes["right"].styleSheet()
    dialog.show_moment(details().at(3050))
    assert PRESSED_FILL in dialog.wheel_arrows["down"].styleSheet()  # red, not yellow
    assert PRESSED_FILL not in dialog.wheel_arrows["up"].styleSheet()
    assert PRESSED_FILL not in dialog.button_boxes["left"].styleSheet()
    dialog.show_message(LOADING)
    assert dialog.where_label.text() == LOADING
    assert dialog.fields["action"].text() == ""


def test_dialog_panels_are_stacked_with_short_fields_side_by_side(qtbot):
    dialog = MouseDetailsDialog()
    qtbot.addWidget(dialog)
    dialog.resize(1100, 900)
    dialog.show()
    qtbot.waitExposed(dialog)
    action, time = dialog.fields["action"], dialog.fields["action_time"]
    assert action.mapTo(dialog, QPoint(0, 0)).y() == time.mapTo(dialog, QPoint(0, 0)).y()
    assert time.mapTo(dialog, QPoint(0, 0)).x() > action.mapTo(dialog, QPoint(0, 0)).x()
    content = dialog.texts["content"]
    clipboard = dialog.texts["clipboard_text"]
    assert content.width() > 3 * action.width()  # multi-line fields span the panel
    assert clipboard.mapTo(dialog, QPoint(0, 0)).y() > content.mapTo(dialog, QPoint(0, 0)).y()


def _write_mouse_log(paths) -> None:
    lines = [
        {
            "seq": 1,
            "t": 900,
            "type": "window",
            "win": 1,
            "title": "Notes",
            "process": "notepad.exe",
        },
        {
            "seq": 2,
            "t": 1000,
            "type": "down",
            "button": "left",
            "screen": "screen1",
            "x": 10,
            "y": 10,
            "gx": 10,
            "gy": 10,
            "win": 1,
        },
        {
            "seq": 3,
            "t": 1010,
            "type": "element",
            "ref": 2,
            "role": "button",
            "name": "Save",
            "win": 1,
        },
        {
            "seq": 4,
            "t": 1300,
            "type": "up",
            "button": "left",
            "screen": "screen1",
            "x": 10,
            "y": 10,
            "gx": 10,
            "gy": 10,
            "win": 1,
        },
    ]
    path = paths.mouse_log_path(paths.final_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")
    sidecar = paths.frames_info_dir(paths.final_dir) / sidecar_filename(500, "screen1")
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    data["clipboard"] = "copied text"
    sidecar.write_text(json.dumps(data), encoding="utf-8")


def test_clicking_the_mouse_opens_the_window_that_follows_the_playhead(qtbot, tmp_path: Path):
    paths = make_recording(tmp_path, NEWEST)
    _write_mouse_log(paths)
    window = EditorWindow(Config(recording_folder=tmp_path))
    qtbot.addWidget(window)
    window.show()
    qtbot.waitUntil(lambda: window.recording is not None, timeout=10_000)
    assert window.mouse_dialog is None
    window._seek(1100)
    qtbot.mouseClick(window.preview.mouse_view, Qt.MouseButton.LeftButton)
    dialog = window.mouse_dialog
    assert dialog is not None and dialog.isVisible() and not dialog.isModal()
    qtbot.waitUntil(lambda: dialog.fields["action"].text() == "CLICK left", timeout=10_000)
    assert dialog.fields["element_name"].text() == "Save"
    assert dialog.fields["window_process"].text() == "notepad.exe"
    assert dialog.texts["clipboard_text"].toPlainText() == "copied text"
    assert dialog.where_label.text() == "screen2 160x100 · cursor 20,5"  # cursor at 1000 ms
    assert PRESSED_FILL in dialog.button_boxes["left"].styleSheet()
    window._seek(2000)
    assert dialog.fields["action"].text() == "Idle"
    assert dialog.title_label.text() == "Mouse at 00:00:02.000"
    assert PRESSED_FILL not in dialog.button_boxes["left"].styleSheet()
    assert window.isEnabled()  # the editor stays usable beside the window
    window.close()
