"""Folders in the editor's recordings list: headers (accordions), Create new folder, the
pin, the folder menu, Move Video(s) to Folder and one-folder selection (offscreen Qt)."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import json  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402
from PySide6.QtCore import QItemSelectionModel, Qt  # noqa: E402
from PySide6.QtGui import QFont  # noqa: E402
from test_editor import NEWEST, OLDER, _wait_loaded, make_recording  # noqa: E402

from video_story.core import folders as fo  # noqa: E402
from video_story.core.config import Config  # noqa: E402
from video_story.core.session import SessionPaths, SessionState, Status, read_state, write_state  # noqa: E402
from video_story.editor.folder_dialogs import CreateFolderDialog, MoveToFolderDialog  # noqa: E402
from video_story.editor.recordings_model import FolderHeader  # noqa: E402
from video_story.editor.recordings_view import (  # noqa: E402
    PIN_TOOLTIP,
    UNPIN_TOOLTIP,
    RecordingDelegate,
)
from video_story.editor.window import (  # noqa: E402
    DELETE_FOLDER_LABEL,
    MOVE_DOWN_LABEL,
    MOVE_UP_LABEL,
    SET_MAIN_LABEL,
    EditorWindow,
)

THIRD = "20260103-100000"


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    """default: THIRD, NEWEST; Clients: OLDER."""
    make_recording(tmp_path, OLDER, first_text="older one")
    make_recording(tmp_path, NEWEST)
    make_recording(tmp_path, THIRD, first_text="third one")
    fo.create_folder(tmp_path, "Clients")
    fo.move_recording(SessionPaths(tmp_path, OLDER), "Clients")
    return tmp_path


@pytest.fixture
def editor(qtbot, folder: Path):
    window = EditorWindow(Config(recording_folder=folder))
    qtbot.addWidget(window)
    window.resize(1400, 900)
    window.show()
    _wait_loaded(qtbot, window, THIRD)
    return window


def _rows(editor: EditorWindow) -> list[str]:
    model = editor.recordings
    out = []
    for row in range(model.rowCount()):
        header = model.header(row)
        out.append(header.label if header else model.item(row).session_id)
    return out


def _actions(menu) -> dict[str, object]:
    return {a.text(): a for a in menu.actions() if a.text()}


def _confirm_all(editor: EditorWindow, answer: bool = True) -> list[str]:
    asked: list[str] = []
    editor._confirm = lambda title, text: asked.append(text) or answer
    return asked


# Headers ------------------------------------------------------------------------------


def test_headers_count_and_order(editor):
    assert _rows(editor) == ["default (2)", THIRD, NEWEST, "Clients (1)", OLDER]
    default = editor.recordings.header(0)
    assert default.main and default.pinned and not default.collapsed
    assert not editor.recordings.header(3).main
    assert not editor.recordings.flags(editor.recordings.index(0)) & Qt.ItemFlag.ItemIsSelectable
    editor.list_view.viewport().grab()  # paints the headers (bold main, pins, ⋯)


def test_header_name_is_elided_but_the_count_stays():
    header = FolderHeader("A very long folder name " * 5, 12)
    font = QFont()
    name, count = RecordingDelegate.header_texts(header, font, 160)
    assert count == " (12)" and name.endswith("…")


def test_clicking_a_header_collapses_and_it_is_remembered(editor, qtbot, folder):
    view = editor.list_view
    rect = view.visualRect(editor.recordings.index(3))
    qtbot.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=rect.center())
    assert editor.recordings.is_collapsed("Clients") and view.isRowHidden(4)
    assert json.loads((folder / "folders.json").read_text(encoding="utf-8"))["collapsed"] == [
        "Clients"
    ]
    assert editor.recording.session_id == THIRD  # a header does not open anything
    second = EditorWindow(Config(recording_folder=folder))
    qtbot.addWidget(second)
    assert second.list_view.isRowHidden(second.recordings.row_of(OLDER))
    # Showing a recording of a closed folder opens the folder.
    second.list_view.setCurrentIndex(second.recordings.index(second.recordings.row_of(OLDER)))
    assert not second.recordings.is_collapsed("Clients")
    assert not second.list_view.isRowHidden(second.recordings.row_of(OLDER))


def test_pin_tooltips_and_a_click_on_the_pin(editor, qtbot):
    view = editor.list_view
    rect = view.visualRect(editor.recordings.index(3))
    pin_box = view.delegate.pin_rect(rect)
    assert view.header_tooltip(pin_box.center()) == PIN_TOOLTIP
    top = view.delegate.pin_rect(view.visualRect(editor.recordings.index(0)))
    assert view.header_tooltip(top.center()) == UNPIN_TOOLTIP
    asked = _confirm_all(editor)
    qtbot.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=pin_box.center())
    assert asked and "above all other folders" in asked[0]
    assert _rows(editor)[0] == "Clients (1)" and editor.recordings.header(0).pinned
    assert editor.recordings.folder_state.order == ["Clients", "default"]


# Pin, main, order -----------------------------------------------------------------------


def test_pin_and_unpin_ask_first(editor):
    asked = _confirm_all(editor, answer=False)
    assert not editor.toggle_pin("Clients")
    assert editor.recordings.folder_state.pinned == "default"
    asked = _confirm_all(editor)
    assert editor.toggle_pin("Clients")
    assert editor.recordings.folder_state.order == ["Clients", "default"]
    assert editor.toggle_pin("Clients")  # unpin
    assert "No folder will stay pinned" in asked[-1]
    state = editor.recordings.folder_state
    assert state.pinned is None and state.order == ["Clients", "default"]
    assert editor.recording.session_id == THIRD  # still shown and selected
    assert editor.list_view.currentIndex().row() == editor.recordings.row_of(THIRD)


def test_folder_menu_items(editor):
    menu = editor.folder_menu("default")
    actions = _actions(menu)
    assert list(actions) == [
        SET_MAIN_LABEL,
        MOVE_UP_LABEL,
        MOVE_DOWN_LABEL,
        "Archive All",
        "Delete All Videos (2)",
        DELETE_FOLDER_LABEL,
    ]
    assert not actions[SET_MAIN_LABEL].isEnabled()  # already the main folder
    assert not actions[MOVE_UP_LABEL].isEnabled() and not actions[MOVE_DOWN_LABEL].isEnabled()
    assert not actions[DELETE_FOLDER_LABEL].isEnabled()  # default cannot go
    clients = _actions(editor.folder_menu("Clients"))
    assert clients[SET_MAIN_LABEL].isEnabled() and clients[DELETE_FOLDER_LABEL].isEnabled()
    assert not clients[MOVE_UP_LABEL].isEnabled()  # nothing passes the pinned folder
    assert "Delete All Videos (1)" in clients


def test_set_as_main_folder_and_move_folders(editor, folder):
    assert editor.set_main_folder("Clients")
    assert editor.recordings.header(3).main and not editor.recordings.header(0).main
    assert fo.main_folder(folder) == "Clients"
    fo.create_folder(folder, "Zeta")
    editor.refresh_recordings()
    assert editor.move_folder_up("Zeta")
    assert editor.recordings.folders == ["default", "Zeta", "Clients"]
    assert editor.move_folder_down("Zeta")
    assert editor.recordings.folders == ["default", "Clients", "Zeta"]


# Create, move, delete -----------------------------------------------------------------------


def test_create_folder_dialog_defaults(qtbot):
    dialog = CreateFolderDialog(["default"], movable=2)
    qtbot.addWidget(dialog)
    assert dialog.name.startswith("New folder ") and dialog.name_edit.selectedText() == dialog.name
    assert dialog.move_checkbox.isChecked() and "(2)" in dialog.move_checkbox.text()
    dialog.name_edit.setText("Default")
    assert dialog.problem() is not None and not dialog.error_label.isHidden()
    none = CreateFolderDialog(["default"], movable=0)
    qtbot.addWidget(none)
    assert not none.move_selected and not none.move_checkbox.isEnabled()
    move = MoveToFolderDialog(["Clients", "Zeta"], 3)
    qtbot.addWidget(move)
    assert move.folder == "Clients" and move.windowTitle() == "Move Videos to Folder"


def test_create_folder_moves_the_shown_video_into_it(editor, qtbot, folder):
    editor._ask_new_folder = lambda existing, movable: ("Project X", True)
    assert editor.create_folder() == "Project X"
    assert editor.recordings.folders == ["default", "Clients", "Project X"]  # at the end
    _wait_loaded(qtbot, editor, THIRD)  # opened again from its new place
    assert editor.recording.paths.final_dir == folder / "Project X" / THIRD
    assert (folder / "Project X" / THIRD / "manifest.json").exists()
    assert not (folder / "default" / THIRD).exists()
    assert _rows(editor)[-2:] == ["Project X (1)", THIRD]


def test_create_folder_without_moving(editor, folder):
    editor._ask_new_folder = lambda existing, movable: ("Empty", False)
    assert editor.create_folder() == "Empty"
    assert _rows(editor)[-1] == "Empty (0)" and (folder / "Empty").is_dir()
    assert editor.recording.session_id == THIRD
    editor._ask_new_folder = lambda existing, movable: None
    assert editor.create_folder() is None


def test_move_videos_to_folder(editor, qtbot, folder):
    selection = editor.list_view.selectionModel()
    for sid in (NEWEST, THIRD):
        index = editor.recordings.index(editor.recordings.row_of(sid))
        selection.select(index, QItemSelectionModel.SelectionFlag.Select)
    items = editor.selected_items()
    assert [i.session_id for i in items] == [THIRD, NEWEST]
    offered: list[list[str]] = []
    editor._ask_target_folder = lambda folders, count: offered.append(folders) or "Clients"
    assert sorted(editor.move_recordings_dialog(items)) == sorted([THIRD, NEWEST])
    assert offered == [["Clients"]]
    assert _rows(editor) == ["default (0)", "Clients (3)", THIRD, NEWEST, OLDER]
    _wait_loaded(qtbot, editor, THIRD)
    assert editor.recording.paths.final_dir == folder / "Clients" / THIRD


def test_moving_the_recording_an_agent_waits_on_points_process_there(editor, qtbot, folder):
    paths = SessionPaths(folder, NEWEST)
    paths.work_dir.mkdir(parents=True)
    write_state(
        paths.state_path,
        SessionState(
            session_id=NEWEST,
            status=Status.EDITING,
            recording_folder=str(folder),
            pid=os.getpid(),
            recording_dir=str(paths.final_dir),
        ),
    )
    editor.recording_session = NEWEST
    assert editor.move_recordings(
        [editor.recordings.item(editor.recordings.row_of(NEWEST))], "Clients"
    )
    assert read_state(paths.state_path).recording_dir == str(folder / "Clients" / NEWEST)
    editor.list_view.setCurrentIndex(editor.recordings.index(editor.recordings.row_of(NEWEST)))
    _wait_loaded(qtbot, editor, NEWEST)
    # Not the green recording any more (another folder comes first): Process asks.
    editor._confirm_process_not_latest = lambda: True
    editor.process()
    state = read_state(paths.state_path)
    assert state.status is Status.PROCESSED
    assert state.recording_dir == str(folder / "Clients" / NEWEST)


def test_move_menu_entry(editor, folder):
    from video_story.editor.window import move_label

    assert move_label(1) == "Move Video to Folder..."
    assert move_label(5) == "Move Videos to Folder... (5)"
    item = editor.recordings.item(editor.recordings.row_of(THIRD))
    assert editor._move_targets([item]) == ["Clients"]
    only_default = editor.recordings.item(editor.recordings.row_of(OLDER))
    assert editor._move_targets([only_default]) == ["default"]


def test_delete_all_videos_keeps_the_folder(editor, folder):
    editor._confirm_delete = lambda items: True
    assert editor.delete_recordings(editor.recordings.items_of("Clients")) == [OLDER]
    assert _rows(editor)[-1] == "Clients (0)" and (folder / "Clients").is_dir()


def test_delete_folder_deletes_its_videos_and_resets_main_and_pin(editor, folder):
    editor.set_main_folder("Clients")
    asked = _confirm_all(editor)
    editor.toggle_pin("Clients")
    assert editor.delete_folder("Clients")
    assert "all 1 video in it" in asked[-1]
    assert not (folder / "Clients").exists()
    state = editor.recordings.folder_state
    assert state.order == ["default"] and state.main == "default" and state.pinned is None
    assert OLDER not in [i.session_id for i in editor.recordings.items]
    assert not editor.delete_folder("default")


def test_delete_folder_declined_or_blocked_keeps_it(editor, folder):
    _confirm_all(editor, answer=False)
    assert not editor.delete_folder("Clients") and (folder / "Clients" / OLDER).exists()
    _confirm_all(editor)
    reported: list[str] = []
    editor._report_failure = lambda title, text: reported.append(text)
    (folder / "Clients" / "notes.txt").write_text("mine", encoding="utf-8")
    assert not editor.delete_folder("Clients")
    assert (folder / "Clients" / "notes.txt").exists() and reported


# Selection ----------------------------------------------------------------------------------


def test_selection_stays_in_one_folder(editor, qtbot):
    view = editor.list_view
    selection = view.selectionModel()
    for sid in (THIRD, NEWEST, OLDER):
        index = editor.recordings.index(editor.recordings.row_of(sid))
        selection.select(index, QItemSelectionModel.SelectionFlag.Select)
    assert [i.session_id for i in editor.selected_items()] == [THIRD, NEWEST]  # OLDER dropped
    older = view.visualRect(editor.recordings.index(editor.recordings.row_of(OLDER))).center()
    qtbot.mouseClick(
        view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier, older
    )
    assert [i.session_id for i in editor.selected_items()] == [OLDER]
    _wait_loaded(qtbot, editor, OLDER)


def test_keys_skip_the_headers(editor, qtbot):
    view = editor.list_view
    view.setCurrentIndex(editor.recordings.index(editor.recordings.row_of(NEWEST)))
    view.setFocus()
    qtbot.keyClick(view, Qt.Key.Key_Down)
    assert view.currentIndex().row() == editor.recordings.row_of(OLDER)
    qtbot.keyClick(view, Qt.Key.Key_Up, Qt.KeyboardModifier.ShiftModifier)
    assert view.currentIndex().row() == editor.recordings.row_of(OLDER)  # Shift stays
    qtbot.keyClick(view, Qt.Key.Key_Up)
    assert view.currentIndex().row() == editor.recordings.row_of(NEWEST)
