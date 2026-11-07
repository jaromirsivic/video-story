"""EditorWindow: recordings list, preview, timeline, crop controls, Render."""

from __future__ import annotations

import logging
import re
from pathlib import Path

from PySide6.QtCore import (
    QItemSelection,
    QObject,
    QRegularExpression,
    QRunnable,
    QSettings,
    Qt,
    QThreadPool,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QAction,
    QCloseEvent,
    QFocusEvent,
    QFont,
    QKeyEvent,
    QKeySequence,
    QRegularExpressionValidator,
)
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QMainWindow,
    QPushButton,
    QSlider,
    QSplitter,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from video_story.core.config import EXPORT_FPS, Config
from video_story.core.session import SessionPaths
from video_story.core.timeline import format_timestamp, parse_timestamp
from video_story.export import ExportRequest, ExportResult

from .player import Player
from .preview import PreviewPanel
from .recording import LoadedRecording
from .recordings_model import ItemRole, RecordingItem, RecordingsModel
from .render_dialog import ExportFn, ask_output_path, default_output_path, run_render
from .timeline.model import TimelineModel
from .timeline.view import TimelineView

log = logging.getLogger(__name__)

WINDOW_TITLE = "Video Story Editor"
TICK_MS = 33
STEP_MS = 1000.0 / EXPORT_FPS
ZOOM_SLIDER_MAX = 1000
TIME_TEXT = re.compile(r"^\d{2,}:\d{2}:\d{2}(\.\d{1,3})?$")  # HH:MM:SS[.mmm]


class _LoadSignals(QObject):
    done = Signal(int, object, str)  # generation, LoadedRecording | None, error


class _LoadJob(QRunnable):
    def __init__(self, generation: int, paths: SessionPaths, signals: _LoadSignals) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self._generation = generation
        self._paths = paths
        self._signals = signals

    def run(self) -> None:
        try:
            recording = LoadedRecording.load(self._paths)
        except Exception as exc:  # noqa: BLE001 - reported in the window
            log.exception("loading %s failed", self._paths.session_id)
            self._signals.done.emit(self._generation, None, str(exc))
            return
        self._signals.done.emit(self._generation, recording, "")


class TimeEdit(QLineEdit):
    """``HH:MM:SS.mmm`` editor; ``committed(ms)`` fires on Enter / focus out.

    The text is parsed on commit (``HH:MM:SS`` without milliseconds is accepted);
    anything unparsable reverts to the last value. ``editingFinished`` is not used:
    QLineEdit only emits it for text its validator rates Acceptable, so a partial
    entry would otherwise survive Enter and focus-out untouched.
    """

    committed = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setValidator(QRegularExpressionValidator(QRegularExpression(r"[0-9:.]*"), self))
        self.setFixedWidth(self.fontMetrics().horizontalAdvance("00:00:00.000") + 24)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._value = 0
        self.set_value(0)

    def set_value(self, ms: int) -> None:
        self._value = max(0, ms)
        self.setText(format_timestamp(self._value))

    @property
    def value(self) -> int:
        return self._value

    def commit(self) -> None:
        """Parse the text: emit ``committed`` or revert to the last valid value."""
        text = self.text().strip()
        if not TIME_TEXT.match(text):  # partial entries such as "00:00:0" revert
            self.set_value(self._value)
            return
        if "." not in text:
            text += ".000"
        try:
            value = parse_timestamp(text)
        except ValueError:
            self.set_value(self._value)
            return
        self.committed.emit(value)
        if self.text() != format_timestamp(self._value):
            self.set_value(self._value)  # the owner did not accept it: show its value

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.commit()
            self.selectAll()
            event.accept()
            return
        super().keyPressEvent(event)

    def focusOutEvent(self, event: QFocusEvent) -> None:  # noqa: N802
        self.commit()
        super().focusOutEvent(event)


class EditorWindow(QMainWindow):
    """The editor. ``export_fn`` replaces the FFmpeg export (tests)."""

    recording_loaded = Signal(object)  # LoadedRecording | None

    def __init__(
        self,
        config: Config,
        session_id: str | None = None,
        export_fn: ExportFn | None = None,
        settings: QSettings | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.config = config
        self._export_fn = export_fn
        self._settings = settings
        self.recording: LoadedRecording | None = None
        self.last_export_result: ExportResult | None = None
        self.model = TimelineModel()
        self.player = Player(self)
        self.player.status_message.connect(self._show_status_message)
        self._load_generation = 0
        self._load_pool = QThreadPool(self)
        self._load_pool.setMaxThreadCount(1)
        self._load_signals = _LoadSignals(self)
        self._load_signals.done.connect(self._on_loaded)
        self._syncing_zoom = False
        self._resume_after_scrub = False  # playback was paused for a playhead drag

        self.setWindowTitle(WINDOW_TITLE)
        self._build_ui()
        self._build_actions()
        self.resize(1280, 800)

        self._timer = QTimer(self)
        self._timer.setInterval(TICK_MS)
        self._timer.timeout.connect(self._on_tick)

        self.recordings = RecordingsModel(config.recording_folder, self)
        self.list_view.setModel(self.recordings)
        self.list_view.selectionModel().selectionChanged.connect(self._on_selection_changed)
        self._select_row(self.recordings.default_row(session_id))
        self._update_status()

    # UI ---------------------------------------------------------------------

    def _build_ui(self) -> None:
        self.main_splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.setCentralWidget(self.main_splitter)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(4, 4, 4, 4)
        title = QLabel("Recordings")
        title_font = QFont(title.font())
        title_font.setBold(True)
        title.setFont(title_font)
        left_layout.addWidget(title)
        self.list_view = QListView()
        self.list_view.setUniformItemSizes(False)
        self.list_view.setSpacing(2)
        self.list_view.setWordWrap(True)
        self.list_view.setEditTriggers(QListView.EditTrigger.NoEditTriggers)
        left_layout.addWidget(self.list_view, 1)
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.setToolTip("Rescan the recording folder (F5)")
        self.refresh_button.clicked.connect(self.refresh_recordings)
        left_layout.addWidget(self.refresh_button)
        self.main_splitter.addWidget(left)

        self.right_splitter = QSplitter(Qt.Orientation.Vertical)
        self.preview = PreviewPanel()
        self.preview.view.set_draw_cursor(self.config.editor_draw_cursor)
        self.preview.play_pause.connect(self.toggle_play)
        self.preview.stop.connect(self.stop_playback)
        self.preview.go_in.connect(self.go_to_in)
        self.preview.go_out.connect(self.go_to_out)
        self.preview.step.connect(self.step_frames)
        self.right_splitter.addWidget(self.preview)

        bottom = QWidget()
        bottom_layout = QVBoxLayout(bottom)
        bottom_layout.setContentsMargins(4, 4, 4, 4)
        bottom_layout.setSpacing(4)

        tools = QHBoxLayout()
        tools.setSpacing(6)
        tools.addWidget(QLabel("In"))
        self.in_edit = TimeEdit()
        self.in_edit.committed.connect(self._on_in_edited)
        tools.addWidget(self.in_edit)
        tools.addWidget(QLabel("Out"))
        self.out_edit = TimeEdit()
        self.out_edit.committed.connect(self._on_out_edited)
        tools.addWidget(self.out_edit)
        self.reset_crop_button = QPushButton("Reset Crop")
        self.reset_crop_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.reset_crop_button.clicked.connect(self.reset_crop)
        tools.addWidget(self.reset_crop_button)
        self.crop_label = QLabel("")
        tools.addWidget(self.crop_label)
        tools.addStretch(1)
        tools.addWidget(QLabel("Zoom"))
        self.zoom_out_button = QPushButton("-")
        self.zoom_out_button.setFixedWidth(28)
        self.zoom_out_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.zoom_out_button.clicked.connect(self.zoom_out)
        tools.addWidget(self.zoom_out_button)
        self.zoom_slider = QSlider(Qt.Orientation.Horizontal)
        self.zoom_slider.setRange(0, ZOOM_SLIDER_MAX)
        self.zoom_slider.setFixedWidth(160)
        self.zoom_slider.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.zoom_slider.valueChanged.connect(self._on_zoom_slider)
        tools.addWidget(self.zoom_slider)
        self.zoom_in_button = QPushButton("+")
        self.zoom_in_button.setFixedWidth(28)
        self.zoom_in_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.zoom_in_button.clicked.connect(self.zoom_in)
        tools.addWidget(self.zoom_in_button)
        self.fit_button = QPushButton("Fit")
        self.fit_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.fit_button.clicked.connect(self.zoom_fit)
        tools.addWidget(self.fit_button)
        bottom_layout.addLayout(tools)

        self.timeline = TimelineView(self.model)
        self.timeline.playhead_moved.connect(self._on_playhead_moved)
        self.timeline.playhead_drag_finished.connect(self._on_playhead_drag_finished)
        self.timeline.crop_changed.connect(self._on_crop_changed)
        self.timeline.screen_selected.connect(self._on_screen_selected)
        self.timeline.view_changed.connect(self._sync_zoom_slider)
        bottom_layout.addWidget(self.timeline, 1)

        render_row = QHBoxLayout()
        render_row.addStretch(1)
        self.render_button = QPushButton("Render")
        self.render_button.setToolTip("Export the selected screen and crop to MP4 + SRT (Ctrl+R)")
        render_font = QFont(self.render_button.font())
        render_font.setBold(True)
        render_font.setPointSizeF(render_font.pointSizeF() + 2)
        self.render_button.setFont(render_font)
        self.render_button.setMinimumSize(180, 44)
        self.render_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.render_button.clicked.connect(self.render)
        render_row.addWidget(self.render_button)
        bottom_layout.addLayout(render_row)
        self.right_splitter.addWidget(bottom)
        self.right_splitter.setStretchFactor(0, 3)
        self.right_splitter.setStretchFactor(1, 2)
        self.right_splitter.setSizes([480, 320])
        self.main_splitter.addWidget(self.right_splitter)
        self.main_splitter.setStretchFactor(0, 0)
        self.main_splitter.setStretchFactor(1, 1)
        self.main_splitter.setSizes([260, 1020])

        self.status_label = QLabel("")
        status = QStatusBar()
        status.addPermanentWidget(self.status_label, 1)
        self.setStatusBar(status)

    def _build_actions(self) -> None:
        def action(text: str, keys: list[str] | str, slot) -> QAction:
            act = QAction(text, self)
            shortcuts = [keys] if isinstance(keys, str) else keys
            act.setShortcuts([QKeySequence(k) for k in shortcuts])
            act.setShortcutContext(Qt.ShortcutContext.WindowShortcut)
            act.triggered.connect(slot)
            self.addAction(act)
            return act

        self.play_action = action("Play/Pause", "Space", self.toggle_play)
        self.stop_action = action("Stop", "Escape", self.stop_playback)
        self.go_in_action = action("Go to In", "Home", self.go_to_in)
        self.go_out_action = action("Go to Out", "End", self.go_to_out)
        self.step_back_action = action("Step back", "Left", lambda: self.step_frames(-1))
        self.step_forward_action = action("Step forward", "Right", lambda: self.step_frames(1))
        self.second_back_action = action("Back 1 s", "Shift+Left", lambda: self.step_ms(-1000))
        self.second_forward_action = action(
            "Forward 1 s", "Shift+Right", lambda: self.step_ms(1000)
        )
        self.set_in_action = action("Set In", "I", self.set_in_at_playhead)
        self.set_out_action = action("Set Out", "O", self.set_out_at_playhead)
        self.zoom_in_action = action("Zoom in", ["Ctrl+=", "Ctrl++"], self.zoom_in)
        self.zoom_out_action = action("Zoom out", "Ctrl+-", self.zoom_out)
        self.fit_action = action("Fit", "Ctrl+0", self.zoom_fit)
        self.render_action = action("Render", "Ctrl+R", self.render)
        self.refresh_action = action("Refresh", "F5", self.refresh_recordings)

    # Recordings list --------------------------------------------------------

    def refresh_recordings(self) -> None:
        current = self.recording.session_id if self.recording else None
        self.recordings.refresh()
        row = self.recordings.default_row(current)
        if row >= 0 and self.recordings.item(row).session_id == current:
            self.list_view.setCurrentIndex(self.recordings.index(row))
            return
        self._select_row(row)

    def _select_row(self, row: int) -> None:
        if row < 0:
            self._set_recording(None)
            self.preview.view.set_message("No processed recordings in the recording folder")
            return
        index = self.recordings.index(row)
        if self.list_view.currentIndex() == index:
            self._load_recording(self.recordings.item(row))
        else:
            self.list_view.setCurrentIndex(index)

    def _on_selection_changed(self, selected: QItemSelection, _deselected: QItemSelection) -> None:
        indexes = selected.indexes()
        if not indexes:
            return
        item = indexes[0].data(ItemRole)
        if isinstance(item, RecordingItem):
            self._load_recording(item)

    def _load_recording(self, item: RecordingItem) -> None:
        if self.recording is not None and self.recording.paths == item.paths:
            return
        self._load_generation += 1
        self._set_recording(None)
        self.preview.view.set_message(f"Loading {item.session_id}...")
        self.statusBar().showMessage(f"Loading {item.session_id}...")
        self._load_pool.start(_LoadJob(self._load_generation, item.paths, self._load_signals))

    def _on_loaded(self, generation: int, recording: object, error: str) -> None:
        if generation != self._load_generation:
            return
        self.statusBar().clearMessage()
        if not isinstance(recording, LoadedRecording):
            self.preview.view.set_message(f"Cannot load recording: {error}")
            self._show_status_message(f"Cannot load recording: {error}")
            self.recording_loaded.emit(None)
            return
        self._set_recording(recording)
        self.recording_loaded.emit(recording)

    def _set_recording(self, recording: LoadedRecording | None) -> None:
        self._timer.stop()
        self.player.stop()
        self.recording = recording
        self.preview.view.clear()
        self.timeline.set_recording(recording)
        self.player.set_audio(recording.audio if recording else None)
        self.player.set_range(self.model.in_ms, self.model.out_ms)
        self._crop_updated()
        self._sync_zoom_slider()
        if recording is None:
            self.preview.view.set_message("No recording")
            self.preview.set_subtitle("")
            self.preview.set_time(0, 0)
        else:
            if not recording.frame_times(self.model.selected_screen):
                self.preview.view.set_message("No frames")
            self.timeline.setFocus()
        self._sync_playhead()
        self._update_status()

    @property
    def loading(self) -> bool:
        return self._load_pool.activeThreadCount() > 0

    # Playhead ---------------------------------------------------------------

    def _sync_playhead(self) -> None:
        position = self.model.playhead_ms
        self.preview.set_time(position, self.model.duration_ms)
        recording = self.recording
        if recording is None:
            return
        screen_id = self.model.selected_screen
        ref = recording.frame_at(screen_id, position)
        cursor = recording.cursor_at(screen_id, position) if ref is not None else None
        self.preview.view.set_frame(ref, cursor, screen_id)
        if self.player.playing and ref is not None:
            self.preview.view.prefetch(recording.next_frame_after(screen_id, ref.time_ms))
        self.preview.set_subtitle(recording.subtitle_at(position))
        self.timeline.update()

    def _seek(self, time_ms: float) -> None:
        was_playing = self.player.playing
        if was_playing:
            self._pause_playback()
        self.model.set_playhead(time_ms)
        if was_playing and self.model.playhead_ms < self.model.out_ms:
            self._start_playback()
        else:
            # Seeking to Out (or beyond) while playing lands there, paused: restarting
            # from In would be a surprise.
            self._sync_playhead()
        self.timeline.ensure_playhead_visible()

    def _on_playhead_moved(self, _time_ms: int) -> None:
        # A playhead drag scrubs: playback pauses once on the first move and resumes
        # once when the button is released (see _on_playhead_drag_finished), instead of
        # restarting the audio stream on every mouse move.
        if self.player.playing:
            self._pause_playback(keep_playhead=True)  # the model already has the new time
            self._resume_after_scrub = True
        self._sync_playhead()

    def _on_playhead_drag_finished(self) -> None:
        resume, self._resume_after_scrub = self._resume_after_scrub, False
        if resume and self.model.playhead_ms < self.model.out_ms:
            self._start_playback()

    def _on_tick(self) -> None:
        if not self.player.playing:
            self._timer.stop()
            return
        if self.player.reached_end:
            self.player.pause()
            self.model.set_playhead(self.model.out_ms)
            self._timer.stop()
            self.preview.set_playing(False)
            self._sync_playhead()
            return
        if self.model.set_playhead(self.player.position_ms()):
            self._sync_playhead()
            self.timeline.ensure_playhead_visible()

    # Transport --------------------------------------------------------------

    def _start_playback(self) -> None:
        if self.recording is None:
            return
        start = self.player.play(self.model.playhead_ms)
        self.model.set_playhead(start)
        self.preview.set_playing(True)
        self._sync_playhead()
        self._timer.start()

    def _pause_playback(self, keep_playhead: bool = False) -> int:
        position = self.player.pause()
        self._timer.stop()
        if not keep_playhead:
            self.model.set_playhead(position)
        self.preview.set_playing(False)
        return position

    def toggle_play(self) -> None:
        self._resume_after_scrub = False
        if self.player.playing:
            self._pause_playback()
            self._sync_playhead()
        else:
            self._start_playback()

    def stop_playback(self) -> None:
        self._resume_after_scrub = False
        self.player.stop()
        self._timer.stop()
        self.preview.set_playing(False)
        self.model.set_playhead(self.model.in_ms)
        self._sync_playhead()
        self.timeline.ensure_playhead_visible()

    def go_to_in(self) -> None:
        self._seek(self.model.in_ms)

    def go_to_out(self) -> None:
        self._seek(self.model.out_ms)

    def step_frames(self, frames: int) -> None:
        if self.player.playing:
            self.toggle_play()
        self._seek(round(self.model.playhead_ms + frames * STEP_MS))

    def step_ms(self, delta_ms: int) -> None:
        if self.player.playing:
            self.toggle_play()
        self._seek(self.model.playhead_ms + delta_ms)

    # Crop -------------------------------------------------------------------

    def set_in_at_playhead(self) -> None:
        if self.model.set_in(self.model.playhead_ms):
            self._crop_updated()

    def set_out_at_playhead(self) -> None:
        if self.model.set_out(self.model.playhead_ms):
            self._crop_updated()

    def reset_crop(self) -> None:
        if self.model.reset_crop():
            self._crop_updated()

    def _on_in_edited(self, value: int) -> None:
        self.model.set_in(value)
        self._crop_updated()

    def _on_out_edited(self, value: int) -> None:
        self.model.set_out(value)
        self._crop_updated()

    def _on_crop_changed(self) -> None:
        self._crop_updated()

    def _crop_updated(self) -> None:
        self.in_edit.set_value(self.model.in_ms)
        self.out_edit.set_value(self.model.out_ms)
        self.crop_label.setText(f"length {format_timestamp(self.model.crop_length_ms)}")
        self.player.set_range(self.model.in_ms, self.model.out_ms)
        self.timeline.update()
        self._update_status()

    # Screen selection -------------------------------------------------------

    def select_screen(self, screen_id: str) -> None:
        if self.model.select_screen(screen_id):
            self._on_screen_selected(screen_id)

    def _on_screen_selected(self, _screen_id: str) -> None:
        self.preview.view.clear()
        self._sync_playhead()
        self._update_status()

    # Zoom -------------------------------------------------------------------

    def zoom_in(self) -> None:
        self.timeline.zoom_in()

    def zoom_out(self) -> None:
        self.timeline.zoom_out()

    def zoom_fit(self) -> None:
        self.timeline.fit()

    def _on_zoom_slider(self, value: int) -> None:
        if self._syncing_zoom:
            return
        self.timeline.set_zoom_fraction(value / ZOOM_SLIDER_MAX)

    def _sync_zoom_slider(self) -> None:
        self._syncing_zoom = True
        try:
            self.zoom_slider.setValue(int(round(self.model.zoom_fraction() * ZOOM_SLIDER_MAX)))
        finally:
            self._syncing_zoom = False

    # Render -----------------------------------------------------------------

    def build_export_request(self, output: Path) -> ExportRequest | None:
        if self.recording is None:
            return None
        return ExportRequest(
            paths=self.recording.paths,
            output=output,
            screen_id=self.model.selected_screen,
            start_ms=self.model.in_ms,
            end_ms=self.model.out_ms,
            draw_cursor=self.config.editor_draw_cursor,
            write_srt=True,
        )

    def render(self) -> ExportResult | None:
        if self.recording is None:
            self._show_status_message("Select a recording first.")
            return None
        if self.player.playing:
            self.toggle_play()
        suggested = default_output_path(
            self.config, self.recording.session_id, self.model.selected_screen
        )
        output = ask_output_path(self, suggested)
        if output is None:
            return None
        request = self.build_export_request(output)
        assert request is not None
        self.render_button.setEnabled(False)
        try:
            result = run_render(self, self.config, request, self._export_fn)
        finally:
            self.render_button.setEnabled(True)
        self.last_export_result = result
        if result is not None:
            self._show_status_message(f"Rendered {result.output}")
        return result

    # Status -----------------------------------------------------------------

    def _show_status_message(self, text: str) -> None:
        self.statusBar().showMessage(text, 8000)

    def _update_status(self) -> None:
        if self.recording is None:
            self.status_label.setText("No recording selected")
            return
        width, height = self.recording.screen_size(self.model.selected_screen)
        size = f"{width}x{height}" if width else "unknown size"
        frames = round(self.model.crop_length_ms / 1000 * EXPORT_FPS)
        self.status_label.setText(
            f"{self.recording.session_id}   |   {self.model.selected_screen} {size}   |   "
            f"crop {format_timestamp(self.model.crop_length_ms)} "
            f"({frames} frames at {EXPORT_FPS} fps)"
        )

    # Settings and shutdown --------------------------------------------------

    def restore_settings(self) -> None:
        settings = self._settings
        if settings is None:
            return
        geometry = settings.value("window/geometry")
        if geometry is not None:
            self.restoreGeometry(geometry)
        main_state = settings.value("splitter/main")
        if main_state is not None:
            self.main_splitter.restoreState(main_state)
        right_state = settings.value("splitter/right")
        if right_state is not None:
            self.right_splitter.restoreState(right_state)

    def save_settings(self) -> None:
        settings = self._settings
        if settings is None:
            return
        settings.setValue("window/geometry", self.saveGeometry())
        settings.setValue("splitter/main", self.main_splitter.saveState())
        settings.setValue("splitter/right", self.right_splitter.saveState())
        settings.sync()

    def shutdown(self) -> None:
        self._timer.stop()
        self.player.stop()
        self._load_generation += 1  # ignore late results
        self._load_pool.waitForDone(10_000)
        self.timeline.shutdown()
        self.preview.view.wait()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        self.save_settings()
        self.shutdown()
        super().closeEvent(event)


__all__ = ["EditorWindow", "TimeEdit", "WINDOW_TITLE"]
