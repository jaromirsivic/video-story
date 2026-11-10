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
* **Process** closes the window once the event went out: the work continues in the
  agent.
* Attach requests: a later ``edit --detach`` while this window runs writes a request
  file; a 1 s timer picks it up, loads the requested recording, adopts the new editor
  session / recording session and raises the window.

The crop ``[In, Out]`` is stored next to the recording in ``crop.json`` on every change
(handle drag - debounced to the end of the drag -, I/O keys, In/Out editors, Reset
Crop) and restored when the recording is loaded again.

The Edit button next to the subtitle opens :class:`SubtitleDialog` for the segment under
the playhead; Save rewrites ``transcript.json`` and ``transcript.txt``, so the agent
reads the corrected words.

A recording that arrives from the Stop dialog is not transcribed yet: the window
starts :class:`TranscriptionManager` for the shown recording, the timeline shows a
green arrow at the transcription front with the estimated time to the end, subtitles
appear as whisper produces them, and **Render** and **Process** stay disabled until the
transcript is complete. With **Auto process** ticked, a complete transcript starts a
five second countdown on the Process button that presses it by itself unless the user
clicks or types.

The bottom row reads **Auto process**, the green **Process**, **Instructions**, a
separator and the blue **Render**. Instructions opens a text box for the master
instructions for the AI about the whole recording (``instructions.txt`` next to the
recording, empty by default); they travel to the agent with the crop. **Stop** returns
to where **Play** was last pressed (a white arrow on the ruler). The theme button above
**Edit** switches between the light and the dark theme (remembered in the settings).

The list's menu offers **Archive Video** / **Archive Videos (N)** under Open Folder
Location (N counts only the selected recordings that can still be archived: not
archived, not being archived, transcript complete). Archiving runs in
:class:`ArchiveManager`, one recording after another; recordings older than
``archive_after_days`` join that queue when the window starts, and so do archives an
earlier editor left unfinished. A recording in the queue cannot be edited: the preview
says "This video is being archived." with the percentage. An archived recording shows
the still of every screen (``archive/<screenId>.jpg``), a flat audio line and the
subtitles; the playhead moves and subtitles can be edited, but Auto process, Process,
Instructions and Render are disabled; `ARCHIVED` blinks in the middle of the preview.

Under the list sit the search box, the magnifier button and the Refresh button (a
circular arrow). Enter or the magnifier searches the subtitles (see
:mod:ideo_story.editor.search): the shown recording at once, the others in the
background; matching recordings get a yellow frame, matching narration boxes turn
yellow.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from pathlib import Path

from PySide6.QtCore import (
    QEvent,
    QModelIndex,
    QObject,
    QPoint,
    QRegularExpression,
    QRunnable,
    QSettings,
    QSize,
    Qt,
    QThreadPool,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import (
    QAction,
    QCloseEvent,
    QDesktopServices,
    QFocusEvent,
    QFont,
    QKeyEvent,
    QKeySequence,
    QPainter,
    QPaintEvent,
    QPalette,
    QPen,
    QRegularExpressionValidator,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSlider,
    QSplitter,
    QSplitterHandle,
    QStatusBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from video_story.core.archive_state import request_archive
from video_story.core.config import EXPORT_FPS, Config
from video_story.core.crop import Crop, read_crop, write_crop
from video_story.core.instructions import read_instructions, write_instructions
from video_story.core.session import SessionPaths
from video_story.core.timeline import format_timestamp, parse_timestamp
from video_story.core.transcript import transcript_status
from video_story.export import ExportRequest, ExportResult
from video_story.host.editor_session import (
    AttachRequest,
    adopt_recording_session,
    cancel_recording_session,
    finish_recording_session,
    take_attach_requests,
    update_recording_session_progress,
    write_editor_event,
)
from video_story.host.retention import (
    IN_USE_REASON,
    delete_recording,
    recording_in_use,
    recordings_due_for_archive,
)
from video_story.ui.icons import refresh_icon, search_icon

from .archiving import PHASE_TRANSCRIBING, ArchiveManager
from .instructions_dialog import InstructionsDialog
from .player import Player
from .preview import PreviewPanel
from .recording import LoadedRecording
from .recordings_model import ItemRole, RecordingItem, RecordingsModel
from .recordings_view import BADGE_ARCHIVED, MIN_LIST_WIDTH, RecordingsListView, format_percent
from .render_dialog import ExportFn, ask_output_path, default_output_path, run_render
from .search import SearchJob, SearchSignals, normalize_query, text_matches
from .subtitle_dialog import SubtitleDialog
from .theme import apply_theme, system_prefers_dark
from .timeline.model import TimelineModel
from .timeline.view import TimelineView, format_eta
from .transcription import TranscriptionManager
from .workers import automatic_gc_paused, collect_garbage

log = logging.getLogger(__name__)

WINDOW_TITLE = "Video Story Editor"
TICK_MS = 33
STEP_MS = 1000.0 / EXPORT_FPS
ZOOM_SLIDER_MAX = 1000
TIME_TEXT = re.compile(r"^\d{2,}:\d{2}:\d{2}(\.\d{1,3})?$")  # HH:MM:SS[.mmm]
CROP_SAVE_DEBOUNCE_MS = 300  # a handle drag writes crop.json once it comes to rest
ATTACH_POLL_MS = 1000
PROCESS_FEEDBACK_MS = 2000
AUTO_PROCESS_SECONDS = 5  # Auto process: countdown before Process is pressed by itself
AUTO_PROCESS_TICK_MS = 1000
SESSION_PROGRESS_INTERVAL_S = 5.0  # how often state.json learns the transcription progress
ETA_MIN_ELAPSED_S = 3.0  # no estimate before the transcription ran this long
ETA_MIN_PROGRESS = 0.01  # ... and advanced this much
ACTION_BUTTON_SIZE = (128, 32)  # Process, Instructions and Render (smaller than before)
INSTRUCTIONS_LABEL = "Instructions"
INSTRUCTIONS_SET_LABEL = "Instructions •"  # a bullet marks stored instructions
INSTRUCTIONS_TOOLTIP = (
    "Master instructions for the AI about the whole recording, e.g. “create an "
    "implementation plan” (Ctrl+I). Empty by default."
)
PROCESS_BUTTON_STYLE = """
QPushButton {
    background-color: #1e9e3e; color: white; border: 1px solid #167a30; border-radius: 4px;
}
QPushButton:hover { background-color: #23b347; }
QPushButton:pressed { background-color: #167a30; }
QPushButton:disabled { background-color: #8fcf9f; color: #f4fff6; border-color: #7fbf8f; }
"""
RENDER_BUTTON_STYLE = """
QPushButton {
    background-color: #1f6fd0; color: white; border: 1px solid #185aa8; border-radius: 4px;
}
QPushButton:hover { background-color: #2a7fe6; }
QPushButton:pressed { background-color: #185aa8; }
QPushButton:disabled { background-color: #9dbfe8; color: #f3f8ff; border-color: #8fb2dc; }
"""
DELETE_ONE_LABEL = "Delete Video"
ARCHIVE_ONE_LABEL = "Archive Video"
ARCHIVING_MESSAGE = "This video is being archived."
ARCHIVED_NOTE = "Not available for an archived video"
BEING_ARCHIVED_REASON = "it is being archived"
TRANSCRIBING_NOTE = "Available once the transcription is complete"
SEARCH_PLACEHOLDER = "Search the spoken text"
SEARCH_TOOLTIP = (
    "Find a word or phrase in the subtitles of every recording; upper and lower case and "
    "diacritics do not matter"
)
SEARCH_ICON_SIZE = 18
SEARCH_BUTTON_MIN = 26  # px: the search and refresh buttons are at least this tall


def delete_label(count: int) -> str:
    """``Delete Video`` for one recording, ``Delete Videos (8)`` for more."""
    return DELETE_ONE_LABEL if count == 1 else f"Delete Videos ({count})"


def archive_label(count: int) -> str:
    """``Archive Video`` for one recording, ``Archive Videos (4)`` for more."""
    return ARCHIVE_ONE_LABEL if count == 1 else f"Archive Videos ({count})"


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
        with automatic_gc_paused():
            try:
                recording = LoadedRecording.load(self._paths)
            except Exception as exc:  # noqa: BLE001 - reported in the window
                log.exception("loading %s failed", self._paths.session_id)
                self._signals.done.emit(self._generation, None, str(exc))
                return
            self._signals.done.emit(self._generation, recording, "")


def format_crop_time(ms: int) -> str:
    """``HH:MM:SS.cc`` - the crop editors and length show hundredths only (narrower)."""
    return format_timestamp(max(0, int(ms)))[:-1]


class SeparatorHandle(QSplitterHandle):
    """Splitter handle with a thin line across, visible on light and dark backgrounds
    (the style's grip dots alone vanish on a white background)."""

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        painter = QPainter(self)
        window = self.palette().color(QPalette.ColorRole.Window)
        if window.lightness() >= 128:
            color = self.palette().color(QPalette.ColorRole.Dark)
        else:
            color = window.lighter(220)
        painter.setPen(QPen(color, 1))
        if self.orientation() == Qt.Orientation.Vertical:  # a horizontal line
            y = self.height() // 2
            painter.drawLine(0, y, self.width(), y)
        else:
            x = self.width() // 2
            painter.drawLine(x, 0, x, self.height())
        painter.end()


class SeparatorSplitter(QSplitter):
    """QSplitter whose handles draw :class:`SeparatorHandle`'s line."""

    def createHandle(self) -> QSplitterHandle:  # noqa: N802
        return SeparatorHandle(self.orientation(), self)


class TimeEdit(QLineEdit):
    """``HH:MM:SS.cc`` editor; ``committed(ms)`` fires on Enter / focus out.

    It shows hundredths of a second; the value itself keeps milliseconds, and text
    left as shown commits nothing (so focus-out never rounds the crop). The text is
    parsed on commit (``HH:MM:SS`` without a fraction and one to three decimals are
    accepted); anything unparsable reverts to the last value. ``editingFinished`` is not used:
    QLineEdit only emits it for text its validator rates Acceptable, so a partial
    entry would otherwise survive Enter and focus-out untouched.
    """

    committed = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setValidator(QRegularExpressionValidator(QRegularExpression(r"[0-9:.]*"), self))
        self.setFixedWidth(self.fontMetrics().horizontalAdvance("00:00:00.00") + 16)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._value = 0
        self.set_value(0)

    def set_value(self, ms: int) -> None:
        self._value = max(0, ms)
        self.setText(format_crop_time(self._value))

    @property
    def value(self) -> int:
        return self._value

    def commit(self) -> None:
        """Parse the text: emit ``committed`` or revert to the last valid value."""
        text = self.text().strip()
        if text == format_crop_time(self._value):
            return  # untouched: the text drops the last digit, the real value stays
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
        if self.text() != format_crop_time(self._value):
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
    transcription_changed = Signal(str)  # session id whose transcript changed (tests)

    def __init__(
        self,
        config: Config,
        session_id: str | None = None,
        export_fn: ExportFn | None = None,
        settings: QSettings | None = None,
        parent: QWidget | None = None,
        editor_session: Path | None = None,
        recording_session: str | None = None,
        config_file: Path | None = None,
        transcription: TranscriptionManager | None = None,
        archiver: ArchiveManager | None = None,
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
        self._paused_at: int | None = None  # playhead when Pause was pressed (resume keeps
        # the play marker; Play from another position sets a new one)
        self._crop_dirty_for: LoadedRecording | None = None  # pending debounced write
        self.transcription = transcription or TranscriptionManager(config, config_file, self)
        self.transcription.started.connect(self._on_transcription_started)
        self.transcription.segment.connect(self._on_transcription_segment)
        self.transcription.progress.connect(self._on_transcription_progress)
        self.transcription.finished.connect(self._on_transcription_finished)
        self.transcription.failed.connect(self._on_transcription_failed)
        self.archiver = archiver or ArchiveManager(config, config_file, self)
        self.archiver.started.connect(self._on_archive_started)
        self.archiver.progress.connect(self._on_archive_progress)
        self.archiver.finished.connect(self._on_archive_finished)
        self.archiver.failed.connect(self._on_archive_failed)
        self._transcription_failed: dict[str, str] = {}  # session id -> message
        self._transcription_fraction: dict[str, float] = {}
        # session id -> (monotonic time, fraction) of the first progress event: the ETA
        # is extrapolated from the progress made since then.
        self._transcription_origin: dict[str, tuple[float, float]] = {}
        self._transcription_eta: dict[str, float] = {}  # session id -> seconds to go
        self._session_progress_at = 0.0  # monotonic time of the last state.json update
        self._countdown: int | None = None  # Auto process: seconds left, None when idle
        self._auto_cancelled = False  # the user acted during the countdown
        self._instructions = ""  # master instructions of the shown recording
        self._dark = system_prefers_dark()
        self._theme_chosen = False  # the user (or the settings) picked a theme
        self._search_query = ""  # the phrase searched last ("" = no search)
        self._search_generation = 0  # results of an older search are ignored
        self._search_cancel: threading.Event | None = None
        self._search_current_hits = 0  # matching subtitles of the shown recording
        self._search_pool = QThreadPool(self)
        self._search_pool.setMaxThreadCount(1)
        self._search_signals = SearchSignals(self)
        self._search_signals.match.connect(self._on_search_match)
        self._search_signals.finished.connect(self._on_search_finished)

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
        self._auto_timer = QTimer(self)
        self._auto_timer.setInterval(AUTO_PROCESS_TICK_MS)
        self._auto_timer.timeout.connect(self._on_auto_tick)
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)  # any click or key cancels the Auto process countdown

        if self.recording_session is not None:
            self._adopt_recording_session(self.recording_session)

        self.recordings = RecordingsModel(config.recording_folder, self)
        self.list_view.setModel(self.recordings)
        self.list_view.selectionModel().currentChanged.connect(self._on_current_changed)
        self._start_archive_queue()
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
        self.list_view = RecordingsListView()
        self.list_view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.list_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list_view.customContextMenuRequested.connect(self._show_list_menu)
        left_layout.addWidget(self.list_view, 1)
        # Under the list: the search box, the magnifier (search) and Refresh (icon only).
        search_row = QHBoxLayout()
        search_row.setSpacing(4)
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText(SEARCH_PLACEHOLDER)
        self.search_edit.setToolTip(SEARCH_TOOLTIP)
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.returnPressed.connect(self.search_recordings)
        self.search_edit.textChanged.connect(self._on_search_text_changed)
        search_row.addWidget(self.search_edit, 1)
        self.search_button = self._icon_button("Search", f"{SEARCH_TOOLTIP} (Enter)")
        self.search_button.clicked.connect(self.search_recordings)
        search_row.addWidget(self.search_button)
        self.refresh_button = self._icon_button("Refresh", "Rescan the recording folder (F5)")
        self.refresh_button.clicked.connect(self.refresh_recordings)
        search_row.addWidget(self.refresh_button)
        left_layout.addLayout(search_row)
        self._apply_icons()
        # Dragged narrower than the rows need, the list snaps away completely.
        left.setMinimumWidth(MIN_LIST_WIDTH)
        self.main_splitter.addWidget(left)

        self.right_splitter = SeparatorSplitter(Qt.Orientation.Vertical)
        self.preview = PreviewPanel()
        self.preview.view.set_draw_cursor(self.config.editor_draw_cursor)
        self.preview.play_pause.connect(self.toggle_play)
        self.preview.stop.connect(self.stop_playback)
        self.preview.go_in.connect(self.go_to_in)
        self.preview.go_out.connect(self.go_to_out)
        self.preview.step.connect(self.step_frames)
        self.preview.edit_subtitle.connect(self.edit_subtitle)
        self.preview.theme_toggled.connect(self.toggle_theme)
        self.preview.set_dark(self._dark)
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
        # No Fit button: the slider's left end (or "-") is the whole recording; Ctrl+0
        # still jumps there.
        bottom_layout.addLayout(tools)

        self.timeline = TimelineView(self.model)
        self.timeline.playhead_moved.connect(self._on_playhead_moved)
        self.timeline.playhead_drag_finished.connect(self._on_playhead_drag_finished)
        self.timeline.crop_changed.connect(self._on_crop_changed)
        self.timeline.screen_selected.connect(self._on_screen_selected)
        self.timeline.view_changed.connect(self._sync_zoom_slider)
        bottom_layout.addWidget(self.timeline, 1)

        # Bottom row: Auto process | Process | Instructions || Render.
        render_row = QHBoxLayout()
        render_row.setSpacing(12)
        render_row.addStretch(1)
        # Hidden unless the transcription of the shown recording failed.
        self.retry_transcription_button = QPushButton("Retry transcription")
        self.retry_transcription_button.setToolTip(
            "The transcription failed; try again from where it stopped"
        )
        self.retry_transcription_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.retry_transcription_button.clicked.connect(self.retry_transcription)
        self.retry_transcription_button.hide()
        render_row.addWidget(self.retry_transcription_button)
        # Auto process is a decision about the recording on screen right now: it is
        # never ticked when the editor opens (it is not remembered) and it is unticked
        # whenever another recording is selected or handed to this window.
        self.auto_process_checkbox = QCheckBox("Auto process")
        self.auto_process_checkbox.setToolTip(
            f"Once the transcription is complete, count down {AUTO_PROCESS_SECONDS} s on the "
            "Process button and press it by itself; any click or key stops the countdown. "
            "Applies to this recording only: it is unticked when the editor opens and "
            "when you select another recording"
        )
        self.auto_process_checkbox.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.auto_process_checkbox.toggled.connect(self._on_auto_process_toggled)
        render_row.addWidget(self.auto_process_checkbox)
        action_font = QFont(self.font())
        action_font.setBold(True)
        action_font.setPointSizeF(action_font.pointSizeF() + 1)
        self.process_button = QPushButton("Process")
        self.process_tooltip = "Send the cropped range to the agent for processing (Ctrl+Enter)"
        self.process_button.setToolTip(self.process_tooltip)
        self.process_button.setFont(action_font)
        self.process_button.setMinimumSize(*ACTION_BUTTON_SIZE)
        self.process_button.setStyleSheet(PROCESS_BUTTON_STYLE)
        self.process_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.process_button.setEnabled(False)
        self.process_button.clicked.connect(self.process)
        render_row.addWidget(self.process_button)
        self.instructions_button = QPushButton(INSTRUCTIONS_LABEL)
        self.instructions_button.setToolTip(INSTRUCTIONS_TOOLTIP)
        self.instructions_button.setFont(action_font)
        self.instructions_button.setMinimumSize(*ACTION_BUTTON_SIZE)
        self.instructions_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.instructions_button.setEnabled(False)
        self.instructions_button.clicked.connect(self.edit_instructions)
        render_row.addWidget(self.instructions_button)
        self.render_separator = QFrame()
        self.render_separator.setFrameShape(QFrame.Shape.VLine)
        self.render_separator.setFrameShadow(QFrame.Shadow.Sunken)
        self.render_separator.setFixedHeight(ACTION_BUTTON_SIZE[1] - 4)
        render_row.addWidget(self.render_separator)
        self.render_button = QPushButton("Render")
        self.render_tooltip = "Export the selected screen and crop to MP4 + SRT (Ctrl+R)"
        self.render_button.setToolTip(self.render_tooltip)
        self.render_button.setFont(action_font)
        self.render_button.setMinimumSize(*ACTION_BUTTON_SIZE)
        self.render_button.setStyleSheet(RENDER_BUTTON_STYLE)
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
        self.main_splitter.setSizes([380, 900])

        self.status_label = QLabel("")
        status = QStatusBar()
        status.addPermanentWidget(self.status_label, 1)
        self.setStatusBar(status)

    def _icon_button(self, name: str, tooltip: str) -> QToolButton:
        """A square icon-only button as tall as the search box; ``name`` stays its text
        (tests, accessibility) without being shown."""
        button = QToolButton()
        button.setText(name)
        button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        button.setToolTip(tooltip)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        side = max(SEARCH_BUTTON_MIN, self.search_edit.sizeHint().height())
        button.setFixedSize(side + 6, side)
        button.setIconSize(QSize(SEARCH_ICON_SIZE, SEARCH_ICON_SIZE))
        return button

    def _apply_icons(self) -> None:
        """(Re)draw the list's icons in the button text colour of the theme."""
        self.search_button.setIcon(search_icon(SEARCH_ICON_SIZE * 2))
        self.refresh_button.setIcon(refresh_icon(SEARCH_ICON_SIZE * 2))

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
        self.instructions_action = action("Instructions", "Ctrl+I", self.edit_instructions)
        self.theme_action = action("Toggle theme", "Ctrl+T", self.toggle_theme)
        self.refresh_action = action("Refresh", "F5", self.refresh_recordings)

    # Recordings list --------------------------------------------------------

    def refresh_recordings(self) -> None:
        current = self.recording.session_id if self.recording else self._current_item_id()
        self.recordings.refresh()
        row = self.recordings.default_row(current)
        if row >= 0 and self.recordings.item(row).session_id == current:
            self.list_view.setCurrentIndex(self.recordings.index(row))
            return
        self._select_row(row)

    # Search -----------------------------------------------------------------

    @property
    def search_query(self) -> str:
        """The phrase searched last (``""`` without a search)."""
        return self._search_query

    @property
    def search_running(self) -> bool:
        return self._search_pool.activeThreadCount() > 0

    def search_recordings(self) -> None:
        """Enter in the search box or the magnifier button: find the phrase in the
        subtitles, without regard to case and diacritics. The shown recording is
        searched first (its matching narration boxes turn yellow at once), the others
        in the background; every recording with a match gets a yellow frame in the list,
        and the phrase is drawn bold and red in the subtitle under the preview.
        An empty box clears the search."""
        query = " ".join(self.search_edit.text().split())
        self._cancel_search()
        self._search_generation += 1
        self._search_query = query
        self.timeline.set_search(query)
        self.preview.set_search(query)
        self.list_view.set_search_matches(())
        self._search_current_hits = 0
        folded = normalize_query(query)
        if not folded:
            return
        recording = self.recording
        if recording is not None:
            self._search_current_hits = self._count_hits(recording, folded)
            if self._search_current_hits:
                self.list_view.add_search_match(recording.session_id)
        shown = recording.session_id if recording is not None else None
        others = [item.paths for item in self.recordings.items if item.session_id != shown]
        self._show_status_message(f"Searching for “{query}”...")
        cancel = threading.Event()
        self._search_cancel = cancel
        self._search_pool.start(
            SearchJob(self._search_generation, folded, others, self._search_signals, cancel)
        )

    @staticmethod
    def _count_hits(recording: LoadedRecording, folded: str) -> int:
        return sum(1 for s in recording.segments if text_matches(s.text, folded))

    def _cancel_search(self) -> None:
        if self._search_cancel is not None:
            self._search_cancel.set()
            self._search_cancel = None

    def _on_search_text_changed(self, text: str) -> None:
        """Emptying the box (also with its clear button) ends the search at once."""
        if not text.strip() and self._search_query:
            self.search_recordings()

    def _on_search_match(self, generation: int, session_id: str) -> None:
        if generation == self._search_generation:
            self.list_view.add_search_match(session_id)

    def _on_search_finished(self, generation: int, _searched: int) -> None:
        if generation != self._search_generation:
            return
        self._search_cancel = None
        found = len(self.list_view.search_matches)
        hits = self._search_current_hits
        here = f"; {hits} subtitle{'s' if hits != 1 else ''} in this one" if hits else ""
        self._show_status_message(
            f"“{self._search_query}”: found in {found} recording{'s' if found != 1 else ''}{here}"
        )

    def _update_current_search_match(self) -> None:
        """The shown recording's subtitles changed (transcription, Edit): keep its frame
        in the list and the hit count in step with the search."""
        recording = self.recording
        folded = normalize_query(self._search_query)
        if recording is None or not folded:
            return
        self._search_current_hits = self._count_hits(recording, folded)
        matches = self.list_view.search_matches
        if self._search_current_hits:
            matches.add(recording.session_id)
        else:
            matches.discard(recording.session_id)
        self.list_view.set_search_matches(matches)

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

    def _on_current_changed(self, current: QModelIndex, _previous: QModelIndex) -> None:
        """The current row (the one clicked last, also with Shift or Ctrl) is shown."""
        item = current.data(ItemRole) if current.isValid() else None
        if isinstance(item, RecordingItem):
            self._load_recording(item)

    def selected_items(self) -> list[RecordingItem]:
        """The recordings selected in the list, in list order."""
        rows = sorted(index.row() for index in self.list_view.selectionModel().selectedRows())
        return [item for row in rows if (item := self.recordings.item(row)) is not None]

    def _current_item_id(self) -> str | None:
        item = self.list_view.currentIndex().data(ItemRole)
        return item.session_id if isinstance(item, RecordingItem) else None

    def archivable(self, items: list[RecordingItem]) -> list[RecordingItem]:
        """The recordings among ``items`` that can be archived now: not archived, not in
        the archive queue, transcript complete."""
        return [
            item
            for item in items
            if not item.archived
            and not item.archiving
            and not self.archiver.is_queued(item.session_id)
            and item.transcript_complete
        ]

    def _show_list_menu(self, pos: QPoint) -> None:
        """Right click on the list: Open Folder Location, Archive Video(s), Delete."""
        index = self.list_view.indexAt(pos)
        clicked = index.data(ItemRole) if index.isValid() else None
        if not isinstance(clicked, RecordingItem):
            return
        selected = self.selected_items()
        if clicked.session_id not in {item.session_id for item in selected}:
            self.list_view.setCurrentIndex(index)  # right click outside the selection
            selected = [clicked]
        menu = QMenu(self)
        open_action = menu.addAction("Open Folder Location")
        open_action.triggered.connect(lambda: self.open_folder_location(clicked))
        archivable = self.archivable(selected)
        archive_action = menu.addAction(archive_label(max(1, len(archivable))))
        archive_action.setEnabled(bool(archivable))
        if not archivable:
            archive_action.setToolTip(self._not_archivable_reason(selected))
        menu.setToolTipsVisible(True)
        archive_action.triggered.connect(lambda: self.archive_recordings(archivable))
        menu.addSeparator()
        delete_action = menu.addAction(delete_label(len(selected)))
        delete_action.triggered.connect(lambda: self.delete_recordings(selected))
        menu.exec(self.list_view.viewport().mapToGlobal(pos))
        menu.deleteLater()

    def open_folder_location(self, item: RecordingItem) -> bool:
        """Open the recording's folder in the system file manager."""
        folder = item.paths.final_dir
        if folder.is_dir() and QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder))):
            return True
        self._show_status_message(f"Cannot open {folder}")
        return False

    def _not_archivable_reason(self, items: list[RecordingItem]) -> str:
        if all(item.archived for item in items):
            return "Already archived"
        if any(not item.transcript_complete for item in items if not item.archived):
            return "Available once the transcription is complete"
        return "Already archived or being archived"

    def _confirm_archive(self, items: list[RecordingItem]) -> bool:
        """Ask before recordings are archived (tests override this)."""
        if len(items) == 1:
            text = f"Do you want to archive this video?\n\n{items[0].paths.final_dir}"
        else:
            text = f"Do you want to archive these {len(items)} videos?"
        answer = QMessageBox.question(
            self,
            archive_label(len(items)),
            text,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def archive_recordings(self, items: list[RecordingItem]) -> list[str]:
        """Queue ``items`` for archiving after a confirmation; returns the queued ids.

        From now on they cannot be edited: a shown one is unloaded (its audio and
        frames released, so that they can be deleted) and replaced by the notice.
        """
        items = self.archivable(items)
        if not items or not self._confirm_archive(items):
            return []
        queued: list[str] = []
        failures: list[str] = []
        for item in items:
            if recording_in_use(item.paths):
                failures.append(f"{item.title} ({item.session_id}): {IN_USE_REASON}")
                continue
            try:
                request_archive(item.paths, reason="user")
            except OSError as exc:
                failures.append(f"{item.title} ({item.session_id}): {exc}")
                continue
            queued.append(item.session_id)
        if self.recording is not None and self.recording.session_id in queued:
            self._unload_for_archive()
        for session_id in queued:
            if self.transcription.running == session_id:
                self.transcription.shutdown()
        self.refresh_recordings()
        for session_id in queued:
            self.archiver.enqueue(SessionPaths(self.config.recording_folder, session_id))
        self._show_status_message(f"Archiving {len(queued)} recording(s)")
        if failures:
            QMessageBox.warning(self, "Not archived", "\n".join(failures))
        return queued

    def _unload_for_archive(self) -> None:
        """Release the shown recording (audio stream, decoded frames) before its files
        are deleted by the archive worker."""
        self._load_generation += 1
        self._set_recording(None)
        collect_garbage()

    def _start_archive_queue(self) -> None:
        """Resume unfinished archives, retry leftover deletions once and queue the
        recordings older than ``archive_after_days``."""
        folder = self.config.recording_folder
        try:
            due = recordings_due_for_archive(folder, self.config.archive_after_days)
        except OSError as exc:
            log.warning("cannot look for old recordings: %s", exc)
            due = []
        requested = False
        for paths in due:
            if paths.session_id == self.recording_session:
                continue  # an agent waits for Process on it
            try:
                request_archive(paths, reason="age")
            except OSError as exc:
                log.warning("cannot queue %s for archiving: %s", paths.session_id, exc)
                continue
            log.info(
                "recording %s is older than %d days: archiving it",
                paths.session_id,
                self.config.archive_after_days,
            )
            requested = True
        if requested:
            self.recordings.refresh()
        for item in self.recordings.items:
            if item.archiving or item.archive_leftovers:
                self.archiver.enqueue(item.paths)

    def _archive_notice(self, session_id: str) -> str:
        phase = self.archiver.phase(session_id)
        if phase is not None and phase[0] == PHASE_TRANSCRIBING:
            return f"{ARCHIVING_MESSAGE}\nTranscribing first: {format_percent(phase[1])}"
        if phase is not None:
            fraction = phase[1]
        else:
            item = self.recordings.item(self.recordings.row_of(session_id))
            fraction = item.archive_fraction if item is not None else 0.0
        return f"{ARCHIVING_MESSAGE}\n{format_percent(fraction)}"

    def _show_archive_notice(self, session_id: str) -> None:
        self.preview.view.set_message(self._archive_notice(session_id), large=True)
        self.preview.set_subtitle("")

    def _refresh_archive_notice(self, session_id: str) -> None:
        if self.recording is None and self._current_item_id() == session_id:
            self._show_archive_notice(session_id)

    def _on_archive_started(self, session_id: str) -> None:
        self.list_view.set_activity(session_id, "archiving", 0.0)
        self._refresh_archive_notice(session_id)

    def _on_archive_progress(self, session_id: str, phase: str, fraction: float) -> None:
        kind = "transcribing" if phase == PHASE_TRANSCRIBING else "archiving"
        self.list_view.set_activity(session_id, kind, fraction)
        self._refresh_archive_notice(session_id)

    def _on_archive_finished(self, session_id: str, done: object) -> None:
        self.list_view.set_activity(session_id, None)
        leftovers = done.get("leftovers") if isinstance(done, dict) else None
        if leftovers:
            self._show_status_message(
                f"Archived {session_id}; {len(leftovers)} file(s) could not be deleted"
            )
        else:
            self._show_status_message(f"Archived {session_id}")
        self.refresh_recordings()

    def _on_archive_failed(self, session_id: str, message: str) -> None:
        self.list_view.set_activity(session_id, None)
        self._show_status_message(f"Archiving {session_id} stopped: {message}")
        self.refresh_recordings()

    def _confirm_delete(self, items: list[RecordingItem]) -> bool:
        """Ask before recordings are deleted for good (tests override this)."""
        if len(items) == 1:
            text = (
                "Permanently delete the folder of this recording?\n\n"
                f"{items[0].paths.final_dir}\n\nThis cannot be undone."
            )
        else:
            text = (
                f"Permanently delete the folders of these {len(items)} recordings?"
                "\n\nThis cannot be undone."
            )
        answer = QMessageBox.question(
            self,
            delete_label(len(items)),
            text,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _report_delete_failures(self, failures: list[str]) -> None:
        QMessageBox.warning(self, "Not deleted", "\n".join(failures))

    def delete_recordings(self, items: list[RecordingItem]) -> list[str]:
        """Delete the folders of ``items`` after a confirmation, then refresh the list.

        Returns the deleted session ids. A recording still in use by a running Video
        Story window (one an agent waits on) is kept and reported.
        """
        if not items or not self._confirm_delete(items):
            return []
        failures = [
            f"{item.title} ({item.session_id}): {IN_USE_REASON}"
            for item in items
            if recording_in_use(item.paths)
        ]
        failures += [
            f"{item.title} ({item.session_id}): {BEING_ARCHIVED_REASON}"
            for item in items
            if self.archiver.running == item.session_id
        ]
        items = [
            item
            for item in items
            if not recording_in_use(item.paths) and self.archiver.running != item.session_id
        ]
        for item in items:
            self.archiver.dequeue(item.session_id)
        ids = {item.session_id for item in items}
        if self.recording is not None and self.recording.session_id in ids:
            self._load_generation += 1  # a load in flight is dropped
            self._set_recording(None)  # releases the audio and the frames
        if self.transcription.running in ids:
            self.transcription.shutdown()
        collect_garbage()  # file handles of the unloaded recording are closed here
        deleted: list[str] = []
        for item in items:
            reason = delete_recording(item.paths)
            if reason is None:
                deleted.append(item.session_id)
            else:
                failures.append(f"{item.title} ({item.session_id}): {reason}")
        self.refresh_recordings()
        self._show_status_message(f"Deleted {len(deleted)} recording(s)")
        if failures:
            self._report_delete_failures(failures)
        return deleted

    def _load_recording(self, item: RecordingItem) -> None:
        if (
            self.recording is not None
            and self.recording.paths == item.paths
            and self.recording.archived == item.archived
        ):
            return
        if item.archiving or self.archiver.is_queued(item.session_id):
            # Queued for archiving: it cannot be edited any more (module docstring).
            self.auto_process_checkbox.setChecked(False)
            self._load_generation += 1
            self._set_recording(None)
            self._show_archive_notice(item.session_id)
            self._update_status()
            return
        # Another recording: Auto process was ticked for the previous one and must
        # never press Process over this one by itself.
        self.auto_process_checkbox.setChecked(False)
        self._load_generation += 1
        self._set_recording(None)
        self.preview.view.set_message(f"Loading {item.session_id}...")
        self.statusBar().showMessage(f"Loading {item.session_id}...")
        collect_garbage()  # closed windows/dialogs die here, on the GUI thread (see workers.py)
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
        self._stop_countdown()
        self._auto_cancelled = False  # a cancelled countdown belongs to the previous recording
        self.recording = recording
        self.preview.view.clear()
        archived = recording is not None and recording.archived
        self.preview.view.set_badge(BADGE_ARCHIVED if archived else None)  # blinks
        self.timeline.set_recording(recording)
        if recording is not None:
            self._restore_crop(recording)
        self._load_instructions(recording)
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
                self.preview.view.set_message(
                    "No still image" if recording.archived else "No frames"
                )
            self.timeline.setFocus()
        self._sync_playhead()
        self._update_transcription_ui()
        self._update_current_search_match()
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
        # The play marker (dark ruler triangle, where Stop returns) follows the playhead
        # once the mouse button is released after a click or drag on the timeline.
        self.model.set_play_marker(self.model.playhead_ms)
        self._paused_at = None
        self.timeline.update()
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
            self._paused_at = self.model.playhead_ms
            self._sync_playhead()
            return
        # Play pressed where the user put the playhead (not a plain resume of a pause):
        # that is where the white arrow goes and where Stop returns.
        fresh_start = self._paused_at is None or self._paused_at != self.model.playhead_ms
        self._start_playback()
        self._paused_at = None
        if self.player.playing and fresh_start:
            self.model.set_play_marker(self.model.playhead_ms)
            self.timeline.update()

    def stop_playback(self) -> None:
        """Stop and return to where Play was last pressed (In when there is no marker)."""
        self._resume_after_scrub = False
        self._paused_at = None
        self.player.stop()
        self._timer.stop()
        self.preview.set_playing(False)
        self.model.set_playhead(self.model.stop_position())
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
        self.crop_label.setText(f"length {format_crop_time(self.model.crop_length_ms)}")
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
        if self.recording.archived:
            self._show_status_message(ARCHIVED_NOTE + ".")
            return None
        if not self.actions_allowed:
            self._show_status_message(TRANSCRIBING_NOTE + ".")
            return None
        self._stop_countdown()
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
            self._update_buttons()
        self.last_export_result = result
        if result is not None:
            self._show_status_message(f"Rendered {result.output}")
        return result

    # Instructions -----------------------------------------------------------

    @property
    def instructions(self) -> str:
        """Master instructions of the shown recording (``""`` when none)."""
        return self._instructions

    def _load_instructions(self, recording: LoadedRecording | None) -> None:
        if recording is None:
            self._instructions = ""
        else:
            path = recording.paths.instructions_path(recording.paths.final_dir)
            self._instructions = read_instructions(path)
        self._update_instructions_button()

    def _update_instructions_button(self) -> None:
        has = bool(self._instructions)
        self.instructions_button.setText(INSTRUCTIONS_SET_LABEL if has else INSTRUCTIONS_LABEL)
        tooltip = INSTRUCTIONS_TOOLTIP
        if has:
            preview = self._instructions if len(self._instructions) <= 400 else (
                self._instructions[:400] + "..."
            )  # fmt: skip
            tooltip += "\n\nCurrent instructions:\n" + preview
        self.instructions_button.setToolTip(tooltip)
        self.instructions_button.setEnabled(self.recording is not None)
        self.instructions_action.setEnabled(self.recording is not None)

    def _instructions_dialog(self, text: str) -> InstructionsDialog:
        return InstructionsDialog(text, self)

    def edit_instructions(self) -> bool:
        """Open the Instructions dialog for the shown recording; True when saved."""
        recording = self.recording
        if recording is None:
            self._show_status_message("Select a recording first.")
            return False
        if self.player.playing:
            self.toggle_play()
        dialog = self._instructions_dialog(self._instructions)
        try:
            if dialog.exec() != InstructionsDialog.DialogCode.Accepted:
                return False
            text = dialog.instructions()
        finally:
            dialog.deleteLater()
        if self.recording is not recording:
            return False  # another recording was loaded meanwhile
        return self.set_instructions(text)

    def set_instructions(self, text: str) -> bool:
        """Store ``text`` as the instructions of the shown recording (``instructions.txt``)."""
        recording = self.recording
        if recording is None:
            return False
        path = recording.paths.instructions_path(recording.paths.final_dir)
        try:
            self._instructions = write_instructions(path, text)
        except OSError as exc:
            log.warning("cannot save instructions of %s: %s", recording.session_id, exc)
            self._show_status_message(f"Cannot save the instructions: {exc}")
            return False
        self._update_instructions_button()
        self._show_status_message(
            "Instructions saved" if self._instructions else "Instructions cleared"
        )
        return True

    # Theme ------------------------------------------------------------------

    @property
    def dark(self) -> bool:
        return self._dark

    def set_theme(self, dark: bool) -> None:
        """Apply the dark or the light theme to the application and the editor's canvases."""
        self._dark = dark
        self._theme_chosen = True
        apply_theme(dark)
        self.timeline.set_theme(dark)
        self.preview.set_dark(dark)  # after the palette: the icons take its text colour
        self._apply_icons()

    def toggle_theme(self) -> None:
        self.set_theme(not self._dark)

    # Subtitles --------------------------------------------------------------

    def _subtitle_dialog(self, segment, duration_ms: int) -> SubtitleDialog:
        return SubtitleDialog(segment, duration_ms, self)

    def edit_subtitle(self) -> bool:
        """Edit the segment shown as the subtitle at the playhead; True when saved."""
        recording = self.recording
        if recording is None:
            return False
        if self.player.playing:
            self.toggle_play()
        segment = recording.segment_at(self.model.playhead_ms)
        if segment is None:
            self._show_status_message("No subtitle at the playhead.")
            return False
        dialog = self._subtitle_dialog(segment, recording.duration_ms)
        try:
            if dialog.exec() != SubtitleDialog.DialogCode.Accepted:
                return False
            edited = dialog.segment()
        finally:
            dialog.deleteLater()
        if edited is None or edited == segment:
            return False
        if self.recording is not recording:
            return False  # another recording was loaded meanwhile
        try:
            recording.replace_segment(segment, edited)
        except (OSError, ValueError) as exc:
            log.warning("cannot save subtitle of %s: %s", recording.session_id, exc)
            self._show_status_message(f"Cannot save the subtitle: {exc}")
            return False
        self._sync_playhead()
        self._update_current_search_match()
        self._show_status_message(
            f"Subtitle saved: {format_timestamp(edited.start_ms)}-{format_timestamp(edited.end_ms)}"
        )
        return True

    # Process ----------------------------------------------------------------

    def process(self) -> Crop | None:
        """Send the shown recording and its crop to the agent, then close the window.

        Stores ``crop.json``, writes the ``process`` event into ``editor_session`` and
        finishes ``recording_session`` (``processed`` with the crop). The recording
        session is finished even when the user has meanwhile selected a different
        recording: the agent waiting on it must be released by the green button, and
        the editor session event carries the id actually shown. The crop applies to
        all screens; the selected track is irrelevant. When the event went out, the
        window closes (the work continues in the agent); on a failure it stays open so
        that the user can retry.
        """
        if self.recording is None:
            self._show_status_message("Select a recording first.")
            return None
        if self._process_feedback_timer.isActive():
            return None  # "Sent to agent" is still showing: no second event
        if not self.actions_allowed:
            self._show_status_message(TRANSCRIBING_NOTE + ".")
            return None
        awaited = self._awaited_recording_pending()
        if awaited is not None:
            # Process would also finish the session of the recording that came from
            # the Stop dialog, whose transcript is not complete yet.
            self._show_status_message(
                f"The agent waits for recording {awaited}, which is still being "
                "transcribed: select it and wait for the transcript."
            )
            return None
        self._stop_countdown()
        self._commit_time_edits()
        if self.player.playing:
            self.toggle_play()
        crop = self.crop
        session_id = self.recording.session_id
        instructions = self._instructions
        self._save_crop()
        problems: list[str] = []
        if self.editor_session is not None:
            try:
                write_editor_event(
                    self.editor_session,
                    "process",
                    session_id=session_id,
                    crop=crop,
                    instructions=instructions,
                )
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
                finish_recording_session(
                    paths, crop, shown_session_id=session_id, instructions=instructions
                )
            except (OSError, RuntimeError) as exc:
                log.warning("cannot finish recording session %s: %s", recording_session, exc)
                problems.append(f"recording session: {exc}")
            else:
                self.recording_session = None  # final now; closing must not cancel it
        detail = f"{session_id} {format_timestamp(crop.start_ms)}-{format_timestamp(crop.end_ms)}"
        if instructions:
            detail += " with instructions"
        self._update_status()
        if problems:
            # The button stays "Process": the user can retry; a session that could not be
            # finished is kept so that closing the window still cancels it.
            self._show_status_message(f"Process failed for {detail}: " + "; ".join(problems))
            return None
        self._show_process_feedback()
        self._show_status_message(f"Sent to agent: {detail}")
        self.processed.emit(session_id, crop)
        QTimer.singleShot(0, self.close)
        return crop

    def _show_process_feedback(self) -> None:
        self.process_button.setText("Sent to agent")
        self.process_button.setEnabled(False)
        self.process_action.setEnabled(False)  # Ctrl+Enter must not bypass the button
        self._process_feedback_timer.start()

    def _restore_process_button(self) -> None:
        self.process_button.setText("Process")
        self._update_buttons()

    # Transcription ----------------------------------------------------------

    @property
    def actions_allowed(self) -> bool:
        """Render and Process need a complete transcript (or one that cannot be made:
        a failed transcription must not lock the user out); an archived recording
        allows neither."""
        recording = self.recording
        if recording is None or recording.archived:
            return False
        return recording.transcript_complete or recording.session_id in self._transcription_failed

    def _awaited_recording_pending(self) -> str | None:
        """Id of the recording an agent waits for (``recording_session``) when it is not
        the one shown and its transcript is not complete; ``None`` otherwise."""
        session_id = self.recording_session
        if session_id is None:
            return None
        if self.recording is not None and self.recording.session_id == session_id:
            return None  # the shown recording: ``actions_allowed`` decides
        if session_id in self._transcription_failed:
            return None  # cannot be completed; must not lock the user out
        paths = SessionPaths(self.config.recording_folder, session_id)
        status = transcript_status(paths.transcript_json_path(paths.final_dir))
        return None if status.complete else session_id

    def retry_transcription(self) -> None:
        """Start the failed transcription of the shown recording again; it continues
        from the last stored segment and the agent's session stays as it is."""
        recording = self.recording
        if recording is None or recording.session_id not in self._transcription_failed:
            return
        del self._transcription_failed[recording.session_id]
        self._show_status_message("Transcription restarted.")
        self._update_transcription_ui()

    def _update_buttons(self) -> None:
        failed = self.recording is not None and (
            self.recording.session_id in self._transcription_failed
        )
        self.retry_transcription_button.setVisible(failed)
        if failed:
            self.retry_transcription_button.setToolTip(
                "The transcription failed: "
                f"{self._transcription_failed[self.recording.session_id]}\n"
                "Try again from where it stopped"
            )
        allowed = self.actions_allowed
        archived = self.recording is not None and self.recording.archived
        if allowed or self.recording is None:
            note = ""
        else:
            note = f"\n{ARCHIVED_NOTE if archived else TRANSCRIBING_NOTE}"
        self.render_button.setEnabled(allowed)
        self.render_action.setEnabled(allowed)
        self.render_button.setToolTip(self.render_tooltip + note)
        self.process_button.setToolTip(self.process_tooltip + note)
        if not self._process_feedback_timer.isActive():
            self.process_button.setEnabled(allowed)
            self.process_action.setEnabled(allowed)
        editable = self.recording is not None and not archived
        self.instructions_button.setEnabled(editable)
        self.instructions_action.setEnabled(editable)
        if archived:
            self.auto_process_checkbox.setChecked(False)
        self.auto_process_checkbox.setEnabled(self.recording is None or not archived)

    def _update_transcription_ui(self) -> None:
        """Start the background transcription of the shown recording when it needs one
        and reflect its state on the timeline, the buttons and the countdown."""
        recording = self.recording
        if recording is None:
            self.timeline.set_transcription(None)
        elif recording.transcript_complete:
            self.timeline.set_transcription(None)
        elif recording.session_id in self._transcription_failed:
            self.timeline.set_transcription("failed")
        else:
            self.transcription.start(recording)  # adopts it when already running
            state = (
                "transcribing" if self.transcription.is_running(recording.session_id) else "pending"
            )
            self.timeline.set_transcription(
                state, recording.transcribed_ms, self._transcription_eta.get(recording.session_id)
            )
        self._update_buttons()
        self._update_status()
        self._maybe_start_countdown()

    def _shown(self, session_id: str) -> LoadedRecording | None:
        recording = self.recording
        return recording if recording is not None and recording.session_id == session_id else None

    def _on_transcription_started(self, session_id: str) -> None:
        self._transcription_origin.pop(session_id, None)
        self._transcription_eta.pop(session_id, None)
        recording = self._shown(session_id)
        if recording is not None:
            self.timeline.set_transcription("transcribing", recording.transcribed_ms)
            self._update_status()

    def _on_transcription_segment(self, session_id: str, _segment: object) -> None:
        recording = self._shown(session_id)
        if recording is None:
            return
        self.timeline.set_transcription(
            "transcribing", recording.transcribed_ms, self._transcription_eta.get(session_id)
        )
        self.preview.set_subtitle(recording.subtitle_at(self.model.playhead_ms))
        self._update_current_search_match()
        self.transcription_changed.emit(session_id)

    def transcription_eta(self, session_id: str) -> float | None:
        """Estimated seconds until the transcription of ``session_id`` ends (``None``
        while there is too little progress to extrapolate from)."""
        return self._transcription_eta.get(session_id)

    def _estimate_eta(self, session_id: str, fraction: float, now: float) -> float | None:
        """Extrapolate from the progress made since the first progress event: the
        remaining fraction at the pace observed so far."""
        origin = self._transcription_origin.get(session_id)
        if origin is None:
            self._transcription_origin[session_id] = (now, fraction)
            return None
        elapsed = now - origin[0]
        progressed = fraction - origin[1]
        if elapsed < ETA_MIN_ELAPSED_S or progressed < ETA_MIN_PROGRESS:
            return None
        return max(0.0, elapsed * (1.0 - fraction) / progressed)

    def _on_transcription_progress(self, session_id: str, fraction: float, reached: int) -> None:
        self._transcription_fraction[session_id] = fraction
        self.list_view.set_activity(session_id, "transcribing", fraction)
        now = time.monotonic()
        eta = self._estimate_eta(session_id, fraction, now)
        if eta is None:
            self._transcription_eta.pop(session_id, None)
        else:
            self._transcription_eta[session_id] = eta
        recording = self._shown(session_id)
        if recording is not None:
            self.timeline.set_transcription(
                "transcribing", max(reached, recording.transcribed_ms), eta
            )
            self._update_status()
        if self.recording_session == session_id:
            if now - self._session_progress_at >= SESSION_PROGRESS_INTERVAL_S:
                self._session_progress_at = now
                eta_text = f" (ETA: {format_eta(eta)})" if eta is not None else ""
                try:
                    update_recording_session_progress(
                        SessionPaths(self.config.recording_folder, session_id),
                        fraction,
                        f"Video Story Editor open; transcribing {int(fraction * 100)} %"
                        f"{eta_text} (waiting for Process).",
                    )
                except OSError as exc:
                    log.debug("progress not written to state.json: %s", exc)

    def _on_transcription_finished(self, session_id: str, done: object) -> None:
        self.list_view.set_activity(session_id, None)
        self._transcription_fraction.pop(session_id, None)
        self._transcription_origin.pop(session_id, None)
        self._transcription_eta.pop(session_id, None)
        recording = self._shown(session_id)
        if recording is None:
            return
        count = len(recording.transcript.segments) if recording.transcript else 0
        self._show_status_message(
            f"Transcription complete: {count} subtitle{'s' if count != 1 else ''}"
            + (
                f" ({recording.transcript.language})"
                if recording.transcript and recording.transcript.language
                else ""
            )
        )
        self._sync_playhead()
        self.refresh_recordings()  # the list shows the first line and drops "transcribing"
        self._update_transcription_ui()
        self.transcription_changed.emit(session_id)

    def _on_transcription_failed(self, session_id: str, message: str) -> None:
        self.list_view.set_activity(session_id, None)
        self._transcription_failed[session_id] = message
        self._transcription_fraction.pop(session_id, None)
        self._transcription_origin.pop(session_id, None)
        self._transcription_eta.pop(session_id, None)
        if self._shown(session_id) is not None:
            self._show_status_message(f"Transcription failed: {message}")
            self._update_transcription_ui()
        self.transcription_changed.emit(session_id)

    # Auto process -----------------------------------------------------------

    def _has_agent_session(self) -> bool:
        """An agent waits for Process (the Stop dialog's recording session or an
        ``edit --detach`` editor session that was not served yet)."""
        if self.recording_session is not None:
            return True
        return self.editor_session is not None and not self.editor_event_sent

    @property
    def countdown(self) -> int | None:
        """Seconds left on the Auto process countdown, ``None`` while idle."""
        return self._countdown

    def _on_auto_process_toggled(self, checked: bool) -> None:
        self._auto_cancelled = False
        if checked:
            self._maybe_start_countdown()
        else:
            self._stop_countdown()

    def _maybe_start_countdown(self) -> None:
        if (
            self._countdown is not None
            or not self.auto_process_checkbox.isChecked()
            or self._auto_cancelled
            or self._process_feedback_timer.isActive()
            or self.recording is None
            or not self.recording.transcript_complete
            or not self._has_agent_session()
        ):
            return
        self._countdown = AUTO_PROCESS_SECONDS
        self._show_countdown()
        self._auto_timer.start()
        self._show_status_message(
            f"Auto process: Process is pressed in {AUTO_PROCESS_SECONDS} s; "
            "any click or key cancels it."
        )

    def _show_countdown(self) -> None:
        if self._countdown is not None and not self._process_feedback_timer.isActive():
            self.process_button.setText(f"Process ({self._countdown})")

    def _on_auto_tick(self) -> None:
        if self._countdown is None:
            self._auto_timer.stop()
            return
        if QApplication.activeModalWidget() is not None:
            return  # a dialog is open: hold the countdown
        self._countdown -= 1
        if self._countdown > 0:
            self._show_countdown()
            return
        self._stop_countdown()
        log.info("Auto process: pressing Process")
        self.process()

    def _stop_countdown(self) -> None:
        self._auto_timer.stop()
        if self._countdown is None:
            return
        self._countdown = None
        if not self._process_feedback_timer.isActive():
            self.process_button.setText("Process")

    def _cancel_countdown(self) -> None:
        """The user did something: the countdown stops for good (until the checkbox is
        toggled again or another transcript completes)."""
        if self._countdown is None:
            return
        self._stop_countdown()
        self._auto_cancelled = True
        self._show_status_message("Auto process cancelled; press Process when you are ready.")

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802
        # Installed on the application. A key that matches one of the window's
        # shortcuts (Space, I, O, arrows) never arrives as KeyPress: the shortcut map
        # takes it after the ShortcutOverride, so that one counts as the user's action.
        if self._countdown is not None and event.type() in (
            QEvent.Type.MouseButtonPress,
            QEvent.Type.KeyPress,
            QEvent.Type.ShortcutOverride,
            QEvent.Type.Wheel,
        ):
            self._cancel_countdown()
        return super().eventFilter(watched, event)

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
        Auto process is unticked: for the newcomer this is a freshly opened editor.
        """
        self.auto_process_checkbox.setChecked(False)
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
        self._maybe_start_countdown()  # an agent waits now
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

    def _transcription_status(self) -> str:
        recording = self.recording
        if recording is None or recording.transcript_complete:
            return ""
        if recording.session_id in self._transcription_failed:
            return "transcription failed"
        if (
            self.transcription.is_running(recording.session_id)
            or recording.session_id in self._transcription_fraction
        ):
            fraction = self._transcription_fraction.get(recording.session_id)
            if fraction is None and recording.duration_ms > 0:
                fraction = recording.transcribed_ms / recording.duration_ms
            percent = f" {int((fraction or 0.0) * 100)} %"
            eta = format_eta(self._transcription_eta.get(recording.session_id))
            return f"transcribing{percent}" + (f" (ETA: {eta})" if eta else "")
        return "transcription pending"

    def _update_status(self) -> None:
        """Recording id and selected screen (plus the transcription while it runs); the
        crop and the agent's state are not repeated here (user's request 2026-11-10)."""
        transcription = self._transcription_status()
        suffix = f"   |   {transcription}" if transcription else ""
        if self.recording is None:
            self.status_label.setText("No recording selected" + suffix)
            return
        width, height = self.recording.screen_size(self.model.selected_screen)
        size = f"{width}x{height}" if width else "unknown size"
        self.status_label.setText(
            f"{self.recording.session_id}   |   {self.model.selected_screen} {size}" + suffix
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
        # Auto process is deliberately not restored: the editor always opens with it
        # unticked, so that no recording is handed to the agent without a decision.
        if settings.contains("view/dark"):
            self.set_theme(settings.value("view/dark", self._dark, type=bool))

    def save_settings(self) -> None:
        settings = self._settings
        if settings is None:
            return
        settings.setValue("window/geometry", self.saveGeometry())
        settings.setValue("splitter/main", self.main_splitter.saveState())
        settings.setValue("splitter/right", self.right_splitter.saveState())
        settings.remove("process/auto")  # stored by earlier versions; no longer used
        if self._theme_chosen:
            settings.setValue("view/dark", self._dark)
        settings.sync()

    def shutdown(self) -> None:
        self._timer.stop()
        self._attach_timer.stop()
        self._process_feedback_timer.stop()
        self._auto_timer.stop()
        self._cancel_search()
        self._search_pool.waitForDone(5000)
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)
        self.player.stop()
        self.transcription.shutdown()  # the stored partial transcript resumes next time
        self.archiver.shutdown()  # stops cleanly; the next editor resumes the queue
        self._load_generation += 1  # ignore late results
        self._load_pool.waitForDone(10_000)
        self.timeline.shutdown()
        self.list_view.shutdown()
        self.preview.view.wait()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        self._attach_timer.stop()
        self._stop_countdown()
        self.transcription.stop()  # no more segments are stored once we start closing
        self._flush_crop()
        # Closing without Process: the agents learn that nothing is to be done.
        self._close_editor_session()
        self._cancel_recording_session()
        self._decline_attach_requests()  # a request that arrived during shutdown
        self.save_settings()
        self.shutdown()
        super().closeEvent(event)


__all__ = [
    "ACTION_BUTTON_SIZE",
    "AUTO_PROCESS_SECONDS",
    "INSTRUCTIONS_LABEL",
    "INSTRUCTIONS_SET_LABEL",
    "EditorWindow",
    "PROCESS_BUTTON_STYLE",
    "RENDER_BUTTON_STYLE",
    "TimeEdit",
    "WINDOW_TITLE",
]
