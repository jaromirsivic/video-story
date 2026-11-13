"""Folders of recordings: folders.json (order, main, pinned, collapsed), names, moving a
recording and where a session's final directory lies."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from video_story.core import folders as fo
from video_story.core.config import Config
from video_story.core.session import (
    SessionPaths,
    SessionState,
    Status,
    list_sessions,
    new_session_id,
    read_state,
    write_state,
)
from video_story.host.runner import resolve_session


def _folders(root: Path, *names: str) -> fo.FolderState:
    state = fo.load_folders(root)
    for name in names:
        state = fo.create_folder(root, name)
    return state


def _saved(root: Path, sid: str, folder: str | None = None) -> SessionPaths:
    paths = SessionPaths(root, sid)
    final = root / folder / sid if folder else paths.final_dir
    paths.frames_dir(final).mkdir(parents=True)
    paths.manifest_path(final).write_text("{}", encoding="utf-8")
    return paths


# folders.json -------------------------------------------------------------------------


def test_defaults_create_the_default_folder(tmp_path: Path):
    state = fo.load_folders(tmp_path)
    assert (tmp_path / "default").is_dir()
    assert state.order == ["default"] and state.main == "default"
    assert state.pinned == "default" and state.collapsed == []
    assert fo.main_folder(tmp_path) == "default"


def test_new_folders_go_to_the_end(tmp_path: Path):
    state = _folders(tmp_path, "Clients", "Archive")
    assert state.order == ["default", "Clients", "Archive"]
    stored = json.loads((tmp_path / "folders.json").read_text(encoding="utf-8"))
    assert stored["order"] == ["default", "Clients", "Archive"]
    assert (tmp_path / "Clients").is_dir()


def test_pin_moves_to_the_top_and_the_previous_pinned_to_second(tmp_path: Path):
    _folders(tmp_path, "A", "B", "C")
    state = fo.pin(tmp_path, "C")
    assert state.order == ["C", "default", "A", "B"] and state.pinned == "C"
    state = fo.pin(tmp_path, "A")
    assert state.order == ["A", "C", "default", "B"] and state.pinned == "A"
    assert fo.load_folders(tmp_path).order == ["A", "C", "default", "B"]


def test_unpin_keeps_the_order_and_frees_the_first_folder(tmp_path: Path):
    _folders(tmp_path, "A", "B")
    state = fo.load_folders(tmp_path)
    assert not state.can_move_down("default")  # pinned: it does not move
    assert not state.can_move_up("A")  # nothing passes the pinned folder
    state = fo.unpin(tmp_path)
    assert state.pinned is None and state.order == ["default", "A", "B"]
    assert state.can_move_up("A") and state.can_move_down("default")
    assert fo.move_folder(tmp_path, "A", -1).order == ["A", "default", "B"]
    assert fo.load_folders(tmp_path).pinned is None  # stays unpinned


def test_move_up_and_down_below_the_pinned_folder(tmp_path: Path):
    _folders(tmp_path, "A", "B", "C")
    assert fo.move_folder(tmp_path, "C", -1).order == ["default", "A", "C", "B"]
    assert fo.move_folder(tmp_path, "A", 1).order == ["default", "C", "A", "B"]
    assert fo.move_folder(tmp_path, "C", -1).order == ["default", "C", "A", "B"]  # refused
    assert fo.move_folder(tmp_path, "B", 1).order == ["default", "C", "A", "B"]  # last


def test_main_folder_anywhere_and_back_to_default_when_deleted(tmp_path: Path):
    _folders(tmp_path, "A", "B")
    state = fo.set_main(tmp_path, "B")
    assert state.main == "B" and state.order == ["default", "A", "B"]
    assert fo.main_folder(tmp_path) == "B"
    state = fo.remove_folder(tmp_path, "B")
    assert state.main == "default" and fo.main_folder(tmp_path) == "default"


def test_removing_the_pinned_folder_leaves_none_pinned(tmp_path: Path):
    _folders(tmp_path, "A")
    fo.pin(tmp_path, "A")
    fo.set_collapsed(tmp_path, "A", True)
    state = fo.remove_folder(tmp_path, "A")
    assert state.pinned is None and state.order == ["default"] and state.collapsed == []
    assert not (tmp_path / "A").exists()


def test_default_cannot_be_removed_and_a_full_folder_is_kept(tmp_path: Path):
    _folders(tmp_path, "A")
    with pytest.raises(ValueError):
        fo.remove_folder(tmp_path, "default")
    (tmp_path / "A" / "notes.txt").write_text("mine", encoding="utf-8")
    with pytest.raises(OSError):
        fo.remove_folder(tmp_path, "A")
    assert fo.load_folders(tmp_path).order == ["default", "A"]


def test_collapsed_folders_are_remembered(tmp_path: Path):
    _folders(tmp_path, "A")
    fo.set_collapsed(tmp_path, "A", True)
    assert fo.load_folders(tmp_path).is_collapsed("A")
    fo.set_collapsed(tmp_path, "A", False)
    assert not fo.load_folders(tmp_path).is_collapsed("A")


def test_collapse_all_and_expand_all(tmp_path: Path):
    _folders(tmp_path, "A", "B")
    state = fo.set_all_collapsed(tmp_path, True)
    assert state.collapsed == ["default", "A", "B"]
    assert fo.load_folders(tmp_path).collapsed == ["default", "A", "B"]
    assert fo.set_all_collapsed(tmp_path, False).collapsed == []
    assert fo.load_folders(tmp_path).collapsed == []


def test_the_file_is_checked_against_the_disk(tmp_path: Path):
    for name in ("zeta", "Alpha", ".hidden"):
        (tmp_path / name).mkdir()
    (tmp_path / "20260101-120000").mkdir()  # a recording of the old flat layout
    (tmp_path / "folders.json").write_text(
        json.dumps(
            {
                "order": ["gone", "zeta", "default"],
                "main": "gone",
                "pinned": "zeta",
                "collapsed": ["gone", "zeta"],
            }
        ),
        encoding="utf-8",
    )
    state = fo.load_folders(tmp_path)
    assert state.order == ["zeta", "default", "Alpha"]  # unknown ones appended by name
    assert state.main == "default" and state.pinned == "zeta" and state.collapsed == ["zeta"]


def test_a_broken_file_means_the_defaults(tmp_path: Path):
    (tmp_path / "B").mkdir()
    (tmp_path / "folders.json").write_text("{not json", encoding="utf-8")
    state = fo.load_folders(tmp_path)
    assert state.order == ["default", "B"] and state.main == "default"


# Names --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "",
        "   ",
        " lead",
        "trail ",
        "dot.",
        ".hidden",
        "a/b",
        "a:b",
        'q"',
        "CON",
        "nul.txt",
        "lpt1",
        "20261112-151530",
        ".work",
        "x" * 101,
        "Default",
        "clients",
    ],
)
def test_invalid_names(name: str):
    assert fo.validate_name(name, ["default", "Clients"]) is not None


def test_valid_names_and_the_default_new_name():
    assert fo.validate_name("Projekt Žluťoučký kůň", ["default"]) is None
    now = datetime(2026, 11, 12, 15, 15, 30)
    assert fo.new_folder_name(["default"], now) == "New folder 20261112-151530"
    taken = ["New folder 20261112-151530", "new folder 20261112-151530 (2)"]
    assert fo.new_folder_name(taken, now) == "New folder 20261112-151530 (3)"


# Sessions in folders ----------------------------------------------------------------------


def test_finalize_saves_into_the_main_folder(tmp_path: Path):
    _folders(tmp_path, "Clients")
    fo.set_main(tmp_path, "Clients")
    paths = SessionPaths(tmp_path, "20261112-100000")
    paths.create_work_dirs()
    (paths.work_dir / "manifest.json").write_text("{}", encoding="utf-8")
    assert paths.finalize() == tmp_path / "Clients" / paths.session_id
    assert paths.final_dir == tmp_path / "Clients" / paths.session_id  # found there now
    fo.set_main(tmp_path, "default")
    assert paths.final_dir == tmp_path / "Clients" / paths.session_id  # it stays


def test_a_vanished_main_folder_saves_into_default(tmp_path: Path):
    _folders(tmp_path, "Gone")
    fo.set_main(tmp_path, "Gone")
    (tmp_path / "Gone").rmdir()
    paths = SessionPaths(tmp_path, "20261112-100000")
    assert paths.final_dir == tmp_path / "default" / paths.session_id


def test_list_sessions_covers_every_folder_and_skips_the_flat_layout(tmp_path: Path):
    _folders(tmp_path, "A")
    _saved(tmp_path, "20261101-100000")
    _saved(tmp_path, "20261102-100000", "A")
    (tmp_path / "20261103-100000").mkdir()  # flat layout: not listed
    found = {p.session_id: p.folder for p in list_sessions(tmp_path)}
    assert found == {"20261101-100000": "default", "20261102-100000": "A"}


def test_new_session_ids_are_unique_across_folders(tmp_path: Path):
    _folders(tmp_path, "A")
    _saved(tmp_path, "20261112-100000", "A")
    when = datetime(2026, 11, 12, 10, 0, 0)
    assert new_session_id(tmp_path, when=when) == "20261112-100000-2"


def test_move_recording_renames_and_updates_state_json(tmp_path: Path):
    _folders(tmp_path, "A")
    paths = _saved(tmp_path, "20261112-100000")
    old = paths.final_dir
    write_state(
        paths.state_path,
        SessionState(
            session_id=paths.session_id,
            status=Status.EDITING,
            recording_folder=str(tmp_path),
            recording_dir=str(old),
            manifest=str(old / "manifest.json"),
            frames_dir=str(old / "frames"),
        ),
    )
    moved = fo.move_recording(paths, "A")
    assert moved.folder == "A" and moved.final_dir == tmp_path / "A" / paths.session_id
    assert moved.final_dir.is_dir() and not old.exists()
    assert SessionPaths(tmp_path, paths.session_id).final_dir == moved.final_dir
    state = read_state(paths.state_path)
    assert state.recording_dir == str(moved.final_dir)
    assert state.manifest == str(moved.final_dir / "manifest.json")
    assert state.frames_dir == str(moved.final_dir / "frames")
    _saved(tmp_path, "20261112-100000", "default")  # a clash in the target folder
    with pytest.raises(FileExistsError):
        fo.move_recording(SessionPaths(tmp_path, paths.session_id, "default"), "A")


def test_a_recording_directory_resolves_to_its_folder(tmp_path: Path):
    _folders(tmp_path, "A")
    paths = _saved(tmp_path, "20261112-100000", "A")
    resolved = resolve_session(Config(recording_folder=tmp_path / "x"), str(paths.final_dir))
    assert resolved.recording_folder == tmp_path and resolved.folder == "A"
    assert resolved.final_dir == tmp_path / "A" / paths.session_id


def test_a_work_directory_resolves_to_the_recording_folder(tmp_path: Path):
    paths = SessionPaths(tmp_path, "20261112-100000")
    paths.create_work_dirs()
    resolved = resolve_session(Config(recording_folder=tmp_path / "x"), str(paths.work_dir))
    assert resolved.recording_folder == tmp_path and resolved.work_dir == paths.work_dir
