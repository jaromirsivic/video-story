"""EditorWindow: recordings list, preview, timeline, crop controls, Render, Process.

The window talks to the agent through the host-side helpers of
:mod:`video_story.host.editor_session`:

* ``editor_session`` (``edit --detach``): the event file the agent polls with
  ``wait --editor``. **Process** writes ``process`` with the id of the recording shown
  and its crop; closing the window without Process writes ``closed``.
* ``recording_session`` (Video Editor from the Stop dialog): a recording session in
  status ``editing``. The window adopts it at start (its pid), **Process** finishes it
  (``processed`` with the crop, ``processed_via`` ``editor``) and closing without
  Process cancels it.
* Attach requests: a later ``edit --detach`` while this window runs writes a request
  file; a 1 s timer picks it up, loads the requested recording, adopts the new editor
  session / recording session and raises the window.

The crop ``[In, Out]`` is stored next to the recording in ``crop.json`` on every change
(handle drag - debounced to the end of the drag -, I/O keys, In/Out editors, Reset
Crop) and restored when the recording is loaded again.
"""

from __future__ import annotations

import logging
import os
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
    QApplication,
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
from video_story.core.crop import Crop, read_crop, write_crop
from video_story.core.session import SessionPaths
from video_story.core.timeline import format_timestamp, parse_timestamp
from video_story.export import ExportRequest, ExportResult
from video_story.host.editor_session import (
    AttachRequest,
    adopt_recording_session,
    cancel_recording_session,
    finish_recording_session,
    take_attach_requests,
    write_editor_event,
)

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
CROP_SAVE_DEBOUNCE_MS = 300  # a handle drag writes crop.json once it comes to rest
ATTACH_POLL_MS = 1000
PROCESS_FEEDBACK_MS = 2000
PROCESS_BUTTON_STYLE = """
QPushButton {
    background-color: #1e9e3e; color: white; border: 1px solid #167a30; border-radius: 4px;
}
QPushButton:hover { background-color: #23b347; }
QPushButton:pressed { background-color: #167a30; }
QPushButton:disabled { background-color: #8fcf9f; color: #f4fff6; border-color: #7fbf8f; }
"""


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
            if event.modifiers() & ~Qt.KeyboardModifier.KeypadModifier:
                event.ignore()  # Ctrl+Enter: committed, now let the window shortcut fire
                return
            self.selectAll()
            event.accept()
            return
        super().keyPressEvent(event)

    def focusOutEvent(self, event: QFocusEvent) -> None:  # noqa: N802
        self.commit()
        super().focusOutEvent(event)


class EditorWindow(QMainWindow):
    """The editor. ``export_fn`` replaces the FFmpeg export (tests).

    ``editor_session`` is the event file an agent waits on (``wait --editor``);
    ``recording_session`` is the id of a recording session in status ``editing`` that
    this window finishes (Process) or cancels (close). See the module docstring.
    """

    recording_loaded = Signal(object)  # LoadedRecording | None
    processed = Signal(str, object)  # session id, Crop - after Process was sent

    def __init__(
        self,
        config: Config,
        session_id: str | None = None,
        export_fn: ExportFn | None = None,
        settings: QSettings | None = None,
        parent: QWidget | None = None,
        editor_session: Path | None = None,
        recording_session: str | None = None,
    ) -> None:
        super().__init__(parent)
        self.config = config
        self._export_fn = export_fn
        self._settings = settings
        self.recording: LoadedRecording | None = None
        self.last_export_result: ExportResult | None = None
        self.editor_session: Path | None = editor_session
        self.recording_session: str | None = recording_session
        self.editor_event_sent = False  # a "process" event went to editor_session
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
        self._crop_dirty_for: LoadedRecording | None = None  # pending debounced write

        self.setWindowTitle(WINDOW_TITLE)
        self._build_ui()
        self._build_actions()
        self.resize(1280, 800)

        self._timer = QTimer(self)
        self._timer.setInterval(TICK_MS)
        self._timer.timeout.connect(self._on_tick)
        self._crop_save_timer = QTimer(self)
        self._crop_save_timer.setSingleShot(True)
        self._crop_save_timer.setInterval(CROP_SAVE_DEBOUNCE_MS)
        self._crop_save_timer.timeout.connect(self._flush_crop)
        self._process_feedback_timer = QTimer(self)
        self._process_feedback_timer.setSingleShot(True)
        self._process_feedback_timer.setInterval(PROCESS_FEEDBACK_MS)
        self._process_feedback_timer.timeout.connect(self._restore_process_button)
        self._attach_timer = QTimer(self)
        self._attach_timer.setInterval(ATTACH_POLL_MS)
        self._attach_timer.timeout.connect(self.check_attach_requests)

        if self.recording_session is not None:
            self._adopt_recording_session(self.recording_session)

        self.recordings = RecordingsModel(config.recording_folder, self)
        self.list_view.setModel(self.recordings)
        self.list_view.selectionModel().selectionChanged.connect(self._on_selection_changed)
        self._select_row(self.recordings.default_row(session_id))
        self._update_status()
        self._decline_attach_requests()  # left behind by an editor that was closing
        self._attach_timer.start()

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
        render_row.setSpacing(12)
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
        self.process_button = QPushButton("Process")
        self.process_button.setToolTip(
            "Send the cropped range to the agent for processing (Ctrl+Enter)"
        )
        self.process_button.setFont(render_font)
        self.process_button.setMinimumSize(180, 44)
        self.process_button.setStyleSheet(PROCESS_BUTTON_STYLE)
        self.process_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.process_button.setEnabled(False)
        self.process_button.clicked.connect(self.process)
        render_row.addWidget(self.process_button)
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
        self.process_action = action("Process", ["Ctrl+Return", "Ctrl+Enter"], self.process)
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
        self._flush_crop()  # a drag of the previous recording still waiting to be written
        self.recording = recording
        self.preview.view.clear()
        self.timeline.set_recording(recording)
        if recording is not None:
            self._restore_crop(recording)
        self.player.set_audio(recording.audio if recording else None)
        self.player.set_range(self.model.in_ms, self.model.out_ms)
        self._crop_updated()
        self._sync_zoom_slider()
        if not self._process_feedback_timer.isActive():
            self.process_button.setEnabled(recording is not None)
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

    @property
    def crop(self) -> Crop:
        """The current ``[In, Out]`` of the timeline."""
        return Crop(self.model.in_ms, self.model.out_ms)

    def set_in_at_playhead(self) -> None:
        if self.model.set_in(self.model.playhead_ms):
            self._crop_edited()

    def set_out_at_playhead(self) -> None:
        if self.model.set_out(self.model.playhead_ms):
            self._crop_edited()

    def reset_crop(self) -> None:
        self.model.reset_crop()
        self._crop_edited()  # always stores the whole range, even when nothing moved

    def _on_in_edited(self, value: int) -> None:
        self.model.set_in(value)
        self._crop_edited()

    def _on_out_edited(self, value: int) -> None:
        self.model.set_out(value)
        self._crop_edited()

    def _on_crop_changed(self) -> None:
        # A handle drag: the view emits on every mouse move, so the file is written
        # once the drag has rested for CROP_SAVE_DEBOUNCE_MS (or at the next flush).
        self._crop_updated()
        if self.recording is not None:
            self._crop_dirty_for = self.recording
            self._crop_save_timer.start()

    def _crop_edited(self) -> None:
        """A discrete crop change (keys, editors, Reset Crop): refresh and store it."""
        self._crop_updated()
        self._save_crop()

    def _crop_updated(self) -> None:
        self.in_edit.set_value(self.model.in_ms)
        self.out_edit.set_value(self.model.out_ms)
        self.crop_label.setText(f"length {format_timestamp(self.model.crop_length_ms)}")
        self.player.set_range(self.model.in_ms, self.model.out_ms)
        self.timeline.update()
        self._update_status()

    def _restore_crop(self, recording: LoadedRecording) -> None:
        """Apply ``crop.json`` of ``recording`` to the timeline (nothing is written)."""
        path = recording.paths.crop_path(recording.paths.final_dir)
        crop = read_crop(path, recording.duration_ms)
        self.model.reset_crop()
        self.model.set_in(crop.start_ms)
        self.model.set_out(crop.end_ms)
        self.model.set_in(crop.start_ms)  # set_in before set_out may have been clamped
        self._crop_dirty_for = None
        self._crop_save_timer.stop()

    def _save_crop(self) -> None:
        """Write the current crop to ``crop.json`` of the shown recording."""
        self._crop_save_timer.stop()
        self._crop_dirty_for = None
        recording = self.recording
        if recording is None:
            return
        path = recording.paths.crop_path(recording.paths.final_dir)
        try:
            write_crop(path, self.crop)
        except OSError as exc:
            log.warning("cannot write %s: %s", path, exc)
            self._show_status_message(f"Cannot save crop: {exc}")

    def _flush_crop(self) -> None:
        """Write a crop change that is still waiting on the debounce timer, if any."""
        self._crop_save_timer.stop()
        dirty, self._crop_dirty_for = self._crop_dirty_for, None
        if dirty is not None and dirty is self.recording:
            self._save_crop()

    # Screen selection -------------------------------------------------------

    def select_screen(self, screen_id: str) -> None:
        if self.model.select_screen(screen_id):
            self._on_screen_selected(screen_id)

    def _on_screen_selected(self, _screen_id: str) -> None:
        self.preview.view.drop_frame()  # keeps the zoom for a screen of the same size
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

    def _commit_time_edits(self) -> None:
        """Apply a value typed into In/Out that was not committed yet (Ctrl+R, Ctrl+Enter)."""
        for edit in (self.in_edit, self.out_edit):
            if edit.hasFocus():
                edit.commit()

    def render(self) -> ExportResult | None:
        if self.recording is None:
            self._show_status_message("Select a recording first.")
            return None
        self._commit_time_edits()
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

    # Process ----------------------------------------------------------------

    def process(self) -> Crop | None:
        """Send the shown recording and its crop to the agent; the window stays open.

        Stores ``crop.json``, writes the ``process`` event into ``editor_session`` and
        finishes ``recording_session`` (``processed`` with the crop). The recording
        session is finished even when the user has meanwhile selected a different
        recording: the agent waiting on it must be released by the green button, and
        the editor session event carries the id actually shown. The crop applies to
        all screens; the selected track is irrelevant.
        """
        if self.recording is None:
            self._show_status_message("Select a recording first.")
            return None
        if self._process_feedback_timer.isActive():
            return None  # "Sent to agent" is still showing: no second event
        self._commit_time_edits()
        if self.player.playing:
            self.toggle_play()
        crop = self.crop
        session_id = self.recording.session_id
        self._save_crop()
        problems: list[str] = []
        if self.editor_session is not None:
            try:
                write_editor_event(self.editor_session, "process", session_id=session_id, crop=crop)
                self.editor_event_sent = True
            except OSError as exc:
                log.warning("cannot write editor event %s: %s", self.editor_session, exc)
                problems.append(f"editor session: {exc}")
        if self.recording_session is not None:
            recording_session = self.recording_session
            if recording_session != session_id:
                log.warning(
                    "Process on %s finishes recording session %s", session_id, recording_session
                )
            paths = SessionPaths(self.config.recording_folder, recording_session)
            try:
                finish_recording_session(paths, crop, shown_session_id=session_id)
            except (OSError, RuntimeError) as exc:
                log.warning("cannot finish recording session %s: %s", recording_session, exc)
                problems.append(f"recording session: {exc}")
            else:
                self.recording_session = None  # final now; closing must not cancel it
        detail = f"{session_id} {format_timestamp(crop.start_ms)}-{format_timestamp(crop.end_ms)}"
        self._update_status()
        if problems:
            # The button stays "Process": the user can retry; a session that could not be
            # finished is kept so that closing the window still cancels it.
            self._show_status_message(f"Process failed for {detail}: " + "; ".join(problems))
            return None
        self._show_process_feedback()
        self._show_status_message(f"Sent to agent: {detail}")
        self.processed.emit(session_id, crop)
        return crop

    def _show_process_feedback(self) -> None:
        self.process_button.setText("Sent to agent")
        self.process_button.setEnabled(False)
        self.process_action.setEnabled(False)  # Ctrl+Enter must not bypass the button
        self._process_feedback_timer.start()

    def _restore_process_button(self) -> None:
        self.process_button.setText("Process")
        self.process_button.setEnabled(self.recording is not None)
        self.process_action.setEnabled(True)

    # Agent sessions ---------------------------------------------------------

    def _adopt_recording_session(self, session_id: str) -> None:
        try:
            adopt_recording_session(SessionPaths(self.config.recording_folder, session_id))
        except (OSError, RuntimeError) as exc:
            log.warning("cannot adopt recording session %s: %s", session_id, exc)

    def _close_editor_session(self) -> None:
        """Report ``closed`` to the agent of ``editor_session`` unless Process went out."""
        path, self.editor_session = self.editor_session, None
        sent, self.editor_event_sent = self.editor_event_sent, False
        if path is None or sent:
            return
        try:
            write_editor_event(path, "closed")
        except OSError as exc:
            log.warning("cannot write editor event %s: %s", path, exc)

    def _cancel_recording_session(self) -> None:
        """Cancel ``recording_session`` (closed without Process) unless already final."""
        session_id, self.recording_session = self.recording_session, None
        if session_id is None:
            return
        try:
            cancel_recording_session(SessionPaths(self.config.recording_folder, session_id))
        except (OSError, RuntimeError) as exc:
            log.warning("cannot cancel recording session %s: %s", session_id, exc)

    def _take_attach_requests(self) -> list[AttachRequest]:
        try:
            return take_attach_requests(self.config)
        except OSError as exc:
            log.warning("cannot read attach requests: %s", exc)
            return []

    def check_attach_requests(self) -> list[AttachRequest]:
        """Serve pending attach requests (the 1 s timer slot); returns what it handled.

        Nothing is taken while a modal dialog (Render) runs its own event loop or a
        recording is still loading: the request waits in its folder for the next tick.
        """
        if QApplication.activeModalWidget() is not None or self.loading:
            return []
        requests = self._take_attach_requests()
        for request in requests:
            self.attach(request)
        return requests

    def _decline_attach_requests(self) -> list[AttachRequest]:
        """Answer pending requests with ``closed`` instead of attaching.

        Called before the timer starts (a request meant for an editor that was closing
        must not hijack this window and its own agent) and once more while closing (a
        request landing during shutdown would otherwise be orphaned until it expires).
        """
        requests = self._take_attach_requests()
        for request in requests:
            log.info("declining attach request %s (not attaching)", request.path.name)
            try:
                write_editor_event(request.editor_session, "closed")
            except OSError as exc:
                log.warning("cannot write editor event %s: %s", request.editor_session, exc)
            if request.recording_session is not None:
                try:
                    cancel_recording_session(
                        SessionPaths(self.config.recording_folder, request.recording_session)
                    )
                except (OSError, RuntimeError) as exc:
                    log.warning(
                        "cannot cancel recording session %s: %s", request.recording_session, exc
                    )
        return requests

    def attach(self, request: AttachRequest) -> None:
        """A later ``edit --detach`` (or the control window) wants this window.

        The previous editor session that is still waiting gets ``closed`` and a
        previous recording session that was not finished is cancelled: only one agent
        waits on this window at a time, and the earlier one must not hang forever.
        """
        if self.editor_session != request.editor_session:
            self._close_editor_session()
            self.editor_session = request.editor_session
        try:
            write_editor_event(self.editor_session, "open", pid=os.getpid())
        except OSError as exc:
            log.warning("cannot record pid in %s: %s", self.editor_session, exc)
            self.editor_event_sent = False
        if request.recording_session is not None:
            if self.recording_session not in (None, request.recording_session):
                self._cancel_recording_session()
            self.recording_session = request.recording_session
            self._adopt_recording_session(request.recording_session)
        if request.session_id is None:
            self.refresh_recordings()
        else:
            self.recordings.refresh()
            row = self.recordings.default_row(request.session_id)
            if row >= 0 and self.recordings.item(row).session_id != request.session_id:
                log.warning("attach request for unknown recording %s", request.session_id)
            if row >= 0:
                self.list_view.setCurrentIndex(self.recordings.index(row))
            else:
                self._select_row(row)
        self._update_status()
        # Un-minimize only: showNormal() would also drop a maximized window to normal.
        self.setWindowState(
            (self.windowState() & ~Qt.WindowState.WindowMinimized) | Qt.WindowState.WindowActive
        )
        self.raise_()
        self.activateWindow()
        log.info(
            "attached: editor session %s, recording session %s, recording %s",
            request.editor_session.name,
            request.recording_session,
            request.session_id or "(current)",
        )

    # Status -----------------------------------------------------------------

    def _show_status_message(self, text: str) -> None:
        self.statusBar().showMessage(text, 8000)

    def _agent_status(self) -> str:
        if self.recording_session is not None:
            return f"waiting for Process (recording {self.recording_session})"
        if self.editor_session is not None:
            if self.editor_event_sent:
                return "sent to agent"
            return f"waiting for Process ({self.editor_session.stem})"
        return ""

    def _update_status(self) -> None:
        agent = self._agent_status()
        suffix = f"   |   {agent}" if agent else ""
        if self.recording is None:
            self.status_label.setText("No recording selected" + suffix)
            return
        width, height = self.recording.screen_size(self.model.selected_screen)
        size = f"{width}x{height}" if width else "unknown size"
        frames = round(self.model.crop_length_ms / 1000 * EXPORT_FPS)
        self.status_label.setText(
            f"{self.recording.session_id}   |   {self.model.selected_screen} {size}   |   "
            f"crop {format_timestamp(self.model.crop_length_ms)} "
            f"({frames} frames at {EXPORT_FPS} fps)" + suffix
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
        self._attach_timer.stop()
        self._process_feedback_timer.stop()
        self.player.stop()
        self._load_generation += 1  # ignore late results
        self._load_pool.waitForDone(10_000)
        self.timeline.shutdown()
        self.preview.view.wait()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        self._attach_timer.stop()
        self._flush_crop()
        # Closing without Process: the agents learn that nothing is to be done.
        self._close_editor_session()
        self._cancel_recording_session()
        self._decline_attach_requests()  # a request that arrived during shutdown
        self.save_settings()
        self.shutdown()
        super().closeEvent(event)


__all__ = ["EditorWindow", "PROCESS_BUTTON_STYLE", "TimeEdit", "WINDOW_TITLE"]
