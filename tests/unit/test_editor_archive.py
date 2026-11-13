"""Archiving in the editor: menu labels, the queue, list labels, read-only archived view."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402

from test_editor import NEWEST, OLDER, _wait_loaded, make_recording  # noqa: E402

import video_story.editor.window as window_mod  # noqa: E402
from video_story.core.archive_state import read_archive_state, request_archive  # noqa: E402
from video_story.core.config import Config  # noqa: E402
from video_story.core.session import SessionPaths  # noqa: E402
from video_story.core.transcript import Segment, Transcript, write_transcript  # noqa: E402
from video_story.editor.archiving import ArchiveManager  # noqa: E402
from video_story.editor.recordings_view import (  # noqa: E402
    BADGE_ARCHIVED,
    BADGE_ARCHIVING,
    BADGE_TRANSCRIBING,
    format_percent,
)
from video_story.editor.window import (  # noqa: E402
    ARCHIVED_NOTE,
    ARCHIVING_MESSAGE,
    EditorWindow,
    archive_label,
    delete_label,
)

# Archives the recording with a stand-in for FFmpeg, like ``archive-stream`` does.
FAKE_ARCHIVER = r"""
import json, sys, time
from pathlib import Path
from video_story.core.config import Config
from video_story.core.session import SessionPaths
from video_story.export.archive import archive_recording

final, mode = Path(sys.argv[1]), sys.argv[2]
if mode == "hang":
    sys.stdin.read()  # until the editor closes stdin
    sys.exit(3)

def exporter(request, config, progress=None, cancel=None):
    request.output.write_bytes(b"mp4")
    request.output.with_suffix(".srt").write_text("1", encoding="utf-8")
    if progress:
        progress(1.0, "")

def audio_encoder(source, output, config, progress=None, cancel=None):
    output.write_bytes(b"aac")

def emit(event):
    sys.stdout.write(json.dumps(event) + "\n")
    sys.stdout.flush()

paths = SessionPaths(final.parent.parent, final.name, final.parent.name)
emit({"event": "phase", "phase": "archiving"})
state = archive_recording(
    paths,
    Config(recording_folder=final.parent),
    progress=lambda f, _t: emit({"event": "progress", "phase": "archiving", "fraction": f}),
    exporter=exporter,
    verifier=lambda *args: None,  # the stand-in MP4 is not a video
    audio_encoder=audio_encoder,
    audio_verifier=lambda *args: None,  # nor is the stand-in .m4a audio
)
emit({"event": "done", "status": str(state.status), "leftovers": state.leftovers, "ok": True})
"""


class ScriptedArchiver(ArchiveManager):
    def __init__(self, config: Config, mode: str = "archive") -> None:
        super().__init__(config)
        self.mode = mode
        self.started_ids: list[str] = []
        self.started.connect(self.started_ids.append)

    def command(self, paths: SessionPaths) -> list[str]:
        return [sys.executable, "-X", "utf8", "-c", FAKE_ARCHIVER, str(paths.final_dir), self.mode]


def _open(qtbot, folder: Path, mode: str = "archive", session_id: str = NEWEST) -> EditorWindow:
    cfg = Config(recording_folder=folder)
    window = EditorWindow(cfg, session_id=session_id, archiver=ScriptedArchiver(cfg, mode))
    window._confirm_archive = lambda items: True  # type: ignore[method-assign]
    qtbot.addWidget(window)
    window.show()
    return window


def _item(window: EditorWindow, session_id: str):
    return window.recordings.item(window.recordings.row_of(session_id))


def test_menu_labels_count_the_videos():
    assert delete_label(1) == "Delete Video" and delete_label(8) == "Delete Videos (8)"
    assert archive_label(1) == "Archive Video" and archive_label(4) == "Archive Videos (4)"


def test_archive_shown_video_locks_it_then_shows_the_archived_view(qtbot, tmp_path: Path):
    make_recording(tmp_path, OLDER)
    paths = make_recording(tmp_path, NEWEST)
    window = _open(qtbot, tmp_path)
    _wait_loaded(qtbot, window, NEWEST)
    assert window.recording is not None and window.recording.audio is not None

    queued = window.archive_recordings([_item(window, NEWEST)])

    assert queued == [NEWEST]
    assert read_archive_state(paths) is not None
    assert window.recording is None  # released at once: its files are about to go
    assert window.preview.view.message.startswith(ARCHIVING_MESSAGE)
    qtbot.waitUntil(lambda: _item(window, NEWEST).archived, timeout=30_000)
    qtbot.waitUntil(
        lambda: window.recording is not None and window.recording.archived, timeout=10_000
    )
    assert not paths.frames_dir(paths.final_dir).exists()
    assert not paths.audio_dir(paths.final_dir).exists()
    # The audio track kept in archive/ does not disturb the archived view.
    assert (paths.archive_dir(paths.final_dir) / "audio.m4a").is_file()
    assert window.list_view.badge_of(_item(window, NEWEST)) == (BADGE_ARCHIVED, None)
    # Read only: subtitles yes, everything in the bottom row no.
    recording = window.recording
    assert recording.audio is None and recording.frame_times("screen1") == [0]
    assert not window.process_button.isEnabled()
    assert not window.instructions_button.isEnabled()
    assert not window.auto_process_checkbox.isEnabled()
    assert not window.process_action.isEnabled()
    assert window.render_block_reason([_item(window, NEWEST)]) == ARCHIVED_NOTE
    assert window.render_video(_item(window, NEWEST)) is False
    window.model.set_playhead(1600)
    window._sync_playhead()
    assert window.preview.subtitle_label.text() == "second line"
    assert window.preview.view.shown_path is None or window.preview.view.shown_path.endswith(
        "screen1.jpg"
    )
    assert window.timeline.fit_button_rect() is None  # no waveform, no stretch toggle
    # The other recording is untouched and editable.
    window.list_view.setCurrentIndex(window.recordings.index(window.recordings.row_of(OLDER)))
    _wait_loaded(qtbot, window, OLDER)
    assert window.process_button.isEnabled() and window.auto_process_checkbox.isEnabled()


def test_archivable_skips_archived_queued_and_untranscribed(qtbot, tmp_path: Path):
    make_recording(tmp_path, OLDER)
    newest = make_recording(tmp_path, NEWEST)
    third = make_recording(tmp_path, "20260103-090000")
    write_transcript(
        third.transcript_txt_path(third.final_dir),
        third.transcript_json_path(third.final_dir),
        Transcript("en", [Segment(0, 500, "x")], complete=False, transcribed_ms=500),
    )
    request_archive(newest)  # queued by an earlier editor: resumed at start
    window = _open(qtbot, tmp_path, mode="hang", session_id=OLDER)  # not the untranscribed
    qtbot.waitUntil(lambda: window.archiver.running == NEWEST, timeout=10_000)
    items = [_item(window, sid) for sid in ("20260103-090000", NEWEST, OLDER)]
    assert [i.session_id for i in window.archivable(items)] == [OLDER]
    # Selecting the queued recording shows the notice instead of loading it.
    window.list_view.setCurrentIndex(window.recordings.index(window.recordings.row_of(NEWEST)))
    qtbot.waitUntil(lambda: (window.preview.view.message or "").startswith(ARCHIVING_MESSAGE))
    assert window.recording is None
    started = time.monotonic()
    window.close()  # closes the worker's stdin: it stops without waiting for the timeout
    assert time.monotonic() - started < 10
    assert read_archive_state(newest).status == "queued"  # resumed by the next editor


def test_old_recordings_are_queued_when_the_editor_starts(qtbot, tmp_path: Path, monkeypatch):
    old = make_recording(tmp_path, OLDER)
    make_recording(tmp_path, NEWEST)
    asked: list[int] = []

    def due(folder, days):
        asked.append(days)
        return [old]

    monkeypatch.setattr(window_mod, "recordings_due_for_archive", due)
    window = _open(qtbot, tmp_path)
    assert asked == [30]
    assert read_archive_state(old).reason == "age"
    qtbot.waitUntil(lambda: _item(window, OLDER).archived, timeout=30_000)
    assert window.archiver.started_ids == [OLDER]


def test_list_labels_show_what_runs_with_one_decimal(qtbot, tmp_path: Path):
    make_recording(tmp_path, NEWEST)
    window = _open(qtbot, tmp_path)
    _wait_loaded(qtbot, window, NEWEST)
    item = _item(window, NEWEST)
    assert window.list_view.badge_of(item) is None
    window.list_view.set_activity(NEWEST, "transcribing", 0.375)
    assert window.list_view.badge_of(item) == (BADGE_TRANSCRIBING, "37.5 %")
    window.list_view.set_activity(NEWEST, "archiving", 0.5)
    assert window.list_view.badge_of(item) == (BADGE_ARCHIVING, "50.0 %")
    window.list_view.set_activity(NEWEST, None)
    assert window.list_view.badge_of(item) is None
    assert format_percent(1 / 3) == "33.3 %"
    window.list_view.grab()  # paints the rows (label included) without errors
