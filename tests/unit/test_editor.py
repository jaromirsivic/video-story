"""Tests for the editor: timeline model, recordings list, window (offscreen Qt)."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import json  # noqa: E402
import re  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
import wave  # noqa: E402
from pathlib import Path  # noqa: E402

import numpy as np  # noqa: E402
import pytest  # noqa: E402
from PIL import Image  # noqa: E402
from PySide6.QtCore import QEvent, QPoint, QPointF, QRunnable, Qt  # noqa: E402
from PySide6.QtGui import QFocusEvent, QImage, QMouseEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from video_story.core.config import Config  # noqa: E402
from video_story.core.manifest import AudioInfo, Manifest, write_manifest  # noqa: E402
from video_story.core.naming import frame_filename, sidecar_filename  # noqa: E402
from video_story.core.screens import RawScreen, assign_screen_ids  # noqa: E402
from video_story.core.session import SessionPaths  # noqa: E402
from video_story.core.transcript import Segment, Transcript, write_transcript  # noqa: E402
from video_story.editor import player as player_mod  # noqa: E402
from video_story.editor.preview import IMAGE_CACHE_SIZE  # noqa: E402
from video_story.editor.recording import LoadedRecording  # noqa: E402
from video_story.editor.recordings_model import (  # noqa: E402
    RecordingsModel,
    folder_size,
    format_size,
    scan_recordings,
)
from video_story.editor.timeline import thumbnails as thumbnails_mod  # noqa: E402
from video_story.editor.timeline.model import MAX_ZOOM_PX_PER_S, TimelineModel  # noqa: E402
from video_story.editor.timeline.thumbnails import ThumbnailCache  # noqa: E402
from video_story.editor.timeline.view import (  # noqa: E402
    COLOR_HANDLE,
    COLOR_PLAYHEAD,
    HEADER_W,
    RULER_H,
    tick_label,
    tick_step_ms,
)
from video_story.editor.timeline.waveform import column_peaks, compute_peaks  # noqa: E402
from video_story.editor.window import EditorWindow, format_crop_time  # noqa: E402

FRAME_TIMES = (0, 500, 1000, 1500, 2000)
DURATION_MS = 2500
SAMPLE_RATE = 16000
NEWEST = "20260102-090000"
OLDER = "20260101-120000"


def make_recording(
    root: Path,
    session_id: str = NEWEST,
    with_transcript: bool = True,
    first_text: str = "hello world",
    duration_ms: int = DURATION_MS,
    audio_ms: int | None = None,
    segments: list[Segment] | None = None,
) -> SessionPaths:
    """A processed recording: two screens, five tiny PNG frames each, WAV, transcript.

    ``audio_ms`` makes the WAV shorter than the recording; ``segments`` replaces the
    default transcript.
    """
    paths = SessionPaths(root, session_id)
    final = paths.final_dir
    frames = paths.frames_dir(final)
    info = paths.frames_info_dir(final)
    frames.mkdir(parents=True)
    info.mkdir(parents=True)
    screens = assign_screen_ids(
        [RawScreen(0, 0, 320, 200, primary=True), RawScreen(320, 0, 160, 100)]
    )
    for t in FRAME_TIMES:
        cursor = {"screen": "screen1" if t < 1000 else "screen2", "x": 10 + t // 100, "y": 5}
        for sid, size, colour in (
            ("screen1", (320, 200), (t // 10 % 255, 80, 40)),
            ("screen2", (160, 100), (40, 80, t // 10 % 255)),
        ):
            Image.new("RGB", size, colour).save(frames / frame_filename(t, sid, "png"))
            (info / sidecar_filename(t, sid)).write_text(
                json.dumps({"time_ms": t, "screen": sid, "cursor": cursor}), encoding="utf-8"
            )
    n = SAMPLE_RATE * (DURATION_MS if audio_ms is None else audio_ms) // 1000
    tt = np.arange(n) / SAMPLE_RATE
    samples = (np.sin(2 * np.pi * 440 * tt) * 12000).astype("<i2")
    paths.audio_dir(final).mkdir(parents=True, exist_ok=True)
    with wave.open(str(paths.audio_path(final)), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(samples.tobytes())
    if with_transcript:
        write_transcript(
            paths.transcript_txt_path(final),
            paths.transcript_json_path(final),
            Transcript(
                "en",
                segments
                if segments is not None
                else [Segment(0, 1000, first_text), Segment(1500, 2500, "second line")],
            ),
        )
    write_manifest(
        paths.manifest_path(final),
        Manifest(
            session_id=session_id,
            started_at="2026-01-01T12:00:00+01:00",
            finished_at=None,
            recorded_duration_ms=duration_ms,
            capture_interval_ms=500,
            image_format="png",
            jpeg_quality=None,
            screens=screens,
            audio=AudioInfo(sample_rate=SAMPLE_RATE, duration_ms=DURATION_MS),
        ),
    )
    return paths


# TimelineModel ----------------------------------------------------------------


def test_model_fit_and_conversions():
    model = TimelineModel(10_000, viewport_width=1000)
    assert model.zoom == pytest.approx(100.0)  # 10 s fill 1000 px
    assert model.time_to_x(5000) == pytest.approx(500.0)
    assert model.x_to_time(250) == 2500
    assert model.visible_ms() == pytest.approx(10_000)
    assert model.max_scroll_ms() == 0
    model.fit(500)
    assert model.viewport_width == 500 and model.zoom == pytest.approx(50.0)


def test_model_zoom_around_keeps_anchor_and_is_bounded():
    model = TimelineModel(10_000, viewport_width=1000)
    model.zoom_around(500, 2.0)
    assert model.zoom == pytest.approx(200.0)
    assert model.x_to_time(500) == 5000  # the time under the mouse stays put
    assert model.scroll_ms == 2500
    model.zoom_around(500, 10.0)
    assert model.zoom == MAX_ZOOM_PX_PER_S
    model.zoom_around(0, 0.01)
    assert model.zoom == pytest.approx(model.min_zoom()) and model.scroll_ms == 0
    model.set_zoom_fraction(1.0)
    assert model.zoom == MAX_ZOOM_PX_PER_S and model.zoom_fraction() == pytest.approx(1.0)
    model.set_zoom_fraction(0.0)
    assert model.zoom == pytest.approx(model.min_zoom())


def test_model_scroll_clamped_and_ensure_visible():
    model = TimelineModel(60_000, viewport_width=600)
    model.set_zoom(200.0)  # 3 s visible
    model.set_scroll(999_999)
    assert model.scroll_ms == 57_000
    model.set_scroll(-5)
    assert model.scroll_ms == 0
    assert model.ensure_visible(1000) is False
    assert model.ensure_visible(30_000) is True
    assert model.scroll_ms <= 30_000 < model.scroll_ms + model.visible_ms()
    model.set_viewport_width(60)  # a narrower view keeps the zoom
    assert model.zoom == 200.0


def test_model_crop_clamping_minimum_range_and_reset():
    model = TimelineModel(10_000)
    assert (model.in_ms, model.out_ms) == (0, 10_000)
    assert model.set_in(9_500) and model.in_ms == 9_000  # keeps 1 s before out
    assert model.set_out(100) is False and model.out_ms == 10_000
    model.reset_crop()
    assert model.set_in(-50) is False and model.in_ms == 0
    assert model.set_out(20_000) is False and model.out_ms == 10_000
    model.set_in(2000)
    model.set_out(2500)
    assert (model.in_ms, model.out_ms) == (2000, 3000)
    assert model.crop_length_ms == 1000
    assert model.reset_crop() and (model.in_ms, model.out_ms) == (0, 10_000)
    assert model.reset_crop() is False


def test_model_short_recording_uses_whole_duration_as_minimum():
    model = TimelineModel(600)
    assert model.min_range_ms == 600
    assert model.set_in(300) is False and model.in_ms == 0
    assert model.set_out(100) is False and model.out_ms == 600
    assert model.set_playhead(5000) and model.playhead_ms == 600
    assert model.set_playhead(-1) and model.playhead_ms == 0


def test_model_screens_and_selection():
    model = TimelineModel(1000)
    model.set_screens(["screen1", "screen2"])
    assert model.selected_screen == "screen1"
    assert model.select_screen("screen2") and model.selected_screen == "screen2"
    assert model.select_screen("screen9") is False
    model.set_screens(["screen3"])
    assert model.selected_screen == "screen3"


def test_ruler_tick_steps_and_labels():
    assert tick_step_ms(200.0) == 500
    assert tick_step_ms(1.0) == 120_000
    assert tick_label(0, 500) == "0:00.0"
    assert tick_label(65_500, 500) == "1:05.5"
    assert tick_label(3_725_000, 5000) == "1:02:05"


# Recording and workers --------------------------------------------------------


def test_loaded_recording_lookup(tmp_path: Path):
    paths = make_recording(tmp_path)
    rec = LoadedRecording.load(paths)
    assert rec.duration_ms == DURATION_MS
    assert rec.screen_ids == ["screen1", "screen2"]
    assert rec.screen_size("screen2") == (160, 100)
    assert rec.frame_at("screen1", 700).time_ms == 500
    assert rec.frame_at("screen1", -5).time_ms == 0
    assert rec.frame_at("screen1", 9_999).time_ms == 2000
    assert rec.next_frame_after("screen1", 500).time_ms == 1000
    assert rec.next_frame_after("screen1", 2000) is None
    assert rec.frame_at("screen7", 0) is None
    assert rec.cursor_at("screen1", 1200).screen_id == "screen2"
    assert rec.cursor_at("screen1", 0).x == 10
    assert rec.subtitle_at(1700) == "second line"
    assert rec.subtitle_at(1200) == ""
    assert rec.subtitle_at(1000) == ""  # the first segment is half-open: [0, 1000)
    assert rec.subtitle_at(2500) == "second line"  # the end of the last segment counts
    assert rec.subtitle_at(2501) == ""


def test_subtitle_single_segment_at_touching_boundary(tmp_path: Path):
    paths = make_recording(
        tmp_path,
        segments=[
            Segment(0, 1000, "one"),
            Segment(1000, 2000, "two"),
            Segment(2000, 2500, "three"),
        ],
    )
    rec = LoadedRecording.load(paths)
    assert rec.subtitle_at(0) == "one"
    assert rec.subtitle_at(999) == "one"
    assert rec.subtitle_at(1000) == "two"  # not "one two"
    assert rec.subtitle_at(2000) == "three"
    assert rec.subtitle_at(2500) == "three"
    assert rec.audio is not None and rec.audio.sample_rate == SAMPLE_RATE
    assert rec.audio.duration_ms == DURATION_MS


def test_waveform_peaks(tmp_path: Path):
    paths = make_recording(tmp_path)
    peaks = compute_peaks(paths.audio_path(paths.final_dir))
    assert peaks.shape == (DURATION_MS // 20, 2)
    assert float(peaks[:, 1].max()) == pytest.approx(12000 / 32768, abs=0.01)
    assert float(peaks[:, 0].min()) == pytest.approx(-12000 / 32768, abs=0.01)
    columns = column_peaks(peaks, 20, 0.0, 250.0, 10)
    assert columns.shape == (10, 2) and columns[0, 1] > 0.3 > columns[0, 0]


# Recordings list ---------------------------------------------------------------


def test_recordings_model_order_default_and_first_line(tmp_path: Path):
    make_recording(tmp_path, OLDER, first_text="older one")
    make_recording(tmp_path, NEWEST)
    make_recording(tmp_path, "20251231-000000", with_transcript=False)
    # Unprocessed (work only) and broken (no manifest) sessions are not listed.
    (tmp_path / ".work" / "20260103-000000").mkdir(parents=True)
    (tmp_path / "default" / "20260104-000000").mkdir()

    items = scan_recordings(tmp_path)
    assert [i.session_id for i in items] == [NEWEST, OLDER, "20251231-000000"]
    assert items[0].first_line == "hello world"
    assert items[1].first_line == "older one"
    assert items[2].first_line == ""
    assert "02.01.2026 09:00:00" in items[0].display_text
    # The length in whole seconds; the screens are the row's dots, not text.
    assert re.fullmatch(r"00:02 \(\d+\.\d KB\)", items[0].details)
    assert "screens" not in items[0].display_text
    assert items[0].screen_ids == ("screen1", "screen2")
    assert [s[2] for s in items[0].segments] == ["hello world", "second line"]
    assert items[0].text_at(0) == "hello world"
    assert items[0].text_at(1200) == "hello world"  # a pause keeps the previous sentence
    assert items[0].text_at(2000) == "second line"

    model = RecordingsModel(tmp_path)
    assert model.rowCount() == 4  # the header of the default folder, then the recordings
    assert model.header(0).label == "default (3)" and model.item(0) is None
    assert model.default_row() == 1
    assert model.default_row(OLDER) == 2
    assert model.default_row("20200101-000000") == 1
    assert model.data(model.index(2), Qt.ItemDataRole.DisplayRole).startswith("01.01.2026")
    (tmp_path / "default" / NEWEST / "manifest.json").unlink()
    model.refresh()
    assert model.rowCount() == 3 and model.default_row() == 1


def test_recording_length_shows_hours_only_when_nonzero(tmp_path: Path):
    make_recording(tmp_path, NEWEST, duration_ms=65 * 60_000)
    make_recording(tmp_path, OLDER)
    items = {item.session_id: item for item in scan_recordings(tmp_path)}
    assert items[NEWEST].details.startswith("1:05:00 (")
    assert items[OLDER].details.startswith("00:02 (")  # no milliseconds


def test_size_format_and_folder_size(tmp_path: Path):
    assert format_size(0) == "0.0 KB" and format_size(500) == "0.5 KB"
    assert format_size(1536) == "1.5 KB" and format_size(1023 * 1024) == "1023.0 KB"
    assert format_size(1024 * 1024 - 10) == "1.0 MB"  # never "1024.0 KB"
    assert format_size(int(2.5 * 1024**2)) == "2.5 MB"
    assert format_size(3 * 1024**3) == "3.0 GB" and format_size(5 * 1024**4) == "5.0 TB"
    assert format_size(2048 * 1024**4) == "2048.0 TB"
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "x.bin").write_bytes(b"1" * 1000)
    (tmp_path / "y.bin").write_bytes(b"1" * 24)
    assert folder_size(tmp_path) == 1024
    assert folder_size(tmp_path / "missing") == 0


def test_recordings_model_empty_folder(tmp_path: Path):
    model = RecordingsModel(tmp_path / "missing")
    # Only the header of the default folder, which is created when it is missing.
    assert model.rowCount() == 1 and model.default_row() == -1
    assert (tmp_path / "missing" / "default").is_dir()


# Editor window -----------------------------------------------------------------


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    make_recording(tmp_path, OLDER, first_text="older one")
    make_recording(tmp_path, NEWEST)
    return tmp_path


def _wait_loaded(qtbot, window: EditorWindow, session_id: str) -> None:
    qtbot.waitUntil(
        lambda: window.recording is not None and window.recording.session_id == session_id,
        timeout=10_000,
    )


def _mouse_move(widget, pos: QPoint) -> None:
    """A mouse-move with the left button held (QTest.mouseMove does not carry buttons)."""
    event = QMouseEvent(
        QEvent.Type.MouseMove,
        QPointF(pos),
        QPointF(widget.mapToGlobal(pos)),
        Qt.MouseButton.NoButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    QApplication.sendEvent(widget, event)


class FakeStream:
    """Stands in for ``sounddevice.OutputStream``: a thread pumps the callback."""

    instances: list[FakeStream] = []
    die_after: int | None = None  # callbacks before PortAudio "stops" the stream silently
    fail_start = False

    def __init__(self, samplerate, channels, dtype, callback):
        self.samplerate = samplerate
        self.callback = callback
        self.latency = 0.0
        self.active = False
        self.aborted = False
        self.stopped = False
        self.closed = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        FakeStream.instances.append(self)

    def start(self):
        if FakeStream.fail_start:
            raise RuntimeError("Pa_StartStream failed")
        self.active = True
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()

    def _pump(self):
        frames = 160
        calls = 0
        while not self._stop.is_set():
            if FakeStream.die_after is not None and calls >= FakeStream.die_after:
                self.active = False  # like a lost device: no CallbackStop, no more callbacks
                return
            out = np.zeros((frames, 1), dtype=np.int16)
            try:
                self.callback(out, frames, None, None)
            except FakeSD.CallbackStop:
                self.active = False
                return
            calls += 1
            self._stop.wait(0.005)

    def _halt(self):
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)
        self.active = False

    def abort(self):
        self.aborted = True
        self._halt()

    def stop(self):
        self.stopped = True
        self._halt()

    def close(self):
        self.closed = True
        self._halt()


class FakeSD:
    class CallbackStop(Exception):
        pass

    OutputStream = FakeStream


@pytest.fixture
def fake_sd(monkeypatch):
    FakeStream.instances = []
    FakeStream.die_after = None
    FakeStream.fail_start = False
    monkeypatch.setattr(player_mod, "_load_sounddevice", lambda: FakeSD)
    return FakeSD


@pytest.fixture
def no_sd(monkeypatch):
    monkeypatch.setattr(player_mod, "_load_sounddevice", lambda: None)


@pytest.fixture
def editor(qtbot, folder: Path):
    window = EditorWindow(Config(recording_folder=folder))
    qtbot.addWidget(window)
    window.show()
    _wait_loaded(qtbot, window, NEWEST)
    return window


def test_window_loads_newest_recording_and_workers(editor, qtbot):
    assert editor.windowTitle() == "Video Story Editor"
    assert editor.list_view.currentIndex().row() == 1  # row 0: the default folder's header
    assert editor.model.duration_ms == DURATION_MS
    assert editor.model.screens == ["screen1", "screen2"]
    assert editor.model.selected_screen == "screen1"
    assert (editor.model.in_ms, editor.model.out_ms) == (0, DURATION_MS)
    assert editor.preview.time_label.text() == "00:00:00.00 / 00:00:02.50"
    assert NEWEST in editor.status_label.text() and "320x200" in editor.status_label.text()
    assert editor.in_edit.text() == "00:00:00.00" and editor.out_edit.text() == "00:00:02.50"

    qtbot.waitUntil(lambda: editor.timeline.waveform.peaks is not None, timeout=10_000)
    assert editor.timeline.waveform.peaks.shape[0] == DURATION_MS // 20
    editor.timeline.grab()  # paints the tracks, which schedules the visible thumbnails
    qtbot.waitUntil(lambda: editor.timeline.thumbnails.pending == 0, timeout=10_000)
    assert len(editor.timeline.thumbnails) > 0
    qtbot.waitUntil(lambda: editor.preview.view.shown_path is not None, timeout=10_000)
    assert editor.preview.view.shown_path.endswith("0_000_screen1.png")
    editor.preview.view.grab()  # paints the frame plus the cursor arrow


def test_window_session_argument_selects_that_recording(qtbot, folder: Path):
    window = EditorWindow(Config(recording_folder=folder), session_id=OLDER)
    qtbot.addWidget(window)
    window.show()
    _wait_loaded(qtbot, window, OLDER)
    assert window.list_view.currentIndex().row() == 2
    window.list_view.setCurrentIndex(window.recordings.index(1))
    _wait_loaded(qtbot, window, NEWEST)
    window.refresh_recordings()
    assert window.recording.session_id == NEWEST


def test_audio_track_is_as_tall_as_the_screen_tracks(editor):
    heights = {rect.height() for _screen, rect in editor.timeline.track_rects()}
    assert heights == {editor.timeline.audio_rect().height()}


def test_track_click_selects_screen_and_preview(editor, qtbot):
    rects = dict(editor.timeline.track_rects())
    rect = rects["screen2"]
    pos = QPoint(HEADER_W + 20, rect.center().y())
    qtbot.mouseClick(editor.timeline, Qt.MouseButton.LeftButton, pos=pos)
    assert editor.model.selected_screen == "screen2"
    assert "screen2 160x100" in editor.status_label.text()
    qtbot.waitUntil(
        lambda: (editor.preview.view.shown_path or "").endswith("_screen2.png"), timeout=10_000
    )
    # Clicking the header of screen1 switches back; the playhead is untouched there.
    editor.model.set_playhead(900)
    qtbot.mouseClick(
        editor.timeline, Qt.MouseButton.LeftButton, pos=QPoint(10, rects["screen1"].center().y())
    )
    assert editor.model.selected_screen == "screen1"
    assert editor.model.playhead_ms == 900


def test_io_keys_time_editors_and_reset_crop(editor, qtbot):
    editor.model.set_playhead(700)
    qtbot.keyClick(editor.timeline, Qt.Key_I)
    assert editor.model.in_ms == 700
    assert editor.in_edit.text() == "00:00:00.70"
    editor.model.set_playhead(2200)
    qtbot.keyClick(editor.timeline, Qt.Key_O)
    assert editor.model.out_ms == 2200
    assert editor.out_edit.text() == "00:00:02.20"
    assert "length 00:00:01.50" in editor.crop_label.text()
    # Typing into the In editor and pressing Return commits (clamped to keep one second).
    editor.in_edit.selectAll()
    qtbot.keyClicks(editor.in_edit, "00:00:01.900")
    qtbot.keyClick(editor.in_edit, Qt.Key_Return)
    assert editor.model.in_ms == 1200
    assert editor.in_edit.text() == "00:00:01.20"
    editor.reset_crop_button.click()
    assert (editor.model.in_ms, editor.model.out_ms) == (0, DURATION_MS)
    assert editor.in_edit.text() == "00:00:00.00"
    # Home/End and the step keys move the playhead.
    editor.model.set_in(1000)
    editor._crop_updated()
    qtbot.keyClick(editor.timeline, Qt.Key_Home)
    assert editor.model.playhead_ms == 1000
    qtbot.keyClick(editor.timeline, Qt.Key_Right)
    assert editor.model.playhead_ms == 1042
    qtbot.keyClick(editor.timeline, Qt.Key_Left, Qt.KeyboardModifier.ShiftModifier)
    assert editor.model.playhead_ms == 42
    qtbot.keyClick(editor.timeline, Qt.Key_End)
    assert editor.model.playhead_ms == DURATION_MS


def test_time_edit_commits_on_return_and_focus_out_and_reverts_partial_text(editor, qtbot):
    edit = editor.in_edit
    editor.model.set_out(2200)
    editor._crop_updated()
    edit.selectAll()
    qtbot.keyClicks(edit, "00:00:01.900")
    qtbot.keyClick(edit, Qt.Key_Return)
    assert editor.model.in_ms == 1200 and edit.text() == "00:00:01.20"
    # A partial entry does not survive Enter: it reverts to the last value.
    edit.selectAll()
    qtbot.keyClicks(edit, "00:00:0")
    assert edit.text() == "00:00:0"
    qtbot.keyClick(edit, Qt.Key_Enter)
    assert editor.model.in_ms == 1200 and edit.text() == "00:00:01.20"
    # HH:MM:SS without milliseconds is accepted; focus-out commits it.
    edit.selectAll()
    qtbot.keyClicks(edit, "00:00:00")
    QApplication.sendEvent(edit, QFocusEvent(QEvent.Type.FocusOut))
    assert editor.model.in_ms == 0 and edit.text() == "00:00:00.00"
    # Letters are refused by the validator, so the text cannot become garbage.
    edit.selectAll()
    qtbot.keyClicks(edit, "garbage")
    assert edit.text() == "00:00:00.00"
    # The Out editor: partial text plus focus-out reverts too.
    out = editor.out_edit
    out.selectAll()
    qtbot.keyClicks(out, "00:00:1")
    QApplication.sendEvent(out, QFocusEvent(QEvent.Type.FocusOut))
    assert editor.model.out_ms == 2200 and out.text() == "00:00:02.20"
    assert "length 00:00:02.20" in editor.crop_label.text()


def test_subtitle_label_follows_playhead(editor):
    editor.model.set_playhead(500)
    editor._sync_playhead()
    assert editor.preview.subtitle_label.text() == "hello world"
    assert editor.preview.time_label.text().startswith("00:00:00.50 /")
    editor.model.set_playhead(1200)
    editor._sync_playhead()
    assert editor.preview.subtitle_label.text() == ""
    editor.model.set_playhead(1800)
    editor._sync_playhead()
    assert editor.preview.subtitle_label.text() == "second line"
    assert not hasattr(editor.preview, "subtitles_check")  # subtitles are always shown


def test_player_fallback_clock_when_sounddevice_missing(editor, qtbot, no_sd):
    messages: list[str] = []
    editor.player.status_message.connect(messages.append)

    editor.model.set_in(1000)
    editor.model.set_out(2000)
    editor._crop_updated()
    editor.model.set_playhead(2400)  # outside the crop: Play starts at In
    editor.toggle_play()
    assert editor.player.playing and not editor.player.using_audio
    assert editor.preview.play_button.text() == "Pause"
    assert editor.model.playhead_ms == 1000
    assert messages and "No audio output" in messages[0]
    qtbot.waitUntil(lambda: editor.model.playhead_ms > 1050, timeout=5000)
    assert 1000 < editor.model.playhead_ms < 2000

    editor.toggle_play()  # pause
    paused_at = editor.model.playhead_ms
    assert not editor.player.playing and editor.preview.play_button.text() == "Play"
    qtbot.wait(60)
    assert editor.model.playhead_ms == paused_at

    editor.model.set_playhead(1950)
    editor.toggle_play()
    assert editor.model.play_marker_ms == 1950  # the white arrow: where Play was pressed
    qtbot.waitUntil(lambda: not editor.player.playing, timeout=5000)  # stops at Out
    assert editor.model.playhead_ms == 2000
    editor.stop_playback()
    assert editor.model.playhead_ms == 1950  # Stop returns to where Play was pressed
    editor.model.set_play_marker(None)
    editor.stop_playback()
    assert editor.model.playhead_ms == 1000  # without a marker Stop goes to In


def test_player_uses_audio_stream_when_available(editor, qtbot, fake_sd):
    """A fake sounddevice: the clock advances by the samples the callback delivered."""
    editor.model.set_in(0)
    editor.model.set_out(1000)
    editor._crop_updated()
    editor.toggle_play()
    assert editor.player.using_audio
    assert FakeStream.instances[-1].samplerate == SAMPLE_RATE
    qtbot.waitUntil(lambda: not editor.player.playing, timeout=10_000)
    assert editor.model.playhead_ms == 1000
    assert not FakeStream.instances[-1].active


def test_pause_aborts_the_stream_instead_of_draining_it(editor, qtbot, fake_sd):
    editor.toggle_play()
    qtbot.waitUntil(lambda: editor.model.playhead_ms > 100, timeout=5000)
    editor.toggle_play()
    stream = FakeStream.instances[-1]
    assert stream.aborted and stream.closed and not stream.stopped
    assert not editor.player.playing
    paused_at = editor.model.playhead_ms
    editor.toggle_play()  # resumes exactly where it paused
    assert editor.model.playhead_ms == paused_at and len(FakeStream.instances) == 2
    assert editor.model.play_marker_ms == 0  # a resume keeps the marker of the first Play
    editor.stop_playback()
    assert editor.model.playhead_ms == 0


def test_stream_start_failure_closes_it_and_falls_back(editor, fake_sd):
    FakeStream.fail_start = True
    messages: list[str] = []
    editor.player.status_message.connect(messages.append)
    editor.toggle_play()
    assert editor.player.playing and not editor.player.using_audio
    assert FakeStream.instances[-1].closed  # the opened stream handle is released
    assert messages and "No audio output" in messages[0]
    editor.stop_playback()


def test_stream_stopped_by_portaudio_continues_on_the_clock(editor, qtbot, fake_sd):
    FakeStream.die_after = 3  # ~30 ms of audio, then the stream goes inactive silently
    messages: list[str] = []
    editor.player.status_message.connect(messages.append)
    editor.model.set_out(1000)
    editor._crop_updated()
    editor.toggle_play()
    assert editor.player.using_audio
    qtbot.waitUntil(lambda: not editor.player.using_audio, timeout=5000)
    assert editor.player.playing and editor.preview.play_button.text() == "Pause"
    assert messages == ["Audio output stopped: continuing silently with a system clock."]
    assert FakeStream.instances[-1].closed
    position = editor.model.playhead_ms
    qtbot.waitUntil(lambda: editor.model.playhead_ms > position + 50, timeout=5000)
    qtbot.waitUntil(lambda: not editor.player.playing, timeout=5000)
    assert editor.model.playhead_ms == 1000
    assert len(FakeStream.instances) == 1


def test_short_audio_plays_silently_past_its_end(qtbot, tmp_path: Path, fake_sd):
    make_recording(tmp_path, audio_ms=1000)  # the WAV ends 1.5 s before the recording
    window = EditorWindow(Config(recording_folder=tmp_path))
    qtbot.addWidget(window)
    window.show()
    _wait_loaded(qtbot, window, NEWEST)
    messages: list[str] = []
    window.player.status_message.connect(messages.append)
    # Starting after the audio's end: the system clock runs without any warning.
    window.model.set_playhead(2000)
    window.toggle_play()
    assert window.player.playing and not window.player.using_audio
    assert messages == [] and FakeStream.instances == []
    assert window.model.playhead_ms == 2000
    qtbot.waitUntil(lambda: not window.player.playing, timeout=5000)
    assert window.model.playhead_ms == DURATION_MS
    # Starting inside the audio with Out past its end: the audio drains, then the clock
    # continues silently until Out instead of ending playback at the audio's end.
    window.model.set_out(1500)
    window._crop_updated()
    window.model.set_playhead(800)
    window.toggle_play()
    assert window.player.using_audio
    qtbot.waitUntil(lambda: not window.player.using_audio, timeout=5000)
    assert window.player.playing and messages == []
    assert 950 <= window.model.playhead_ms <= 1100
    qtbot.waitUntil(lambda: not window.player.playing, timeout=5000)
    assert window.model.playhead_ms == 1500


def test_go_to_out_while_playing_pauses_at_out(editor, qtbot, no_sd):
    editor.model.set_in(500)
    editor.model.set_out(2000)
    editor._crop_updated()
    editor.toggle_play()
    qtbot.waitUntil(lambda: editor.model.playhead_ms > 550, timeout=5000)
    editor.go_to_out()
    assert not editor.player.playing and editor.preview.play_button.text() == "Play"
    assert editor.model.playhead_ms == 2000
    qtbot.wait(60)
    assert editor.model.playhead_ms == 2000  # no restart from In
    # Home while playing keeps playing from In.
    editor.toggle_play()
    assert editor.model.playhead_ms == 500
    qtbot.waitUntil(lambda: editor.model.playhead_ms > 550, timeout=5000)
    editor.go_to_in()
    assert editor.player.playing and editor.model.playhead_ms == 500
    editor.stop_playback()


def test_playhead_drag_while_playing_scrubs_and_resumes_once(editor, qtbot, no_sd, monkeypatch):
    plays: list[int] = []
    original_play = editor.player.play

    def counting_play(from_ms: int) -> int:
        plays.append(from_ms)
        return original_play(from_ms)

    monkeypatch.setattr(editor.player, "play", counting_play)
    editor.toggle_play()
    assert len(plays) == 1
    view = editor.timeline
    y = view.track_rects()[0][1].center().y()
    x0 = int(view._x(600))
    qtbot.mousePress(view, Qt.MouseButton.LeftButton, pos=QPoint(x0, y))
    assert not editor.player.playing  # paused once for the scrub
    assert editor.model.playhead_ms == view._time(x0)
    for dx in (5, 10, 15, 20):
        _mouse_move(view, QPoint(x0 + dx, y))
        assert editor.model.playhead_ms == view._time(x0 + dx)
        assert editor.preview.time_label.text().startswith(
            f"{format_crop_time(editor.model.playhead_ms)} /"
        )
    assert len(plays) == 1 and not editor.player.playing
    qtbot.mouseRelease(view, Qt.MouseButton.LeftButton, pos=QPoint(x0 + 20, y))
    assert len(plays) == 2 and plays[1] == view._time(x0 + 20)
    assert editor.player.playing and editor.preview.play_button.text() == "Pause"
    # A drag released at Out stays paused there instead of restarting from In.
    x_out = int(view._x(editor.model.out_ms)) - 1
    qtbot.mousePress(view, Qt.MouseButton.LeftButton, pos=QPoint(x0, y))
    _mouse_move(view, QPoint(x_out, y))
    qtbot.mouseRelease(view, Qt.MouseButton.LeftButton, pos=QPoint(x_out, y))
    assert len(plays) == 2 + (1 if editor.model.playhead_ms < editor.model.out_ms else 0)
    # Scrubbing while paused only moves the playhead.
    editor.stop_playback()
    qtbot.mousePress(view, Qt.MouseButton.LeftButton, pos=QPoint(x0, y))
    _mouse_move(view, QPoint(x0 + 10, y))
    qtbot.mouseRelease(view, Qt.MouseButton.LeftButton, pos=QPoint(x0 + 10, y))
    assert not editor.player.playing and editor.model.playhead_ms == view._time(x0 + 10)


def test_crop_handle_press_keeps_grab_offset_and_playhead_wins(qtbot, tmp_path: Path):
    make_recording(tmp_path, duration_ms=3_600_000)  # one hour: seconds per pixel at Fit
    window = EditorWindow(Config(recording_folder=tmp_path))
    qtbot.addWidget(window)
    window.show()
    _wait_loaded(qtbot, window, NEWEST)
    view, model = window.timeline, window.model
    assert model.zoom == pytest.approx(model.min_zoom())
    ms_per_px = 1000.0 / model.zoom
    assert ms_per_px > 1000
    model.set_in(60_000)
    window._crop_updated()
    y = view.audio_rect().center().y()
    x_in = view._x(60_000)
    press_x = int(x_in) + 5  # near the In handle, not on it
    qtbot.mousePress(view, Qt.MouseButton.LeftButton, pos=QPoint(press_x, y))
    assert view._drag == "in"
    assert model.in_ms == 60_000  # pressing does not move the crop
    _mouse_move(view, QPoint(press_x + 10, y))
    assert model.in_ms == pytest.approx(60_000 + 10 * ms_per_px, abs=1)
    assert window.in_edit.text() == format_crop_time(model.in_ms)
    qtbot.mouseRelease(view, Qt.MouseButton.LeftButton, pos=QPoint(press_x + 10, y))
    assert view._drag is None
    # The playhead sitting on the In handle can still be scrubbed.
    model.reset_crop()
    window._crop_updated()
    model.set_playhead(0)
    x0 = int(view._x(0))
    qtbot.mousePress(view, Qt.MouseButton.LeftButton, pos=QPoint(x0 + 1, y))
    assert view._drag == "playhead"
    _mouse_move(view, QPoint(x0 + 10, y))
    assert model.in_ms == 0 and model.playhead_ms == view._time(x0 + 10)
    qtbot.mouseRelease(view, Qt.MouseButton.LeftButton, pos=QPoint(x0 + 10, y))
    # Further away from the playhead the handle is grabbed again.
    model.set_playhead(0)
    qtbot.mousePress(view, Qt.MouseButton.LeftButton, pos=QPoint(x0 + 5, y))
    assert view._drag == "in" and model.in_ms == 0
    qtbot.mouseRelease(view, Qt.MouseButton.LeftButton, pos=QPoint(x0 + 5, y))


def test_playhead_and_out_handle_painted_on_the_last_column_at_fit(qtbot, tmp_path: Path):
    make_recording(tmp_path, duration_ms=60_000)
    window = EditorWindow(Config(recording_folder=tmp_path))
    qtbot.addWidget(window)
    window.show()
    _wait_loaded(qtbot, window, NEWEST)
    view, model = window.timeline, window.model
    assert model.zoom < MAX_ZOOM_PX_PER_S  # Fit is not capped: the end is at the edge
    assert view._x(model.duration_ms) == pytest.approx(view.width())
    last = view.width() - 1
    model.set_playhead(model.duration_ms)
    image = view.grab().toImage()
    assert image.pixelColor(last, RULER_H // 2).name() == COLOR_PLAYHEAD.name()
    model.set_playhead(0)
    image = view.grab().toImage()
    assert image.pixelColor(last, RULER_H // 2).name() != COLOR_PLAYHEAD.name()
    handle_y = view.audio_rect().center().y()
    assert image.pixelColor(last, handle_y).name() == COLOR_HANDLE.name()


def test_preview_cache_never_evicts_the_shown_or_wanted_frame(editor, qtbot):
    view = editor.preview.view
    qtbot.waitUntil(lambda: view.shown_path is not None, timeout=10_000)
    shown = view.shown_path
    view._wanted = "wanted-but-not-decoded-yet"
    for i in range(IMAGE_CACHE_SIZE * 3):  # many decodes that are no longer wanted
        view._on_decoded(f"stale-{i}", QImage(4, 4, QImage.Format.Format_RGB32))
    assert len(view._images) == IMAGE_CACHE_SIZE
    assert view.shown_path == shown and shown in view._images
    view._on_decoded("wanted-but-not-decoded-yet", QImage(4, 4, QImage.Format.Format_RGB32))
    assert view.shown_path == "wanted-but-not-decoded-yet"
    assert len(view._images) == IMAGE_CACHE_SIZE and view.shown_path in view._images


class _Blocker(QRunnable):
    def __init__(self, gate: threading.Event) -> None:
        super().__init__()
        self._gate = gate

    def run(self) -> None:
        self._gate.wait(10)


def test_thumbnail_jobs_skip_unwanted_keys_and_clear_drops_the_queue(
    qtbot, tmp_path: Path, monkeypatch
):
    paths = make_recording(tmp_path)
    frames = sorted(paths.frames_dir(paths.final_dir).glob("*.png"))
    runs: list[tuple[str, int]] = []
    original_run = thumbnails_mod._DecodeJob.run

    def counting_run(self) -> None:
        runs.append((self._path.name, self._height))
        original_run(self)

    monkeypatch.setattr(thumbnails_mod._DecodeJob, "run", counting_run)
    cache = ThumbnailCache(capacity=100, threads=1)
    gate = threading.Event()
    cache._pool.start(_Blocker(gate))  # holds the single thread: the jobs queue up
    cache.begin_paint()
    for path in frames:
        cache.get(path, 20)
    assert cache.pending == len(frames)
    cache.begin_paint()  # a new paint wants only the first frame
    assert cache.get(frames[0], 20) is None
    gate.set()
    qtbot.waitUntil(lambda: cache.pending == 0, timeout=10_000)
    assert len(cache) == 1 and cache.skipped == len(frames) - 1
    assert cache.get(frames[0], 20) is not None
    assert len(runs) == len(frames)

    # clear() empties the queue and the pending set; stale results do not come back.
    gate.clear()
    cache._pool.start(_Blocker(gate))
    cache.begin_paint()
    for height in (24, 28, 32):
        for path in frames:
            cache.get(path, height)
    assert cache.pending == 3 * len(frames)
    ready: list[int] = []
    cache.ready.connect(lambda: ready.append(1))
    runs_before = len(runs)
    cache.clear()
    assert cache.pending == 0 and len(cache) == 0
    gate.set()
    assert cache.shutdown(timeout_ms=5000)
    qtbot.wait(100)
    assert len(runs) == runs_before and ready == []
    assert cache.get(frames[1], 24) is None  # requests work again after clear()
    qtbot.waitUntil(lambda: cache.pending == 0, timeout=10_000)
    assert ready == [1]


def test_window_close_does_not_wait_for_a_thumbnail_backlog(editor, qtbot, monkeypatch):
    runs: list[int] = []
    original_run = thumbnails_mod._DecodeJob.run

    def counting_run(self) -> None:
        runs.append(1)
        original_run(self)

    monkeypatch.setattr(thumbnails_mod._DecodeJob, "run", counting_run)
    cache = editor.timeline.thumbnails
    qtbot.waitUntil(lambda: cache.pending == 0, timeout=10_000)
    frames = sorted(
        editor.recording.paths.frames_dir(editor.recording.paths.final_dir).glob("*.png")
    )
    gate = threading.Event()
    for _ in range(2):  # both pool threads
        cache._pool.start(_Blocker(gate))
    cache.begin_paint()
    for height in range(100, 130):  # heights the view never painted
        for path in frames:
            cache.get(path, height)
    assert cache.pending == 30 * len(frames)
    runs_before = len(runs)
    # The blockers are released only once close() is already waiting for them: the
    # queued jobs must have been dropped by then, so none of them runs.
    threading.Timer(0.1, gate.set).start()
    t0 = time.perf_counter()
    editor.close()
    assert time.perf_counter() - t0 < 5
    assert cache.pending == 0 and len(runs) == runs_before
    qtbot.wait(50)
    assert len(runs) == runs_before


def test_empty_recording_folder(qtbot, tmp_path: Path):
    window = EditorWindow(Config(recording_folder=tmp_path / "empty"))
    qtbot.addWidget(window)
    window.show()
    assert window.recording is None
    assert window.status_label.text() == "No recording selected"
    window.toggle_play()
    assert not window.player.playing
