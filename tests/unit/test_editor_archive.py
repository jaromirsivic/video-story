"""Archiving in the editor: menu labels, the queue, list labels, read-only archived view,
failed archives (the recording is given back), recordings that were asked for, the queue
that waits for the transcription an agent waits for."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402

from PySide6.QtCore import Qt  # noqa: E402
from test_editor import NEWEST, OLDER, _wait_loaded, make_recording  # noqa: E402
from test_editor_transcription import DONE, ScriptedManager, _spec  # noqa: E402

import video_story.editor.window as window_mod  # noqa: E402
from video_story.core.archive_state import (  # noqa: E402
    ArchiveStatus,
    read_archive_state,
    record_archive_failure,
    request_archive,
    retry_archive,
    write_archive_state,
)
from video_story.core.config import Config  # noqa: E402
from video_story.core.session import SessionPaths  # noqa: E402
from video_story.core.transcript import Segment, Transcript, write_transcript  # noqa: E402
from video_story.editor.archiving import (  # noqa: E402
    CANCEL_DONE,
    CANCEL_FINISHED,
    CANCEL_TOO_LATE,
    ArchiveManager,
)
from video_story.editor.recordings_view import (  # noqa: E402
    BADGE_ARCHIVED,
    BADGE_ARCHIVING,
    BADGE_TRANSCRIBING,
    format_percent,
)
from video_story.editor.window import (  # noqa: E402
    ARCHIVED_NOTE,
    ARCHIVING_MESSAGE,
    CANCEL_ARCHIVE_ONE_LABEL,
    FRAMES_DELETED_NOTE,
    NOT_BEING_ARCHIVED_NOTE,
    EditorWindow,
    archive_label,
    cancel_archive_label,
    delete_label,
)
from video_story.export.archive import cancel_archive  # noqa: E402
from video_story.host import editor_session as es  # noqa: E402

# Archives the recording with a stand-in for FFmpeg, like ``archive-stream`` does.
# Modes: "archive" (works), "hang" (waits until the editor closes stdin), "crash" (dies
# without recording anything, like a killed worker), "fail:<screenId>" (the export of
# that screen fails: ``archive_recording`` records the failure, the worker reports it).
# Modes for Cancel Archiving, which write ``<screenId>.part.mp4`` when the export of a
# screen starts: "slow" (the export of screen1 runs until stdin is closed, then it is
# cancelled like FFmpeg), "race" (the export of screen1 waits for the closed stdin and
# then completes - the screen is done, its frames deleted - before the stop is seen),
# "finish" (like "race", but the stop is never seen: the whole archive is done) and
# "deaf" (the export of screen1 never ends and stdin is not read: it must be killed).
FAKE_ARCHIVER = r"""
import json, sys, threading, time
from pathlib import Path
from video_story.core.config import Config
from video_story.core.session import SessionPaths
from video_story.export import ExportError
from video_story.export.archive import ArchiveError, archive_recording

final, mode = Path(sys.argv[1]), sys.argv[2]
if mode == "hang":
    sys.stdin.read()  # until the editor closes stdin
    sys.exit(3)
if mode == "crash":
    sys.stderr.write(json.dumps({"ok": False, "code": "X", "message": "worker crashed"}) + "\n")
    sys.exit(1)
fail_on = mode.split(":", 1)[1] if mode.startswith("fail:") else None
stop = threading.Event()
if mode in ("slow", "race", "finish"):

    def watch_stdin():
        sys.stdin.read()  # until the editor closes stdin (--stop-on-eof)
        stop.set()

    threading.Thread(target=watch_stdin, daemon=True).start()

def wait_for_stop():
    deadline = time.monotonic() + 30
    while not stop.is_set() and time.monotonic() < deadline:
        time.sleep(0.02)

def exporter(request, config, progress=None, cancel=None):
    if request.screen_id == fail_on:
        raise ExportError("FFMPEG_FAILED", "simulated failure")
    partial = request.output.with_name(request.output.stem + ".part.mp4")
    if mode in ("slow", "race", "finish", "deaf") and request.screen_id == "screen1":
        partial.write_bytes(b"part")
        if mode == "deaf":
            time.sleep(60)
        wait_for_stop()
        if mode == "slow":
            raise ExportError("CANCELLED", "export cancelled")  # partial left behind
        partial.unlink()
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
try:
    state = archive_recording(
        paths,
        Config(recording_folder=final.parent),
        progress=lambda f, _t: emit({"event": "progress", "phase": "archiving", "fraction": f}),
        cancel=None if mode == "finish" else stop.is_set,
        exporter=exporter,
        verifier=lambda *args: None,  # the stand-in MP4 is not a video
        audio_encoder=audio_encoder,
        audio_verifier=lambda *args: None,  # nor is the stand-in .m4a audio
    )
except ArchiveError as exc:
    sys.stderr.write(json.dumps({"ok": False, "code": exc.code, "message": str(exc)}) + "\n")
    sys.exit(1)
emit({"event": "done", "status": str(state.status), "leftovers": state.leftovers, "ok": True})
"""

THIRD = "20251201-090000"  # older than OLDER: the last row, the last one in the queue


class ScriptedArchiver(ArchiveManager):
    def __init__(self, config: Config, mode: str = "archive") -> None:
        super().__init__(config)
        self.mode = mode
        self.modes: dict[str, str] = {}  # session id -> mode, instead of ``mode``
        self.started_ids: list[str] = []
        self.started.connect(self.started_ids.append)
        self.failed_ids: list[str] = []
        self.failed.connect(lambda session_id, _message: self.failed_ids.append(session_id))
        self.cancel_outcomes: list[tuple[str, str]] = []
        self.cancelled.connect(lambda *outcome: self.cancel_outcomes.append(outcome))

    def command(self, paths: SessionPaths) -> list[str]:
        mode = self.modes.get(paths.session_id, self.mode)
        return [sys.executable, "-X", "utf8", "-c", FAKE_ARCHIVER, str(paths.final_dir), mode]


def _open(
    qtbot,
    folder: Path,
    mode: str = "archive",
    session_id: str | None = NEWEST,
    days: int = 30,
    **kwargs,
) -> EditorWindow:
    cfg = Config(recording_folder=folder, archive_after_days=days)
    window = EditorWindow(
        cfg, session_id=session_id, archiver=ScriptedArchiver(cfg, mode), **kwargs
    )
    window._confirm_archive = lambda items: True  # type: ignore[method-assign]
    qtbot.addWidget(window)
    window.show()
    return window


def _due(monkeypatch, *due: SessionPaths) -> None:
    """The recordings the automatic archiving finds old (conftest finds none)."""
    monkeypatch.setattr(window_mod, "recordings_due_for_archive", lambda *_a, **_k: list(due))


def _select(window: EditorWindow, session_id: str) -> None:
    window.list_view.setCurrentIndex(window.recordings.index(window.recordings.row_of(session_id)))


def _shows_notice(window: EditorWindow) -> bool:
    return window.recording is None and (window.preview.view.message or "").startswith(
        ARCHIVING_MESSAGE
    )


def _attach(window: EditorWindow, session_id: str, recording_session: str | None = None) -> None:
    """A later ``edit --session`` (or the control window) asks the running editor."""
    cfg = window.config
    es.write_attach_request(
        cfg, es.new_editor_session(cfg), session_id, recording_session=recording_session
    )
    assert [r.session_id for r in window.check_attach_requests()] == [session_id]


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


# A failed archive gives the recording back ------------------------------------


def _tooltip(window: EditorWindow, session_id: str) -> str:
    index = window.recordings.index(window.recordings.row_of(session_id))
    return window.recordings.data(index, Qt.ItemDataRole.ToolTipRole)


def test_crashed_archive_gives_the_recording_back_as_a_normal_one(qtbot, tmp_path: Path):
    """The worker dies without recording anything: the editor counts the attempt (once)
    and the recording is an ordinary one again - loaded, editable, processable."""
    make_recording(tmp_path, OLDER)
    paths = make_recording(tmp_path, NEWEST)
    window = _open(qtbot, tmp_path, mode="crash")
    _wait_loaded(qtbot, window, NEWEST)
    assert "Archiving failed" not in _tooltip(window, NEWEST)

    assert window.archive_recordings([_item(window, NEWEST)]) == [NEWEST]
    qtbot.waitUntil(lambda: _item(window, NEWEST).archive_failed, timeout=30_000)
    state = read_archive_state(paths)
    assert state.status == "failed" and state.failed_attempts == 1
    assert state.reason == "user" and state.error == "worker crashed"
    assert not window.archiver.is_queued(NEWEST)

    # It is the current row: loaded again, with everything it had.
    _wait_loaded(qtbot, window, NEWEST)
    assert window.recording.audio is not None and not window.recording.archived
    assert len(window.recording.frame_times("screen1")) == 5
    item = _item(window, NEWEST)
    assert not item.archiving and not item.archived
    assert window.list_view.badge_of(item) is None  # no ARCHIVING label
    assert "Archiving failed: worker crashed" in _tooltip(window, NEWEST)
    assert "Archiving failed" not in _tooltip(window, OLDER)
    assert window.statusBar().currentMessage() == f"Archiving {NEWEST} stopped: worker crashed"
    # Everything a recording can do.
    assert window.process_button.isEnabled() and window.instructions_button.isEnabled()
    assert window.render_block_reason([item]) is None
    assert window.movable([item]) == [item] and window.archivable([item]) == [item]
    processed: list[str] = []
    window.processed.connect(lambda session_id, _crop: processed.append(session_id))
    assert window.process() is not None and processed == [NEWEST]


def test_failure_recorded_by_the_worker_is_counted_once(qtbot, tmp_path: Path):
    paths = make_recording(tmp_path, NEWEST)
    window = _open(qtbot, tmp_path, mode="fail:screen1")
    _wait_loaded(qtbot, window, NEWEST)
    window.archive_recordings([_item(window, NEWEST)])
    qtbot.waitUntil(lambda: _item(window, NEWEST).archive_failed, timeout=30_000)
    state = read_archive_state(paths)
    assert state.failed_attempts == 1 and state.error == "simulated failure"
    assert state.audio == {"microphone": "audio.m4a"}  # kept for the next attempt
    _wait_loaded(qtbot, window, NEWEST)
    assert "Archiving failed: simulated failure" in _tooltip(window, NEWEST)


def test_archive_video_on_a_failed_recording_starts_over(qtbot, tmp_path: Path):
    paths = make_recording(tmp_path, NEWEST)
    request_archive(paths, reason="user")
    record_archive_failure(paths, "no space")
    window = _open(qtbot, tmp_path)
    _wait_loaded(qtbot, window, NEWEST)  # a normal recording; the user's is never retried
    assert window.archiver.started_ids == [] and _item(window, NEWEST).archive_failed
    assert read_archive_state(paths).failed_attempts == 1

    assert window.archive_recordings([_item(window, NEWEST)]) == [NEWEST]
    state = read_archive_state(paths)
    assert not state.failed and state.failed_attempts == 0 and state.reason == "user"
    assert _shows_notice(window)  # locked again while it is archived
    qtbot.waitUntil(lambda: _item(window, NEWEST).archived, timeout=30_000)
    assert window.archiver.started_ids == [NEWEST]
    assert read_archive_state(paths).failed_attempts == 0


def test_automatic_retry_stops_after_three_failures(qtbot, tmp_path: Path):
    old = make_recording(tmp_path, OLDER)
    make_recording(tmp_path, NEWEST)
    request_archive(old, reason="age")
    record_archive_failure(old, "first")
    retry_archive(old)
    record_archive_failure(old, "second")
    assert read_archive_state(old).failed_attempts == 2

    # Automatic archiving switched off (0 = never): no retry either.
    window = _open(qtbot, tmp_path, mode="crash", days=0)
    _wait_loaded(qtbot, window, NEWEST)
    assert window.archiver.started_ids == [] and read_archive_state(old).failed
    window.close()

    # The third attempt is made by the next editor - and fails too.
    window = _open(qtbot, tmp_path, mode="crash")
    assert window.archiver.started_ids == [OLDER]
    qtbot.waitUntil(lambda: _item(window, OLDER).archive_failed, timeout=30_000)
    state = read_archive_state(old)
    assert state.failed_attempts == 3 and state.reason == "age" and state.error == "worker crashed"
    window.close()

    # From now on it is left alone: a normal recording until the user archives it.
    window = _open(qtbot, tmp_path, session_id=None)
    _wait_loaded(qtbot, window, NEWEST)
    assert window.archiver.started_ids == [] and not window.archiver.is_queued(OLDER)
    assert read_archive_state(old).failed_attempts == 3
    _select(window, OLDER)
    _wait_loaded(qtbot, window, OLDER)
    assert window.process_button.isEnabled()
    assert window.archive_recordings([_item(window, OLDER)]) == [OLDER]
    assert read_archive_state(old).reason == "user"
    qtbot.waitUntil(lambda: _item(window, OLDER).archived, timeout=30_000)


def test_failed_archive_of_the_user_is_not_retried_automatically(qtbot, tmp_path: Path):
    old = make_recording(tmp_path, OLDER)
    make_recording(tmp_path, NEWEST)
    request_archive(old, reason="user")
    record_archive_failure(old, "first")
    window = _open(qtbot, tmp_path)
    _wait_loaded(qtbot, window, NEWEST)
    assert window.archiver.started_ids == [] and read_archive_state(old).failed


def test_half_archived_recording_stays_locked_and_is_resumed(qtbot, tmp_path: Path):
    """A failure after a screen is done (its frames are gone) does not give the
    recording back: it stays in the queue and the next editor renders the rest."""
    make_recording(tmp_path, OLDER)
    paths = make_recording(tmp_path, NEWEST)
    window = _open(qtbot, tmp_path, mode="fail:screen2")
    _wait_loaded(qtbot, window, NEWEST)
    window.archive_recordings([_item(window, NEWEST)])
    qtbot.waitUntil(
        lambda: window.archiver.running is None and bool(read_archive_state(paths).error),
        timeout=30_000,
    )
    state = read_archive_state(paths)
    assert state.status == "archiving" and state.screens_done == ["screen1"]
    assert state.failed_attempts == 0 and state.error == "simulated failure"
    item = _item(window, NEWEST)
    assert item.archiving and not item.archive_failed
    assert window.list_view.badge_of(item) == (BADGE_ARCHIVING, "50.0 %")
    assert "Archiving failed" not in _tooltip(window, NEWEST)
    assert _shows_notice(window) and window.archivable([item]) == []
    assert "simulated failure" in window.statusBar().currentMessage()
    window.close()

    resumed = _open(qtbot, tmp_path, session_id=OLDER)
    assert resumed.archiver.started_ids == [NEWEST]  # by itself, when the editor starts
    qtbot.waitUntil(lambda: _item(resumed, NEWEST).archived, timeout=30_000)
    state = read_archive_state(paths)
    assert state.screens_done == ["screen1", "screen2"] and state.error is None


# Recordings that were asked for ------------------------------------------------


def test_recording_the_editor_was_started_for_is_not_archived_by_age(
    qtbot, tmp_path: Path, monkeypatch
):
    old = make_recording(tmp_path, OLDER)
    third = make_recording(tmp_path, THIRD)
    make_recording(tmp_path, NEWEST)
    _due(monkeypatch, old, third)
    window = _open(qtbot, tmp_path, session_id=OLDER)  # edit --session OLDER
    _wait_loaded(qtbot, window, OLDER)
    assert read_archive_state(old) is None  # not even asked for
    assert window.process_button.isEnabled()
    qtbot.waitUntil(lambda: _item(window, THIRD).archived, timeout=30_000)
    assert window.archiver.started_ids == [THIRD]
    assert window.recording is not None and window.recording.session_id == OLDER


def test_age_request_left_by_an_earlier_editor_is_taken_back_for_the_asked_recording(
    qtbot, tmp_path: Path
):
    old = make_recording(tmp_path, OLDER)
    third = make_recording(tmp_path, THIRD)
    make_recording(tmp_path, NEWEST)
    request_archive(old, reason="age")  # queued by an editor that was closed at once
    request_archive(third, reason="user")  # the user's request is never taken back
    window = _open(qtbot, tmp_path, mode="hang", session_id=OLDER)
    _wait_loaded(qtbot, window, OLDER)
    assert read_archive_state(old) is None
    assert not old.archive_dir(old.final_dir).exists()
    assert window.archiver.started_ids == [THIRD]

    # Asked for, but the user's own archive of it is resumed: the notice stays.
    window.close()
    window = _open(qtbot, tmp_path, mode="hang", session_id=THIRD)
    qtbot.waitUntil(lambda: _shows_notice(window), timeout=10_000)
    assert window.archiver.running == THIRD and read_archive_state(third).status == "queued"


def test_attach_takes_back_an_age_request_that_has_not_started(qtbot, tmp_path: Path, monkeypatch):
    old = make_recording(tmp_path, OLDER)
    third = make_recording(tmp_path, THIRD)
    newest = make_recording(tmp_path, NEWEST)
    _due(monkeypatch, old, third)
    window = _open(qtbot, tmp_path, mode="hang")
    window._attach_timer.stop()  # the slot is triggered by hand below
    _wait_loaded(qtbot, window, NEWEST)
    assert window.archiver.running == OLDER and window.archiver.queued == [THIRD]
    assert read_archive_state(third).reason == "age"

    # THIRD waits in the queue: the request is taken back and the recording opens.
    _attach(window, THIRD)
    _wait_loaded(qtbot, window, THIRD)
    assert read_archive_state(third) is None and window.archiver.queued == []
    item = _item(window, THIRD)
    assert not item.archiving and window.list_view.badge_of(item) is None
    assert window.process_button.isEnabled()
    assert THIRD in window._archive_exempt

    # The user's own request waits in the queue too: never taken back.
    assert window.archive_recordings([_item(window, NEWEST)]) == [NEWEST]
    _attach(window, NEWEST)
    assert _shows_notice(window) and window.archiver.queued == [NEWEST]
    assert read_archive_state(newest).reason == "user"

    # OLDER is being worked on: not touched, the notice stays.
    _attach(window, OLDER)
    assert _shows_notice(window) and window.archiver.running == OLDER
    assert read_archive_state(old).status == "queued"


# The automatic queue waits for the awaited transcription -----------------------


def test_automatic_archiving_waits_for_the_awaited_transcription(
    qtbot, tmp_path: Path, monkeypatch
):
    """An agent waits for NEWEST, which is still being transcribed: old recordings are
    not archived meanwhile (Archive Video by hand is), and the one on screen when the
    queue finally starts is left alone."""
    old = make_recording(tmp_path, OLDER)
    third = make_recording(tmp_path, THIRD)
    fourth = make_recording(tmp_path, "20251101-090000")
    make_recording(tmp_path, NEWEST, with_transcript=False)
    _due(monkeypatch, old, third)
    spec = _spec(
        tmp_path,
        [
            {"event": "segment", "start_ms": 0, "end_ms": 1000, "text": "hello"},
            {"event": "progress", "fraction": 0.5, "transcribed_ms": 1000},
            {"sleep": 4.0},
            DONE,
        ],
    )
    cfg = Config(recording_folder=tmp_path)
    window = _open(
        qtbot, tmp_path, recording_session=NEWEST, transcription=ScriptedManager(cfg, spec)
    )
    _wait_loaded(qtbot, window, NEWEST)
    assert window.transcription.is_running(NEWEST)
    assert window._archive_queue_waits_for == NEWEST
    assert window.archiver.started_ids == []
    assert read_archive_state(old) is None and read_archive_state(third) is None

    # Archive Video by hand is not postponed.
    assert window.archive_recordings([_item(window, fourth.session_id)]) == [fourth.session_id]
    assert window.archiver.started_ids == [fourth.session_id]
    assert window.transcription.is_running(NEWEST) and read_archive_state(old) is None

    # The user looks at OLDER meanwhile; the transcription of NEWEST goes on.
    _select(window, OLDER)
    _wait_loaded(qtbot, window, OLDER)
    assert window.transcription.is_running(NEWEST) and window._archive_queue_waits_for == NEWEST

    # The transcript is complete: the queue starts, without the recording on screen.
    qtbot.waitUntil(lambda: window._archive_queue_waits_for is None, timeout=30_000)
    assert not window.transcription.is_running(NEWEST)
    assert read_archive_state(third).reason == "age"
    assert read_archive_state(old) is None
    qtbot.waitUntil(lambda: _item(window, THIRD).archived, timeout=30_000)
    assert window.archiver.started_ids == [fourth.session_id, THIRD]
    assert window.recording is not None and window.recording.session_id == OLDER
    assert read_archive_state(SessionPaths(tmp_path, NEWEST)) is None  # the awaited one


def test_automatic_archiving_starts_when_the_awaited_transcription_fails(
    qtbot, tmp_path: Path, monkeypatch
):
    old = make_recording(tmp_path, OLDER)
    make_recording(tmp_path, NEWEST, with_transcript=False)
    _due(monkeypatch, old)
    spec = _spec(tmp_path, [{"sleep": 1.0}], fail="no model")
    cfg = Config(recording_folder=tmp_path)
    window = _open(
        qtbot, tmp_path, recording_session=NEWEST, transcription=ScriptedManager(cfg, spec)
    )
    assert window._archive_queue_waits_for == NEWEST and window.archiver.started_ids == []
    qtbot.waitUntil(lambda: window._archive_queue_waits_for is None, timeout=30_000)
    assert NEWEST in window._transcription_failed
    qtbot.waitUntil(lambda: _item(window, OLDER).archived, timeout=30_000)
    assert window.archiver.started_ids == [OLDER]


def test_automatic_archiving_does_not_wait_for_a_complete_transcript(
    qtbot, tmp_path: Path, monkeypatch
):
    old = make_recording(tmp_path, OLDER)
    make_recording(tmp_path, NEWEST)  # the awaited recording is transcribed already
    _due(monkeypatch, old)
    window = _open(qtbot, tmp_path, mode="hang", recording_session=NEWEST)
    assert window._archive_queue_waits_for is None
    assert window.archiver.started_ids == [OLDER]


# Cancel Archiving ---------------------------------------------------------------

FOURTH = "20251101-090000"


def _messages(window: EditorWindow) -> list[str]:
    """Every status bar message from now on."""
    messages: list[str] = []
    window.statusBar().messageChanged.connect(messages.append)
    return messages


def _partial(paths: SessionPaths, screen_id: str = "screen1") -> Path:
    """What the fake archiver writes when the export of a screen starts."""
    return paths.archive_dir(paths.final_dir) / f"{screen_id}.part.mp4"


def _cancel_action(window: EditorWindow, selected: list[str]):
    """The Cancel Archiving action of the row menu (checked to sit under Archive Video)."""
    items = [_item(window, session_id) for session_id in selected]
    menu = window.recording_menu(items[0], items)
    actions = [action for action in menu.actions() if not action.isSeparator()]
    texts = [action.text() for action in actions]
    index = next(i for i, text in enumerate(texts) if text.startswith(CANCEL_ARCHIVE_ONE_LABEL))
    assert texts[index - 1].startswith("Archive Video")
    return actions[index]


def test_cancel_archiving_menu_action(qtbot, tmp_path: Path):
    assert cancel_archive_label(1) == "Cancel Archiving"
    assert cancel_archive_label(3) == "Cancel Archiving (3)"
    make_recording(tmp_path, OLDER)
    third = make_recording(tmp_path, THIRD)
    fourth = make_recording(tmp_path, FOURTH)
    make_recording(tmp_path, NEWEST)
    window = _open(qtbot, tmp_path, mode="hang")
    _wait_loaded(qtbot, window, NEWEST)

    action = _cancel_action(window, [NEWEST])
    assert action.text() == "Cancel Archiving" and not action.isEnabled()
    assert action.toolTip() == NOT_BEING_ARCHIVED_NOTE == "Not being archived"

    assert window.archive_recordings([_item(window, OLDER), _item(window, THIRD)]) == [
        OLDER,
        THIRD,
    ]
    assert window.archiver.running == OLDER and window.archiver.queued == [THIRD]
    action = _cancel_action(window, [NEWEST, OLDER, THIRD])  # N counts the cancellable
    assert action.text() == "Cancel Archiving (2)" and action.isEnabled()
    action = _cancel_action(window, [OLDER])
    assert action.text() == "Cancel Archiving" and action.isEnabled()

    # Left half done by an earlier editor: a screen is done, its frames are gone.
    state = request_archive(fourth, reason="age")
    state.status = ArchiveStatus.ARCHIVING
    state.screens = ["screen1", "screen2"]
    state.screens_done = ["screen1"]
    write_archive_state(fourth, state, fourth.final_dir)
    for selected in ([FOURTH], [NEWEST, FOURTH]):
        action = _cancel_action(window, selected)
        assert action.text() == "Cancel Archiving" and not action.isEnabled()
        assert action.toolTip() == FRAMES_DELETED_NOTE
        assert FRAMES_DELETED_NOTE == "Frames were already deleted; the archiving has to finish"

    action = _cancel_action(window, [FOURTH, THIRD])
    assert action.text() == "Cancel Archiving" and action.isEnabled()
    action.trigger()  # no question asked: nothing is lost
    assert read_archive_state(third).status == "cancelled"
    assert window.archiver.queued == [] and window.archiver.running == OLDER
    assert read_archive_state(fourth).status == "archiving"  # not touched


def test_cancel_a_waiting_recording_exempts_it_for_good(qtbot, tmp_path: Path, monkeypatch):
    make_recording(tmp_path, OLDER)
    third = make_recording(tmp_path, THIRD)
    make_recording(tmp_path, NEWEST)
    window = _open(qtbot, tmp_path, mode="hang")
    _wait_loaded(qtbot, window, NEWEST)
    window.archive_recordings([_item(window, OLDER), _item(window, THIRD)])
    assert window.archiver.running == OLDER and window.archiver.queued == [THIRD]
    _select(window, THIRD)
    qtbot.waitUntil(lambda: _shows_notice(window))
    messages = _messages(window)

    assert window.cancel_archiving([_item(window, THIRD)]) == [THIRD]

    assert window.archiver.queued == [] and window.archiver.running == OLDER
    assert window.archiver.cancel_outcomes == [(THIRD, CANCEL_DONE)]
    state = read_archive_state(third)
    assert state.status == "cancelled" and state.reason == "user"
    assert state.failed_attempts == 0 and state.error is None
    assert f"Archiving of {THIRD} cancelled" in messages
    # An ordinary recording: it opens, no label, no tooltip, Archive Video offered.
    _wait_loaded(qtbot, window, THIRD)
    item = _item(window, THIRD)
    assert not item.archiving and not item.archived and not item.archive_failed
    assert window.list_view.badge_of(item) is None
    assert "Archiving" not in _tooltip(window, THIRD)
    assert window.archivable([item]) == [item] and window.cancellable([item]) == []
    assert window.statusBar().currentMessage() == f"Archiving of {THIRD} cancelled"
    assert window.process_button.isEnabled()
    window.close()

    # A fresh editor with the real automatic archiving: never due, never retried.
    from video_story.host import retention

    monkeypatch.setattr(
        window_mod, "recordings_due_for_archive", retention.recordings_due_for_archive
    )
    window = _open(qtbot, tmp_path, mode="hang")  # NEWEST is exempt (asked for)
    assert window.archiver.started_ids == [OLDER]  # the user's stopped archive resumes
    assert not window.archiver.is_queued(THIRD)
    assert read_archive_state(third).status == "cancelled"
    _select(window, THIRD)
    _wait_loaded(qtbot, window, THIRD)


def test_cancel_the_recording_being_archived_and_the_queue_goes_on(qtbot, tmp_path: Path):
    make_recording(tmp_path, OLDER)
    paths = make_recording(tmp_path, NEWEST)
    archive = paths.archive_dir(paths.final_dir)
    window = _open(qtbot, tmp_path, mode="slow")
    window.archiver.modes[OLDER] = "archive"
    _wait_loaded(qtbot, window, NEWEST)
    messages = _messages(window)
    window.archive_recordings([_item(window, NEWEST), _item(window, OLDER)])
    assert window.archiver.running == NEWEST and window.archiver.queued == [OLDER]
    qtbot.waitUntil(lambda: _partial(paths).is_file(), timeout=30_000)

    started = time.monotonic()
    assert window.cancel_archiving([_item(window, NEWEST)]) == [NEWEST]
    assert time.monotonic() - started < 2  # the window does not wait for the worker
    assert window.archiver.running == NEWEST and window.archiver.cancel_outcomes == []
    assert window.cancellable([_item(window, NEWEST)])  # under way: still offered

    qtbot.waitUntil(lambda: bool(window.archiver.cancel_outcomes), timeout=30_000)
    assert window.archiver.cancel_outcomes == [(NEWEST, CANCEL_DONE)]
    state = read_archive_state(paths)
    assert state.status == "cancelled" and state.failed_attempts == 0 and state.error is None
    assert state.audio == {"microphone": "audio.m4a"} and state.screens_done == []
    assert sorted(p.name for p in archive.iterdir()) == ["archive.json", "audio.m4a"]
    assert len(list(paths.frames_dir(paths.final_dir).glob("*_screen1.png"))) == 5
    assert window.archiver.failed_ids == [] and window.archiver.failures == {}
    assert f"Archiving of {NEWEST} cancelled" in messages
    assert not any("stopped" in message for message in messages)

    # The queue goes on with the next recording.
    qtbot.waitUntil(lambda: _item(window, OLDER).archived, timeout=30_000)
    assert window.archiver.started_ids == [NEWEST, OLDER]
    # The cancelled one is the current row: loaded again, editable.
    _wait_loaded(qtbot, window, NEWEST)
    assert window.recording.audio is not None and not window.recording.archived
    item = _item(window, NEWEST)
    assert not item.archiving and window.list_view.badge_of(item) is None
    assert window.process_button.isEnabled() and window.render_block_reason([item]) is None
    assert window.movable([item]) == [item]

    # Archive Video by hand works and reuses the verified audio copy.
    (archive / "audio.m4a").write_bytes(b"kept")
    window.archiver.modes[NEWEST] = "archive"
    assert window.archivable([item]) == [item]
    assert window.archive_recordings([item]) == [NEWEST]
    state = read_archive_state(paths)
    assert state.reason == "user" and state.failed_attempts == 0
    assert state.audio == {"microphone": "audio.m4a"}
    qtbot.waitUntil(lambda: _item(window, NEWEST).archived, timeout=30_000)
    assert (archive / "audio.m4a").read_bytes() == b"kept"  # not encoded again


def test_cancel_too_late_finishes_the_archive_without_a_failure(qtbot, tmp_path: Path):
    """The worker completes screen1 (its frames are deleted) before it sees the stop:
    the recording cannot be given back; it is archived to the end, nothing counted."""
    make_recording(tmp_path, OLDER)
    paths = make_recording(tmp_path, NEWEST)
    window = _open(qtbot, tmp_path, mode="race", session_id=OLDER)
    _wait_loaded(qtbot, window, OLDER)
    messages = _messages(window)
    seen: list = []
    window.archiver.cancelled.connect(lambda *_a: seen.append(read_archive_state(paths)))
    window.archive_recordings([_item(window, NEWEST)])
    qtbot.waitUntil(lambda: _partial(paths).is_file(), timeout=30_000)

    assert window.cancel_archiving([_item(window, NEWEST)]) == [NEWEST]

    qtbot.waitUntil(lambda: bool(window.archiver.cancel_outcomes), timeout=30_000)
    assert window.archiver.cancel_outcomes == [(NEWEST, CANCEL_TOO_LATE)]
    assert seen[0].status == "archiving" and seen[0].screens_done == ["screen1"]
    assert seen[0].failed_attempts == 0
    assert any("could not be cancelled any more" in message for message in messages)
    qtbot.waitUntil(lambda: _item(window, NEWEST).archived, timeout=30_000)
    state = read_archive_state(paths)
    assert state.screens_done == ["screen1", "screen2"]
    assert state.failed_attempts == 0 and state.error is None
    assert window.archiver.started_ids == [NEWEST, NEWEST]  # put back into the queue
    assert window.archiver.failed_ids == [] and window.archiver.failures == {}


def test_cancel_after_the_whole_archive_was_done_keeps_it_archived(qtbot, tmp_path: Path):
    make_recording(tmp_path, OLDER)
    paths = make_recording(tmp_path, NEWEST)
    window = _open(qtbot, tmp_path, mode="finish", session_id=OLDER)
    _wait_loaded(qtbot, window, OLDER)
    messages = _messages(window)
    window.archive_recordings([_item(window, NEWEST)])
    qtbot.waitUntil(lambda: _partial(paths).is_file(), timeout=30_000)

    assert window.cancel_archiving([_item(window, NEWEST)]) == [NEWEST]

    qtbot.waitUntil(lambda: bool(window.archiver.cancel_outcomes), timeout=30_000)
    assert window.archiver.cancel_outcomes == [(NEWEST, CANCEL_FINISHED)]
    state = read_archive_state(paths)
    assert state.archived and state.failed_attempts == 0
    assert _item(window, NEWEST).archived and window.archiver.started_ids == [NEWEST]
    assert window.archiver.failed_ids == []
    assert any("could not be cancelled any more" in message for message in messages)


def test_cancelled_worker_that_has_to_be_killed_ends_cancelled(qtbot, tmp_path: Path):
    """The worker does not react to its closed stdin: it is killed, and that is still a
    cancel, not a failure ("worker died without a word")."""
    make_recording(tmp_path, OLDER)
    paths = make_recording(tmp_path, NEWEST)
    window = _open(qtbot, tmp_path, mode="deaf", session_id=OLDER)
    window.archiver.cancel_wait_ms = 300
    _wait_loaded(qtbot, window, OLDER)
    messages = _messages(window)
    window.archive_recordings([_item(window, NEWEST)])
    qtbot.waitUntil(lambda: _partial(paths).is_file(), timeout=30_000)

    assert window.cancel_archiving([_item(window, NEWEST)]) == [NEWEST]

    qtbot.waitUntil(lambda: bool(window.archiver.cancel_outcomes), timeout=30_000)
    assert window.archiver.cancel_outcomes == [(NEWEST, CANCEL_DONE)]
    state = read_archive_state(paths)
    assert state.status == "cancelled" and state.failed_attempts == 0 and state.error is None
    assert state.audio == {"microphone": "audio.m4a"}
    assert not _partial(paths).exists()  # left by the killed worker, removed
    assert window.archiver.failed_ids == [] and window.archiver.failures == {}
    assert f"Archiving of {NEWEST} cancelled" in messages
    assert window.archiver.running is None


def test_cancelled_recording_loads_and_can_be_processed(qtbot, tmp_path: Path):
    paths = make_recording(tmp_path, NEWEST)
    request_archive(paths, reason="age")
    record_archive_failure(paths, "first")
    retry_archive(paths)
    assert cancel_archive(paths, failed_attempts=1).cancelled
    window = _open(qtbot, tmp_path)
    _wait_loaded(qtbot, window, NEWEST)
    assert window.archiver.started_ids == []  # never retried
    item = _item(window, NEWEST)
    assert item.archive == "cancelled"
    assert not item.archiving and not item.archived and not item.archive_failed
    assert window.list_view.badge_of(item) is None and "Archiving" not in _tooltip(window, NEWEST)
    assert window.cancellable([item]) == [] and window.archivable([item]) == [item]
    assert window.recording.audio is not None and len(window.recording.frame_times("screen1")) == 5
    assert window.process_button.isEnabled() and window.instructions_button.isEnabled()
    processed: list[str] = []
    window.processed.connect(lambda session_id, _crop: processed.append(session_id))
    assert window.process() is not None and processed == [NEWEST]
