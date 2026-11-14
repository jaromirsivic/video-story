"""Custom-painted timeline: ruler, one video track per screen, audio track, narration
tracks, crop handles.

The narration under the audio track has two subtitle tracks (each half as tall as the
audio track): the upper one holds what the transcription produced, the lower one the
subtitles the user moved there (mostly to make room while rearranging them). Each
shows a box wherever a subtitle has words and nothing in the pauses. Zoomed in to
:data:`NARRATION_TEXT_MIN_ZOOM` px/s or more, each box shows its words, cut with an
ellipsis; hovering a box shows the whole text. Boxes that contain the phrase of the
recordings search (:mod:`video_story.editor.search`) are yellow. Boxes have slightly
rounded corners and a 2 px edge.

A padlock under the "narration" title locks the tracks (the default for every
recording). It can be opened only once the transcript is complete
(:meth:`TimelineView.set_narration_editable`). Unlocked, the subtitles are edited with
the mouse (:mod:`.narration_edit`: the zones, shown only by the cursor, move an edge,
move the whole subtitle - also to the other track - or the shared edge of two
touching subtitles). While dragging, a green rectangle (80 % opaque, rounded corners)
shows the result over the boxes; it turns red where it would overlap another subtitle
of the track and releasing then changes nothing.
:meth:`TimelineView.cancel_segment_drag` (Escape in the window) drops the drag;
releasing a valid one emits :attr:`TimelineView.segment_edit_requested` and the window
stores it.

Below the narration track there is a narrow lane for the background transcription: while
the recording is still being transcribed, a green arrow under the tracks marks how
far the transcription got (the audio beyond it is shaded) with "transcribing" and the
estimated time to the end written under the arrow; the lane stays empty once the
transcript is complete.

Keypoints (the Keypoint button while recording) are thin green lines across the tracks
with a green arrow at the top of the ruler, drawn like the red playhead: they cannot be
moved, show "Keypoint N" on hover and act as a magnet for the crop handles and the
playhead. Where the user paused the recording (the paused time is cut out) a gray line
with a gray arrow is drawn the same way: "Paused 42 s" on hover, a magnet as well.
A white arrow on the ruler marks where Play was last pressed
(Stop returns there). The +/- zoom keeps the playhead in the middle of the view.

Pressing the middle mouse button grabs the timeline: while the button is held the view
moves with the mouse (drag to the right and earlier times come into view), like a
horizontal two-finger scroll on a touchpad. The playhead and the crop stay where they
are.

A full screen / normal toggle in the audio header stretches the waveform vertically so
that the loudest sound of the recording reaches the full height of the track: quiet
speech becomes visible. Only the drawing is scaled; the audio itself is unchanged.

Next to it three icons: a microphone and a speaker switch the two audio tracks (the
microphone, the sound this computer played) on and off - the waveform shows the sum of
the tracks that are on, a crossed icon is a track that is off, a dim crossed speaker a
recording without computer audio - and a pen writing on paper transcribes the audio
again. The view only reports the clicks (:attr:`audio_track_toggled`,
:attr:`transcribe_requested`); the window changes the recording.
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QContextMenuEvent,
    QFont,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QPen,
    QPolygonF,
    QResizeEvent,
    QWheelEvent,
)
from PySide6.QtWidgets import QApplication, QScrollBar, QToolTip, QWidget

from video_story.core.audio_mix import COMPUTER, MICROPHONE
from video_story.core.timeline import format_timestamp
from video_story.editor.recording import LoadedRecording

from ..search import normalize_query, text_matches
from ..theme import PROCESS_GREEN
from . import narration_edit
from .model import TimelineModel
from .narration_edit import Zone
from .thumbnails import ThumbnailCache
from .waveform import BLOCK_MS, WaveformLoader, column_peaks

RULER_H = 26
HEADER_W = 92  # "narration"; the fit toggle and three audio icons
TRACK_H = 60
AUDIO_H = TRACK_H  # the audio track is as tall as each screen track
NARRATION_LANE_H = AUDIO_H // 2  # each narration track is half as tall as the audio track
NARRATION_TRACKS = 2  # upper (the transcription) and lower (subtitles moved there)
NARRATION_H = NARRATION_TRACKS * NARRATION_LANE_H + 1  # both tracks and the line between
NARRATION_TITLE = "narration"
NARRATION_TOOLTIP = (
    "Spoken text: one box per subtitle. Upper track: the transcription; lower track: "
    "subtitles moved out of the way"
)
LOCK_TOOLTIP_LOCKED = "Locked: click to edit the subtitles"
LOCK_TOOLTIP_UNLOCKED = "Unlocked: drag the subtitles; click to lock"
LOCK_TOOLTIP_UNAVAILABLE = "Unlocks once the transcription is complete"
BOX_RADIUS = 3.0  # rounded corners of the narration boxes and of the drag preview
BOX_EDGE = 2  # px: the edge of a narration box, the same on every side
COLOR_PREVIEW = QColor(0, 192, 0, 204)  # the drag preview: green, 80 % opaque
COLOR_PREVIEW_EDGE = QColor(0, 128, 0, 230)
COLOR_PREVIEW_BAD = QColor(224, 40, 40, 204)  # the preview where the change is not allowed
COLOR_PREVIEW_BAD_EDGE = QColor(150, 20, 20, 230)
ZONE_CURSORS = {
    Zone.MOVE: Qt.CursorShape.SizeAllCursor,
    Zone.RESIZE_START: Qt.CursorShape.SizeHorCursor,
    Zone.RESIZE_END: Qt.CursorShape.SizeHorCursor,
    Zone.ROLL: Qt.CursorShape.SplitHCursor,
}
NARRATION_TEXT_MIN_ZOOM = 40.0  # px per second from which the boxes show their words
NARRATION_TEXT_MIN_W = 24  # px: narrower boxes stay plain even zoomed in
LOCK_W = 10  # the padlock in the narration header
LOCK_H = 12
PROGRESS_H = 30  # lane under the narration track: transcription arrow and its label
ARROW_H = 10
ARROW_HALF_W = 7
GAP = 3
HANDLE_HIT_PX = 7
PLAYHEAD_HIT_PX = 2  # the playhead wins over a crop handle this close to its line
SNAP_PX = 8
KEYPOINT_W = 1  # the green keypoint line: as thin as the red playhead
KEYPOINT_ARROW_HALF_W = 6  # the green arrow on the ruler: the size of the playhead's
KEYPOINT_ARROW_H = 9
KEYPOINT_HIT_PX = 5  # hover distance that shows the keypoint tooltip
THUMB_SPACING_PX = 160
FIT_BUTTON = 20  # the audio header's full screen / normal toggle (square, px)
FIT_TOOLTIP_ON = "Stretch the waveform so the loudest sound fills the track"
FIT_TOOLTIP_OFF = "Show the waveform at its real level"
TRANSCRIBE = "transcribe"  # the pen icon of the audio header
AUDIO_ICONS = (MICROPHONE, COMPUTER, TRANSCRIBE)  # left to right, after the fit toggle
AUDIO_ICON = 18  # px, square
AUDIO_ICON_GAP = 2
COLOR_TRACK_OFF = QColor("#e05050")  # the slash over a switched-off track
AUDIO_ICON_TOOLTIPS = {
    (MICROPHONE, True): "Microphone: in the audio track (click to leave it out)",
    (MICROPHONE, False): "Microphone: left out of the audio track (click to add it)",
    (COMPUTER, True): "Computer audio: in the audio track (click to leave it out)",
    (COMPUTER, False): "Computer audio: left out of the audio track (click to add it)",
    (COMPUTER, None): "No computer audio was recorded for this recording",
    (TRANSCRIBE, True): "Transcribe again: delete the narration and create it again "
    "from the current audio track",
}
MIN_TICK_PX = 70
TICK_STEPS_MS = (
    100, 200, 500, 1000, 2000, 5000, 10_000, 15_000, 30_000, 60_000, 120_000, 300_000,
    600_000, 900_000, 1_800_000, 3_600_000,
)  # fmt: skip

COLOR_BG = QColor("#232323")
COLOR_RULER = QColor("#303030")
COLOR_HEADER = QColor("#3a3a3a")
COLOR_TRACK = QColor("#2b2b2b")
COLOR_TEXT = QColor("#d8d8d8")
COLOR_TEXT_DIM = QColor("#9a9a9a")
COLOR_TICK = QColor("#6a6a6a")
COLOR_SELECTED = QColor("#4a90e2")
COLOR_WAVE = QColor("#5fb3a1")
COLOR_SHADE = QColor(0, 0, 0, 130)
COLOR_HANDLE = QColor("#f0c040")
COLOR_PLAYHEAD = QColor("#ff4a4a")
COLOR_PLAY_MARKER = QColor("#ffffff")
COLOR_KEYPOINT = QColor("#2fd36f")
COLOR_PAUSE = QColor("#9e9e9e")  # the gray line where the recording was paused
COLOR_TRANSCRIBING = QColor("#3ddc84")
COLOR_TRANSCRIBING_LIGHT = QColor("#178a45")  # the bright green fades on the light lane
COLOR_UNTRANSCRIBED = QColor(0, 0, 0, 90)  # audio whisper has not reached yet
COLOR_NARRATION = QColor("#4d5966")  # a box of the narration track (one per subtitle)
COLOR_NARRATION_EDGE = QColor("#6c7a88")
COLOR_MATCH = QColor("#f5cf1d")  # search hit: narration box, recordings list frame
COLOR_MATCH_EDGE = QColor("#b89600")
COLOR_MATCH_TEXT = QColor("#000000")

# Theme dependent colours (the dark set equals the module constants above).
DARK_COLORS = {
    "bg": COLOR_BG,
    "ruler": COLOR_RULER,
    "header": COLOR_HEADER,
    "track": COLOR_TRACK,
    "text": COLOR_TEXT,
    "text_dim": COLOR_TEXT_DIM,
    "tick": COLOR_TICK,
    "shade": COLOR_SHADE,
    "untranscribed": COLOR_UNTRANSCRIBED,
    "play_marker": COLOR_PLAY_MARKER,
    "transcribing": COLOR_TRANSCRIBING,
    "narration": COLOR_NARRATION,
    "narration_edge": COLOR_NARRATION_EDGE,
    "lock": COLOR_KEYPOINT,  # the open padlock
}
LIGHT_COLORS = {
    "bg": QColor("#e4e4e4"),
    "ruler": QColor("#d4d4d4"),
    "header": QColor("#c8c8c8"),
    "track": QColor("#f2f2f2"),
    "text": QColor("#202020"),
    "text_dim": QColor("#666666"),
    "tick": QColor("#8a8a8a"),
    "shade": QColor(0, 0, 0, 70),
    "untranscribed": QColor(0, 0, 0, 50),
    "play_marker": QColor("#505050"),  # a white arrow would vanish on the light ruler
    "transcribing": COLOR_TRANSCRIBING_LIGHT,
    "narration": QColor("#c3ccd5"),
    "narration_edge": QColor("#98a5b2"),
    "lock": QColor(PROCESS_GREEN),  # the bright green fades on the light header
}

TRANSCRIBING_LABEL = "transcribing"
TRANSCRIPTION_LABELS = {
    "transcribing": TRANSCRIBING_LABEL,
    "pending": "transcription pending",
    "failed": "transcription failed",
}


def format_eta(seconds: float | None) -> str:
    """``m:ss`` (``h:mm:ss`` from one hour) for an estimate; ``""`` when unknown."""
    if seconds is None or seconds < 0:
        return ""
    total = int(round(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def transcribing_label(eta_s: float | None) -> str:
    """The lane label: ``transcribing`` plus ``(ETA: m:ss)`` once an estimate exists."""
    eta = format_eta(eta_s)
    return f"{TRANSCRIBING_LABEL} (ETA: {eta})" if eta else TRANSCRIBING_LABEL


def tick_step_ms(zoom_px_per_s: float, min_px: int = MIN_TICK_PX) -> int:
    """Smallest 'nice' label interval that keeps labels at least ``min_px`` apart."""
    for step in TICK_STEPS_MS:
        if step * zoom_px_per_s / 1000.0 >= min_px:
            return step
    return TICK_STEPS_MS[-1]


def tick_label(ms: int, step_ms: int) -> str:
    hours, rem = divmod(ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    seconds, millis = divmod(rem, 1000)
    text = f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"
    if step_ms < 1000:
        text += f".{millis // 100}"
    return text


class TimelineView(QWidget):
    """Paints the timeline from a :class:`TimelineModel` and a :class:`LoadedRecording`.

    Signals report what the user changed; the window owns the model and reacts.
    """

    playhead_moved = Signal(int)
    playhead_drag_finished = Signal()  # the mouse button was released after a scrub
    crop_changed = Signal()
    screen_selected = Signal(str)
    view_changed = Signal()
    audio_track_toggled = Signal(str)  # "microphone" or "computer" was clicked
    transcribe_requested = Signal()  # the pen icon was clicked
    narration_lock_toggled = Signal(bool)  # the padlock was clicked: True = unlocked
    # A subtitle drag ended with a valid change: (old segments, new segments), tuples.
    segment_edit_requested = Signal(object, object)
    # The right mouse button on the narration: (track, time ms or None over the header,
    # the subtitle under the pointer or None, global position) - the window shows the menu.
    narration_menu_requested = Signal(int, object, object, QPoint)
    segment_double_clicked = Signal(object)  # a double click on a subtitle box: Edit it

    def __init__(self, model: TimelineModel, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.model = model
        self.recording: LoadedRecording | None = None
        self.thumbnails = ThumbnailCache(self)
        self.thumbnails.ready.connect(self.update)
        self.waveform = WaveformLoader(self)
        self.waveform.ready.connect(self._on_peaks_ready)
        self.scrollbar = QScrollBar(Qt.Orientation.Horizontal, self)
        self.scrollbar.valueChanged.connect(self._on_scrollbar)
        self._syncing_scrollbar = False
        self._drag: str | None = None
        self._drag_offset = 0.0  # px between the grabbed handle and the press position
        self._pan: tuple[float, float] | None = None  # middle button: (press x, scroll_ms)
        self._transcription: tuple[str, int] | None = None  # (state, transcribed_ms)
        self._eta_s: float | None = None  # estimated seconds until the transcription ends
        self._hover_keypoint: int | None = None  # keypoint under the mouse (tooltip shown)
        self._hover_pause: int | None = None  # pause (at_ms) under the mouse (tooltip shown)
        self._audio_fit = False  # the waveform is stretched to the loudest sound
        self._audio_peak: float | None = None  # loudest sample of the loaded audio (0..1)
        self._search = ""  # folded search phrase: matching narration boxes are yellow
        self._hover_segment: tuple[int, int] | None = None  # narration box under the mouse
        self._hover_icon: str | None = None  # audio header icon under the mouse
        self._narration_editable = False  # the padlock can be opened (transcript complete)
        self._narration_unlocked = False  # the subtitles can be dragged
        self._segment_drag: narration_edit.Drag | None = None
        self._segment_press_x = 0.0
        self._segment_proposal: narration_edit.Proposal | None = None
        # A click on a subtitle box moves the playhead only once no double click
        # followed (a double click opens Edit and leaves the playhead where it is).
        self._pending_click: QPoint | None = None
        self._click_timer = QTimer(self)
        self._click_timer.setSingleShot(True)
        self._click_timer.timeout.connect(self._apply_pending_click)
        self.colors = dict(DARK_COLORS)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMinimumHeight(RULER_H + TRACK_H + AUDIO_H + NARRATION_H + PROGRESS_H + 5 * GAP + 20)
        self.setMinimumWidth(HEADER_W + 200)

    # Public API -------------------------------------------------------------

    def set_recording(self, recording: LoadedRecording | None) -> None:
        self.recording = recording
        self._transcription = None
        self._eta_s = None
        self._hover_keypoint = None
        self._hover_pause = None
        self._hover_segment = None
        self._narration_unlocked = False  # every recording opens locked
        self._narration_editable = False
        self._end_segment_drag()
        self._cancel_pending_click()
        self.thumbnails.clear()
        self.model.set_duration(recording.duration_ms if recording else 0)
        self.model.set_screens(recording.screen_ids if recording else [])
        self.model.set_keypoints(recording.keypoints if recording else [])
        self.model.set_pauses(getattr(recording, "pauses", []) if recording else [])
        self.model.fit(self._body_width())
        self._audio_peak = None
        self.waveform.load(recording.audio if recording else None)
        # Every track must stay visible: the splitter may not shrink the timeline below it.
        self.setMinimumHeight(self.sizeHint().height())
        self.updateGeometry()
        self._after_view_change()

    def reload_audio(self) -> None:
        """The audio tracks switched on changed: draw the waveform of the new mix."""
        self._audio_peak = None
        self.waveform.load(self.recording.audio if self.recording else None)
        self.update()

    def set_transcription(
        self, state: str | None, transcribed_ms: int = 0, eta_s: float | None = None
    ) -> None:
        """What the lane under the audio track shows: ``None`` (complete: nothing),
        ``"transcribing"`` (green arrow at ``transcribed_ms``, ``eta_s`` seconds to go
        when known), ``"pending"`` (another recording is being transcribed first) or
        ``"failed"``."""
        value = None if state is None else (state, max(0, int(transcribed_ms)))
        eta = None if value is None else eta_s
        if value != self._transcription or eta != self._eta_s:
            self._transcription = value
            self._eta_s = eta
            self.update()

    @property
    def transcription(self) -> tuple[str, int] | None:
        return self._transcription

    # Narration lock -----------------------------------------------------------

    @property
    def narration_editable(self) -> bool:
        """The padlock can be opened (the transcript is complete, not archived)."""
        return self._narration_editable

    @property
    def narration_unlocked(self) -> bool:
        return self._narration_unlocked

    def set_narration_editable(self, editable: bool) -> None:
        """Whether the padlock can be opened; ``False`` also locks the tracks."""
        editable = bool(editable)
        if editable == self._narration_editable:
            return
        self._narration_editable = editable
        if not editable:
            self.set_narration_unlocked(False)
        self.update()

    def set_narration_unlocked(self, unlocked: bool) -> bool:
        """Open or close the padlock; returns True when the state changed. It opens
        only while :attr:`narration_editable`."""
        unlocked = bool(unlocked) and self._narration_editable
        if unlocked == self._narration_unlocked:
            return False
        self._narration_unlocked = unlocked
        if not unlocked:
            self._end_segment_drag()
        self.unsetCursor()
        self.update()
        self.narration_lock_toggled.emit(unlocked)
        return True

    def toggle_narration_lock(self) -> bool:
        return self.set_narration_unlocked(not self._narration_unlocked)

    # Subtitle drag ------------------------------------------------------------

    @property
    def segment_dragging(self) -> bool:
        return self._segment_drag is not None

    @property
    def segment_proposal(self) -> narration_edit.Proposal | None:
        """What the running subtitle drag would change (``None`` before the mouse moved)."""
        return self._segment_proposal

    def cancel_segment_drag(self) -> bool:
        """Escape: drop the running subtitle drag (nothing changes); True when one ran."""
        if self._segment_drag is None:
            return False
        self._end_segment_drag()
        self.update()
        return True

    def _end_segment_drag(self) -> None:
        self._segment_drag = None
        self._segment_proposal = None
        if self._drag == "segment":
            self._drag = None

    def set_search(self, query: str | None) -> None:
        """The search phrase: narration boxes whose text contains it are drawn yellow
        (case and diacritics do not matter); ``None`` or ``""`` clears it."""
        folded = normalize_query(query)
        if folded != self._search:
            self._search = folded
            self.update()

    @property
    def search(self) -> str:
        """The folded search phrase (``""`` without a search)."""
        return self._search

    def segment_matches(self, text: str) -> bool:
        """True when a narration box with ``text`` is highlighted by the search."""
        return text_matches(text, self._search)

    @property
    def eta_s(self) -> float | None:
        return self._eta_s

    @property
    def audio_fit(self) -> bool:
        return self._audio_fit

    def set_audio_fit(self, fit: bool) -> None:
        """Stretch the waveform to the loudest sound (``True``) or show the real level."""
        if fit != self._audio_fit:
            self._audio_fit = fit
            self.update()

    def toggle_audio_fit(self) -> None:
        self.set_audio_fit(not self._audio_fit)

    def waveform_gain(self) -> float:
        """Vertical scale of the waveform: 1, or 1 / loudest sample when fitted."""
        if not self._audio_fit or not self._audio_peak or self._audio_peak <= 0:
            return 1.0
        return 1.0 / self._audio_peak

    def _on_peaks_ready(self, peaks) -> None:
        self._audio_peak = float(abs(peaks).max()) if peaks is not None and len(peaks) else None
        self.update()

    def fit_button_rect(self) -> QRect | None:
        """The toggle in the bottom left corner of the audio header (``None`` without
        a waveform)."""
        peaks = self.waveform.peaks
        if peaks is None or len(peaks) == 0:
            return None
        audio = self.audio_rect()
        return QRect(audio.x() + 6, audio.bottom() - 5 - FIT_BUTTON, FIT_BUTTON, FIT_BUTTON)

    def audio_icon_rects(self) -> dict[str, QRect]:
        """The microphone, speaker and pen icons of the audio header (none without a
        recording that has audio, e.g. an archived one)."""
        recording = self.recording
        if recording is None or recording.archived or not recording.tracks:
            return {}
        audio = self.audio_rect()
        centre_y = audio.bottom() - 5 - FIT_BUTTON / 2
        x = audio.x() + 6 + FIT_BUTTON + AUDIO_ICON_GAP
        rects: dict[str, QRect] = {}
        for name in AUDIO_ICONS:
            rects[name] = QRect(x, int(centre_y - AUDIO_ICON / 2), AUDIO_ICON, AUDIO_ICON)
            x += AUDIO_ICON + AUDIO_ICON_GAP
        return rects

    def audio_icon_state(self, name: str) -> bool | None:
        """``True`` on, ``False`` off, ``None`` not available (no such track)."""
        recording = self.recording
        if recording is None:
            return None
        if name == TRANSCRIBE:
            return True
        if not recording.has_track(name):
            return None
        return recording.selection.enabled(name)

    def audio_icon_at(self, pos: QPoint) -> str | None:
        for name, rect in self.audio_icon_rects().items():
            if rect.adjusted(-1, -1, 1, 1).contains(pos):
                return name
        return None

    def audio_icon_tooltip(self, name: str) -> str:
        return AUDIO_ICON_TOOLTIPS[(name, self.audio_icon_state(name))]

    def click_audio_icon(self, name: str) -> None:
        """What a click on an icon does (emits the request; unavailable icons do nothing)."""
        if self.audio_icon_state(name) is None:
            return
        if name == TRANSCRIBE:
            self.transcribe_requested.emit()
        else:
            self.audio_track_toggled.emit(name)

    def _on_fit_button(self, pos: QPoint) -> QRect | None:
        fit = self.fit_button_rect()
        if fit is not None and fit.adjusted(-2, -2, 2, 2).contains(pos):
            return fit
        return None

    def set_theme(self, dark: bool) -> None:
        """Dark (the default) or light colours for the ruler, headers and tracks."""
        self.colors = dict(DARK_COLORS if dark else LIGHT_COLORS)
        self.update()

    def sizeHint(self) -> QSize:  # noqa: N802
        tracks = max(1, len(self.model.screens))
        height = (
            RULER_H + GAP + tracks * (TRACK_H + GAP) + AUDIO_H + GAP + NARRATION_H + GAP
            + PROGRESS_H + GAP
        )  # fmt: skip
        return QSize(900, height + self.scrollbar.sizeHint().height())

    def zoom_in(self) -> None:
        self._zoom_toward_playhead(1.5)

    def zoom_out(self) -> None:
        self._zoom_toward_playhead(1 / 1.5)

    def fit(self) -> None:
        self.model.fit(self._body_width())
        self._after_view_change()

    def set_zoom_fraction(self, fraction: float) -> None:
        """The zoom slider: like the buttons it keeps the playhead centred."""
        self.model.set_zoom_fraction(fraction, center_ms=self.model.playhead_ms)
        self._after_view_change()

    def ensure_playhead_visible(self) -> None:
        if self.model.ensure_visible(self.model.playhead_ms):
            self._after_view_change()
        else:
            self.update()

    def shutdown(self) -> None:
        self.thumbnails.shutdown()
        self.waveform.wait()

    # Geometry ---------------------------------------------------------------

    def _body_width(self) -> int:
        return max(1, self.width() - HEADER_W)

    def _canvas_height(self) -> int:
        return max(0, self.height() - self.scrollbar.sizeHint().height())

    def _x(self, time_ms: float) -> float:
        return HEADER_W + self.model.time_to_x(time_ms)

    def _time(self, x: float) -> int:
        return self.model.clamp_time(self.model.x_to_time(x - HEADER_W))

    def _tracks_top(self) -> int:
        return RULER_H + GAP

    def track_rects(self) -> list[tuple[str, QRect]]:
        y = self._tracks_top()
        rects = []
        for screen_id in self.model.screens:
            rects.append((screen_id, QRect(0, y, self.width(), TRACK_H)))
            y += TRACK_H + GAP
        return rects

    def audio_rect(self) -> QRect:
        y = self._tracks_top() + len(self.model.screens) * (TRACK_H + GAP)
        return QRect(0, y, self.width(), AUDIO_H)

    def narration_rect(self) -> QRect:
        """Both narration tracks under the audio track."""
        audio = self.audio_rect()
        return QRect(0, audio.bottom() + 1 + GAP, self.width(), NARRATION_H)

    def narration_lane_rect(self, track: int) -> QRect:
        """One narration track: 0 the upper, 1 the lower (a 1 px line between them)."""
        rect = self.narration_rect()
        top = rect.y() + track * (NARRATION_LANE_H + 1)
        return QRect(0, top, self.width(), NARRATION_LANE_H)

    def narration_lane_at(self, pos: QPoint) -> int | None:
        """The narration track under ``pos`` (``None`` outside the narration)."""
        if not self.narration_rect().contains(pos):
            return None
        return 1 if pos.y() >= self.narration_lane_rect(1).y() - 1 else 0

    def tracks_bottom(self) -> int:
        """Last pixel row of the tracks (the narration track): playhead, keypoints and
        the crop reach down to here."""
        return self.narration_rect().bottom()

    def progress_rect(self) -> QRect:
        """The lane under the narration track that holds the transcription arrow."""
        narration = self.narration_rect()
        return QRect(0, narration.bottom() + 1 + GAP, self.width(), PROGRESS_H)

    def lock_rect(self) -> QRect:
        """The padlock in the narration header: left, in the row of the lower track
        (under the "narration" title)."""
        lane = self.narration_lane_rect(1)
        return QRect(6, lane.y() + (lane.height() - LOCK_H) // 2, LOCK_W, LOCK_H)

    def _on_lock(self, pos: QPoint) -> bool:
        return self.lock_rect().adjusted(-4, -4, 4, 4).contains(pos)

    def segment_box(self, segment) -> QRectF:
        """Where ``segment`` is drawn on its track: whole pixels, a 1 px gap after it so
        that touching boxes stay apart."""
        lane = self.narration_lane_rect(segment.track)
        left = round(self._x(segment.start_ms))
        right = round(self._x(segment.end_ms))
        width = max(2, right - left - 1)
        return QRectF(left, lane.y() + 3, width, lane.height() - 6)

    def narration_boxes(self, track: int | None = None) -> list[tuple[QRectF, object]]:
        """The visible narration boxes: ``(rect, segment)`` for every subtitle with text,
        of one ``track`` or of both (upper first)."""
        if self.recording is None:
            return []
        start = self.model.scroll_ms
        end = self.model.x_to_time(self._body_width())
        tracks = range(NARRATION_TRACKS) if track is None else (track,)
        boxes: list[tuple[QRectF, object]] = []
        for lane in tracks:
            for segment in self.recording.segments_between(start, end, lane):
                if not segment.text.strip():
                    continue  # no words, no box
                boxes.append((self.segment_box(segment), segment))
        return boxes

    def narration_segment_at(self, pos: QPoint):
        """The narration segment whose box is under ``pos`` (``None`` elsewhere)."""
        lane = self.narration_lane_at(pos)
        if pos.x() < HEADER_W or lane is None:
            return None
        for rect, segment in self.narration_boxes(lane):
            if rect.left() - 1 <= pos.x() <= rect.right() + 1:
                return segment
        return None

    def _px_per_ms(self) -> float:
        return self.model.zoom / 1000.0

    def _exact_time(self, x: float) -> float:
        """The time under ``x`` without rounding (zones are measured in pixels)."""
        return self.model.scroll_ms + (x - HEADER_W) / self._px_per_ms()

    def segment_hit(self, pos: QPoint) -> narration_edit.Hit:
        """The subtitle zone under ``pos``; nothing while the tracks are locked."""
        lane = self.narration_lane_at(pos)
        if (
            not self._narration_unlocked
            or self.recording is None
            or lane is None
            or pos.x() < HEADER_W
        ):
            return narration_edit.Hit(Zone.NONE)
        return narration_edit.hit_test(
            self.recording.segments, lane, self._exact_time(pos.x()), self._px_per_ms()
        )

    def narration_shows_text(self) -> bool:
        """Zoomed in far enough for the boxes to show their words."""
        return self.model.zoom >= NARRATION_TEXT_MIN_ZOOM

    def transcription_arrow_x(self) -> float | None:
        """Where the transcription arrow is drawn (``None`` when nothing is shown)."""
        if self._transcription is None:
            return None
        state, reached = self._transcription
        if state != "transcribing":
            return None
        return min(max(self._x(reached), float(HEADER_W)), float(self.width() - 1))

    def track_at(self, pos: QPoint) -> str | None:
        for screen_id, rect in self.track_rects():
            if rect.contains(pos):
                return screen_id
        return None

    # Scrollbar --------------------------------------------------------------

    def _sync_scrollbar(self) -> None:
        self._syncing_scrollbar = True
        try:
            self.scrollbar.setRange(0, self.model.max_scroll_ms())
            self.scrollbar.setPageStep(int(self.model.visible_ms()))
            self.scrollbar.setSingleStep(max(1, int(self.model.visible_ms() / 20)))
            self.scrollbar.setValue(self.model.scroll_ms)
        finally:
            self._syncing_scrollbar = False

    def _on_scrollbar(self, value: int) -> None:
        if self._syncing_scrollbar:
            return
        self.model.set_scroll(value)
        self.view_changed.emit()
        self.update()

    def _after_view_change(self) -> None:
        self._sync_scrollbar()
        self.view_changed.emit()
        self.update()

    def _zoom_toward_playhead(self, factor: float) -> None:
        """The +/- buttons and Ctrl+/-: zoom with the red playhead line in the middle of
        the view (as far as the ends of the recording allow)."""
        self.model.zoom_centered(factor, self.model.playhead_ms)
        self._after_view_change()

    def _snap_tolerance_ms(self) -> float:
        return SNAP_PX * 1000.0 / self.model.zoom

    def keypoint_at(self, pos: QPoint) -> int | None:
        """The keypoint whose line is under ``pos`` (within ``KEYPOINT_HIT_PX``)."""
        inside = 0 <= pos.y() <= self.tracks_bottom()  # its arrow on the ruler too
        if pos.x() < HEADER_W or not inside:
            return None
        best: int | None = None
        best_d = float(KEYPOINT_HIT_PX) + 0.5
        for keypoint in self.model.keypoints:
            d = abs(pos.x() - self._x(keypoint))
            if d < best_d:
                best, best_d = keypoint, d
        return best

    @staticmethod
    def keypoint_tooltip(index: int, time_ms: int) -> str:
        return f"Keypoint {index}  ({format_timestamp(time_ms)})"

    def pause_at(self, pos: QPoint) -> int | None:
        """The pause (its ``at_ms``) whose gray line is under ``pos``; keypoints win."""
        inside = 0 <= pos.y() <= self.tracks_bottom()
        if pos.x() < HEADER_W or not inside:
            return None
        best: int | None = None
        best_d = float(KEYPOINT_HIT_PX) + 0.5
        for at_ms, _length in self.model.pauses:
            d = abs(pos.x() - self._x(at_ms))
            if d < best_d:
                best, best_d = at_ms, d
        return best

    @staticmethod
    def pause_tooltip(at_ms: int, duration_ms: int | None) -> str:
        """``Paused 42 s  (00:01:05.000)``: how long the recording was paused there."""
        if duration_ms is None:
            length = ""
        else:
            seconds = max(0, round(duration_ms / 1000))
            length = (
                f" {seconds} s" if seconds < 60 else f" {seconds // 60} min {seconds % 60:02d} s"
            )
        return f"Paused{length}  ({format_timestamp(at_ms)})"

    # Events -----------------------------------------------------------------

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802
        super().resizeEvent(event)
        sb_height = self.scrollbar.sizeHint().height()
        self.scrollbar.setGeometry(0, self.height() - sb_height, self.width(), sb_height)
        self.model.set_viewport_width(self._body_width())
        self._after_view_change()

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802
        delta = event.angleDelta().y() or event.angleDelta().x()
        if delta == 0:
            return
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            x = event.position().x() - HEADER_W
            self.model.zoom_around(max(0.0, x), 1.25 ** (delta / 120.0))
        else:
            self.model.set_scroll(self.model.scroll_ms - delta * self.model.visible_ms() / 1200.0)
        self._after_view_change()
        event.accept()

    def _handle_at(self, pos: QPoint) -> str | None:
        if pos.y() < self._tracks_top() or pos.x() < HEADER_W:
            return None
        d_in = abs(pos.x() - self._x(self.model.in_ms))
        d_out = abs(pos.x() - self._x(self.model.out_ms))
        if min(d_in, d_out) > HANDLE_HIT_PX:
            return None
        return "in" if d_in <= d_out else "out"

    @property
    def panning(self) -> bool:
        """The middle mouse button holds the timeline."""
        return self._pan is not None

    def _start_pan(self, x: float) -> None:
        self._pan = (x, float(self.model.scroll_ms))
        self.setCursor(Qt.CursorShape.ClosedHandCursor)

    def _pan_to(self, x: float) -> None:
        """The grabbed moment stays under the mouse: moving right shows earlier times."""
        if self._pan is None or self.model.zoom <= 0:
            return
        start_x, start_scroll = self._pan
        scroll = start_scroll - (x - start_x) * 1000.0 / self.model.zoom
        if int(round(scroll)) != self.model.scroll_ms:
            self.model.set_scroll(scroll)
            self._after_view_change()

    def _end_pan(self) -> None:
        self._pan = None
        self.setCursor(Qt.CursorShape.ArrowCursor)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.MiddleButton:
            if self._drag is None and event.position().y() < self._canvas_height():
                self._start_pan(event.position().x())
                event.accept()
            return
        if event.button() != Qt.MouseButton.LeftButton:
            return
        pos = event.position().toPoint()
        if pos.y() >= self._canvas_height():
            return
        fit = self._on_fit_button(pos)
        if fit is not None:
            self.toggle_audio_fit()
            self._show_fit_tooltip(event.globalPosition().toPoint(), fit)
            return
        icon = self.audio_icon_at(pos)
        if icon is not None:
            self.click_audio_icon(icon)
            return
        self.setFocus()
        if self._on_lock(pos):
            if self._narration_editable:
                self.toggle_narration_lock()
                self._show_lock_tooltip(event.globalPosition().toPoint())
            return
        handle = self._handle_at(pos)
        on_playhead = (
            pos.x() >= HEADER_W
            and abs(pos.x() - self._x(self.model.playhead_ms)) <= PLAYHEAD_HIT_PX
        )
        if handle is not None and not on_playhead:
            # Grab the handle where it is: the crop only changes once the mouse moves,
            # by the distance moved (not by the press offset).
            self._drag = handle
            handle_ms = self.model.in_ms if handle == "in" else self.model.out_ms
            self._drag_offset = pos.x() - self._x(handle_ms)
            return
        hit = self.segment_hit(pos)
        if hit.editable and self.recording is not None:
            self._cancel_pending_click()
            self._start_segment_drag(hit, pos.x())
            return
        if pos.x() >= HEADER_W and self.narration_segment_at(pos) is not None:
            # Maybe the first click of a double click: the playhead waits (see
            # _apply_pending_click); moving the mouse scrubs as usual.
            self._click_timer.stop()
            self._pending_click = QPoint(pos)
            self._drag = "pending"
            return
        self._cancel_pending_click()
        screen_id = self.track_at(pos)
        if screen_id is not None and self.model.select_screen(screen_id):
            self.screen_selected.emit(screen_id)
        if pos.x() >= HEADER_W:
            self._drag = "playhead"
            self._move_playhead_to(pos.x())
        self.update()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        pos = event.position().toPoint()
        if self._pan is not None:
            self._pan_to(event.position().x())
            return
        if self._drag == "segment":
            self._drag_segment_to(pos)
        elif self._drag in ("in", "out"):
            self._drag_handle_to(pos.x())
        elif self._drag == "playhead":
            self._move_playhead_to(pos.x())
        elif self._drag == "pending":
            press = self._pending_click
            if press is None or (pos - press).manhattanLength() >= QApplication.startDragDistance():
                self._pending_click = None
                self._drag = "playhead"
                self._move_playhead_to(pos.x())
        else:
            fit = self._on_fit_button(pos)
            if fit is not None:
                self.setCursor(Qt.CursorShape.PointingHandCursor)
                self._show_fit_tooltip(event.globalPosition().toPoint(), fit)
                return
            icon = self.audio_icon_at(pos)
            if icon is not None:
                available = self.audio_icon_state(icon) is not None
                self.setCursor(
                    Qt.CursorShape.PointingHandCursor if available else Qt.CursorShape.ArrowCursor
                )
                rect = self.audio_icon_rects()[icon]
                QToolTip.showText(
                    event.globalPosition().toPoint(),
                    self.audio_icon_tooltip(icon),
                    self,
                    rect.adjusted(-1, -1, 1, 1),
                )
                self._hover_icon = icon
                return
            if self._hover_icon is not None:
                self._hover_icon = None
                QToolTip.hideText()
            near_handle = self._handle_at(pos) is not None
            cursor = Qt.CursorShape.SizeHorCursor if near_handle else Qt.CursorShape.ArrowCursor
            if not near_handle:
                if self._on_lock(pos) and self._narration_editable:
                    cursor = Qt.CursorShape.PointingHandCursor
                else:
                    cursor = ZONE_CURSORS.get(self.segment_hit(pos).zone, cursor)
            self.setCursor(cursor)
            self._update_keypoint_tooltip(pos, event.globalPosition().toPoint())

    def _show_fit_tooltip(self, global_pos: QPoint, rect: QRect) -> None:
        text = FIT_TOOLTIP_OFF if self._audio_fit else FIT_TOOLTIP_ON
        QToolTip.showText(global_pos, text, self, rect.adjusted(-2, -2, 2, 2))

    def _update_keypoint_tooltip(self, pos: QPoint, global_pos: QPoint) -> None:
        keypoint = self.keypoint_at(pos)
        pause = None if keypoint is not None else self.pause_at(pos)
        if keypoint == self._hover_keypoint and pause == self._hover_pause:
            if keypoint is None and pause is None:
                self._update_narration_tooltip(pos, global_pos)
            return
        self._hover_keypoint = keypoint
        self._hover_pause = pause
        self.update()  # the hovered line is drawn bolder
        if keypoint is None and pause is None:
            QToolTip.hideText()
            self._update_narration_tooltip(pos, global_pos)
            return
        self._hover_segment = None
        if keypoint is not None:
            index = self.model.keypoints.index(keypoint) + 1
            QToolTip.showText(global_pos, self.keypoint_tooltip(index, keypoint), self)
            return
        length = dict(self.model.pauses).get(pause)
        QToolTip.showText(global_pos, self.pause_tooltip(pause, length), self)

    def lock_tooltip(self) -> str:
        if not self._narration_editable:
            return LOCK_TOOLTIP_UNAVAILABLE
        return LOCK_TOOLTIP_UNLOCKED if self._narration_unlocked else LOCK_TOOLTIP_LOCKED

    def _show_lock_tooltip(self, global_pos: QPoint) -> None:
        self._hover_segment = (-2, int(self._narration_unlocked))
        QToolTip.showText(global_pos, self.lock_tooltip(), self, self.lock_rect())

    def _update_narration_tooltip(self, pos: QPoint, global_pos: QPoint) -> None:
        """Hovering a narration box shows its words; the header explains the tracks and
        the padlock its state."""
        if self._segment_drag is not None:
            return
        on_lock = self._on_lock(pos)
        segment = None if on_lock else self.narration_segment_at(pos)
        on_header = pos.x() < HEADER_W and self.narration_rect().contains(pos)
        key = (segment.start_ms, segment.end_ms, segment.track) if segment is not None else None
        if on_header:
            key = (-1, -1)
        if on_lock:
            key = (-2, int(self._narration_unlocked))
        if key == self._hover_segment:
            return
        self._hover_segment = key
        if on_lock:
            self._show_lock_tooltip(global_pos)
        elif on_header:
            QToolTip.showText(global_pos, NARRATION_TOOLTIP, self)
        elif segment is not None:
            text = f"{format_timestamp(segment.start_ms)}  {segment.text.strip()}"
            QToolTip.showText(global_pos, text, self)
        else:
            QToolTip.hideText()

    def leaveEvent(self, event) -> None:  # noqa: N802
        if (
            self._hover_keypoint is not None
            or self._hover_pause is not None
            or self._hover_segment is not None
            or self._hover_icon is not None
        ):
            self._hover_keypoint = None
            self._hover_pause = None
            self._hover_segment = None
            self._hover_icon = None
            QToolTip.hideText()
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.MiddleButton and self._pan is not None:
            self._pan_to(event.position().x())
            self._end_pan()
            return
        if event.button() == Qt.MouseButton.LeftButton:
            drag, self._drag = self._drag, None
            if drag == "playhead":
                self.playhead_drag_finished.emit()
            elif drag == "segment":
                self._finish_segment_drag()
            elif drag == "pending" and self._pending_click is not None:
                self._click_timer.start(QApplication.doubleClickInterval())

    @property
    def click_pending(self) -> bool:
        """A click on a subtitle waits for a possible second click."""
        return self._pending_click is not None

    def _apply_pending_click(self) -> None:
        """No double click followed the click on a subtitle: move the playhead there
        as a plain click would have."""
        press, self._pending_click = self._pending_click, None
        if press is None or self.recording is None:
            return
        self._move_playhead_to(press.x())
        self.playhead_drag_finished.emit()

    def _cancel_pending_click(self) -> None:
        self._click_timer.stop()
        self._pending_click = None
        if self._drag == "pending":
            self._drag = None

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        """A double click on a subtitle box opens Edit for that subtitle only; the
        playhead does not move. Elsewhere it is a press (Qt's default)."""
        pos = event.position().toPoint()
        if event.button() == Qt.MouseButton.LeftButton and pos.y() < self._canvas_height():
            segment = None if self._on_lock(pos) else self.narration_segment_at(pos)
            if segment is not None:
                self._cancel_pending_click()
                if self._drag == "segment":
                    self._end_segment_drag()
                self._drag = None
                event.accept()
                self.segment_double_clicked.emit(segment)
                return
        super().mouseDoubleClickEvent(event)

    def contextMenuEvent(self, event: QContextMenuEvent) -> None:  # noqa: N802
        """The right mouse button on the narration tracks (or their header): the
        window shows the narration menu (Unlock / Add / Delete / Lock)."""
        pos = event.pos()
        busy = self._drag is not None or self._pan is not None
        if busy or pos.y() >= self._canvas_height() or not self.narration_rect().contains(pos):
            event.ignore()
            return
        lane = self.narration_lane_at(pos)
        if lane is None:
            event.ignore()
            return
        if pos.x() < HEADER_W:
            time_ms, segment = None, None
        else:
            time_ms = self._time(pos.x())
            segment = self.narration_segment_at(pos)
        event.accept()
        self.narration_menu_requested.emit(lane, time_ms, segment, event.globalPos())

    def focusOutEvent(self, event) -> None:  # noqa: N802
        self.cancel_segment_drag()  # a drag never survives losing the keyboard
        super().focusOutEvent(event)

    def _start_segment_drag(self, hit: narration_edit.Hit, x: float) -> None:
        recording = self.recording
        assert recording is not None
        magnets = narration_edit.Magnets.of(
            self.model.keypoints,
            [at for at, _length in self.model.pauses],
            self.model.playhead_ms,
        )
        self._segment_drag = narration_edit.Drag(
            hit, recording.segments, recording.duration_ms, magnets
        )
        self._segment_press_x = x
        self._segment_proposal = None
        self._drag = "segment"
        QToolTip.hideText()
        self.update()

    def _drag_segment_to(self, pos: QPoint) -> None:
        drag = self._segment_drag
        if drag is None:
            return
        delta_ms = (pos.x() - self._segment_press_x) / self._px_per_ms()
        lane = 1 if pos.y() >= self.narration_lane_rect(1).y() - 1 else 0
        snap_ms = SNAP_PX / self._px_per_ms()
        self._segment_proposal = drag.propose(delta_ms, lane, snap_ms)
        self.update()

    def _finish_segment_drag(self) -> None:
        proposal = self._segment_proposal
        self._segment_drag = None
        self._segment_proposal = None
        self.update()
        if proposal is not None and proposal.valid and proposal.changed:
            self.segment_edit_requested.emit(proposal.old, proposal.new)

    def _move_playhead_to(self, x: float) -> None:
        time_ms = self.model.snap_to_keypoint(self._time(x), self._snap_tolerance_ms())
        if self.model.set_playhead(time_ms):
            self.playhead_moved.emit(self.model.playhead_ms)
        self.update()

    def _drag_handle_to(self, x: float) -> None:
        x -= self._drag_offset
        time_ms = self._time(x)
        if abs(x - self._x(self.model.playhead_ms)) <= SNAP_PX:
            time_ms = self.model.playhead_ms
        else:
            time_ms = self.model.snap_to_keypoint(time_ms, self._snap_tolerance_ms())  # magnet
        changed = self.model.set_in(time_ms) if self._drag == "in" else self.model.set_out(time_ms)
        if changed:
            self.crop_changed.emit()
        self.update()

    # Painting ---------------------------------------------------------------

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        canvas = QRect(0, 0, self.width(), self._canvas_height())
        painter.fillRect(canvas, self.colors["bg"])
        self._paint_ruler(painter)
        self.thumbnails.begin_paint()  # only the thumbnails requested below get decoded
        for screen_id, rect in self.track_rects():
            self._paint_video_track(painter, screen_id, rect)
        self._paint_audio_track(painter, self.audio_rect())
        self._paint_audio_icons(painter)
        self._paint_narration_track(painter, self.narration_rect())
        self._paint_crop(painter, canvas)
        self._paint_pauses(painter)
        self._paint_keypoints(painter)
        self._paint_transcription(painter)
        self._paint_play_marker(painter)
        self._paint_playhead(painter, canvas)
        painter.end()

    def _paint_ruler(self, painter: QPainter) -> None:
        rect = QRect(0, 0, self.width(), RULER_H)
        painter.fillRect(rect, self.colors["ruler"])
        painter.setClipRect(QRect(HEADER_W, 0, self._body_width(), RULER_H))
        step = tick_step_ms(self.model.zoom)
        minor = step / 5.0
        first = int(self.model.scroll_ms // step) * step
        end = self.model.x_to_time(self._body_width()) + step
        font = QFont(painter.font())
        font.setPointSizeF(max(7.0, font.pointSizeF() - 1))
        painter.setFont(font)
        t = first
        while t <= end and t <= self.model.duration_ms:
            x = int(self._x(t))
            painter.setPen(QPen(self.colors["tick"]))
            painter.drawLine(x, RULER_H - 10, x, RULER_H)
            for k in range(1, 5):
                mx = int(self._x(t + k * minor))
                painter.drawLine(mx, RULER_H - 4, mx, RULER_H)
            painter.setPen(QPen(self.colors["text"]))
            painter.drawText(x + 3, RULER_H - 12, tick_label(int(t), step))
            t += step
        painter.setClipping(False)
        painter.setPen(QPen(self.colors["tick"]))
        painter.drawLine(0, RULER_H - 1, self.width(), RULER_H - 1)

    def _paint_header(self, painter: QPainter, rect: QRect, title: str, subtitle: str) -> None:
        header = QRect(rect.x(), rect.y(), HEADER_W, rect.height())
        painter.fillRect(header, self.colors["header"])
        painter.setPen(QPen(self.colors["text"]))
        painter.drawText(
            header.adjusted(6, 4, -4, 0),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
            title,
        )
        if subtitle:
            painter.setPen(QPen(self.colors["text_dim"]))
            painter.drawText(
                header.adjusted(6, 0, -4, -4),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom,
                subtitle,
            )

    def _paint_video_track(self, painter: QPainter, screen_id: str, rect: QRect) -> None:
        body = QRect(HEADER_W, rect.y(), self._body_width(), rect.height())
        painter.fillRect(body, self.colors["track"])
        size = self.recording.screen_size(screen_id) if self.recording else (0, 0)
        subtitle = f"{size[0]}x{size[1]}" if size[0] else ""
        self._paint_header(painter, rect, screen_id, subtitle)
        if self.recording is not None and self.recording.frame_times(screen_id):
            painter.setClipRect(body)
            self._paint_thumbnails(painter, screen_id, body)
            painter.setClipping(False)
        if screen_id == self.model.selected_screen:
            pen = QPen(COLOR_SELECTED, 2)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(rect.adjusted(1, 1, -1, -1))

    def _paint_thumbnails(self, painter: QPainter, screen_id: str, body: QRect) -> None:
        assert self.recording is not None
        spacing_ms = THUMB_SPACING_PX * 1000.0 / self.model.zoom
        height = body.height() - 4
        first = int(self.model.scroll_ms // spacing_ms)
        last = int(self.model.x_to_time(body.width()) // spacing_ms) + 1
        for k in range(first, last + 1):
            t = int(k * spacing_ms)
            if t > self.model.duration_ms:
                break
            ref = self.recording.frame_at(screen_id, t)
            if ref is None or ref.image is None:
                continue
            x = int(self._x(t))
            image = self.thumbnails.get(ref.image, height)
            if image is None:
                painter.setPen(QPen(self.colors["tick"]))
                painter.drawRect(x, body.y() + 2, int(height * 1.6), height)
                continue
            painter.drawImage(QPoint(x, body.y() + 2), image)

    def _paint_audio_track(self, painter: QPainter, rect: QRect) -> None:
        body = QRect(HEADER_W, rect.y(), self._body_width(), rect.height())
        painter.fillRect(body, self.colors["track"])
        peaks = self.waveform.peaks
        subtitle = "" if peaks is not None else ("loading" if self.waveform.busy else "")
        self._paint_header(painter, rect, "audio", subtitle)
        if peaks is None or len(peaks) == 0:
            if self.recording is not None and self.recording.archived:
                # Archived: the audio is gone; a plain line where the waveform was.
                mid = int(body.y() + body.height() / 2.0)
                painter.setPen(QPen(self.colors["tick"]))
                painter.drawLine(body.x(), mid, body.right(), mid)
            return
        columns = body.width()
        ms_per_px = 1000.0 / self.model.zoom
        reduced = column_peaks(peaks, BLOCK_MS, self.model.scroll_ms, ms_per_px, columns)
        mid = body.y() + body.height() / 2.0
        half = body.height() / 2.0 - 2
        gain = self.waveform_gain()
        painter.setPen(QPen(COLOR_WAVE))
        for col in range(columns):
            lo, hi = float(reduced[col, 0]), float(reduced[col, 1])
            if lo == 0.0 and hi == 0.0:
                continue
            lo, hi = max(-1.0, lo * gain), min(1.0, hi * gain)
            x = body.x() + col
            painter.drawLine(QPointF(x, mid - hi * half), QPointF(x, mid - lo * half))
        painter.setPen(QPen(self.colors["tick"]))
        painter.drawLine(body.x(), int(mid), body.right(), int(mid))
        self._paint_fit_button(painter)

    def _paint_audio_icons(self, painter: QPainter) -> None:
        """Microphone, speaker (crossed when switched off) and the pen."""
        rects = self.audio_icon_rects()
        if not rects:
            return
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        for name, rect in rects.items():
            state = self.audio_icon_state(name)
            color = self.colors["text"] if state else self.colors["text_dim"]
            r = QRectF(rect).adjusted(2.0, 2.0, -2.0, -2.0)
            pen = QPen(color, 1.5)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            if name == MICROPHONE:
                self._paint_microphone(painter, r, color)
            elif name == COMPUTER:
                self._paint_speaker(painter, r, color)
            else:
                self._paint_pen(painter, r, color)
            if name != TRANSCRIBE and not state:
                slash = QPen(COLOR_TRACK_OFF if state is False else color, 1.8)
                slash.setCapStyle(Qt.PenCapStyle.RoundCap)
                painter.setPen(slash)
                painter.drawLine(r.topLeft(), r.bottomRight())
        painter.restore()

    @staticmethod
    def _paint_microphone(painter: QPainter, r: QRectF, color: QColor) -> None:
        w, h = r.width(), r.height()
        capsule = QRectF(r.x() + w * 0.34, r.y(), w * 0.32, h * 0.58)
        painter.setBrush(QBrush(color))
        painter.drawRoundedRect(capsule, w * 0.16, w * 0.16)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        holder = QRectF(r.x() + w * 0.18, r.y() + h * 0.12, w * 0.64, h * 0.6)
        painter.drawArc(holder, 180 * 16, 180 * 16)  # the lower half: a U around it
        painter.drawLine(QPointF(r.center().x(), holder.bottom()),
                         QPointF(r.center().x(), r.bottom()))  # fmt: skip
        painter.drawLine(QPointF(r.x() + w * 0.3, r.bottom()),
                         QPointF(r.x() + w * 0.7, r.bottom()))  # fmt: skip

    @staticmethod
    def _paint_speaker(painter: QPainter, r: QRectF, color: QColor) -> None:
        w, h = r.width(), r.height()
        cone = QPolygonF(
            [
                QPointF(r.x(), r.y() + h * 0.36),
                QPointF(r.x() + w * 0.22, r.y() + h * 0.36),
                QPointF(r.x() + w * 0.5, r.y() + h * 0.1),
                QPointF(r.x() + w * 0.5, r.y() + h * 0.9),
                QPointF(r.x() + w * 0.22, r.y() + h * 0.64),
                QPointF(r.x(), r.y() + h * 0.64),
            ]
        )
        painter.setBrush(QBrush(color))
        painter.drawPolygon(cone)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        centre = QPointF(r.x() + w * 0.5, r.center().y())
        for radius in (w * 0.2, w * 0.4):
            arc = QRectF(centre.x() - radius, centre.y() - radius, 2 * radius, 2 * radius)
            painter.drawArc(arc, -45 * 16, 90 * 16)

    @staticmethod
    def _paint_pen(painter: QPainter, r: QRectF, color: QColor) -> None:
        """A sheet of paper with lines of text and a pen writing the last one."""
        w, h = r.width(), r.height()
        painter.drawRect(QRectF(r.x(), r.y() + h * 0.08, w * 0.62, h * 0.9))
        for i, length in enumerate((0.42, 0.42, 0.22)):
            y = r.y() + h * (0.32 + i * 0.2)
            painter.drawLine(QPointF(r.x() + w * 0.1, y), QPointF(r.x() + w * (0.1 + length), y))
        nib = QPointF(r.x() + w * 0.42, r.y() + h * 0.8)
        top = QPointF(r.right(), r.y())
        body = QPainterPath()
        body.moveTo(nib)
        body.lineTo(QPointF(top.x() - w * 0.14, top.y()))
        body.lineTo(QPointF(top.x(), top.y() + h * 0.14))
        body.closeSubpath()
        painter.setBrush(QBrush(color))
        painter.drawPath(body)

    def _paint_narration_track(self, painter: QPainter, rect: QRect) -> None:
        """Both narration tracks: a box wherever a subtitle has words (none in the
        pauses), yellow when it contains the search phrase; zoomed in far enough, each
        box shows its words, cut with an ellipsis where they do not fit. Over them the
        preview of a running drag."""
        for track in range(NARRATION_TRACKS):
            lane = self.narration_lane_rect(track)
            painter.fillRect(
                QRect(HEADER_W, lane.y(), self._body_width(), lane.height()),
                self.colors["track"],
            )
        header = QRect(rect.x(), rect.y(), HEADER_W, rect.height())
        painter.fillRect(header, self.colors["header"])
        painter.setPen(QPen(self.colors["text"]))
        upper = self.narration_lane_rect(0)
        title_rect = QRect(6, upper.y(), HEADER_W - 12, upper.height())
        title = painter.fontMetrics().elidedText(
            NARRATION_TITLE, Qt.TextElideMode.ElideRight, title_rect.width()
        )
        painter.drawText(
            title_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, title
        )
        self._paint_lock(painter, self.lock_rect())
        body = QRect(HEADER_W, rect.y(), self._body_width(), rect.height())
        boxes = self.narration_boxes()
        proposal = self._segment_proposal
        if not boxes and proposal is None:
            return
        painter.save()
        painter.setClipRect(body)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        font = QFont(painter.font())
        font.setPointSizeF(max(7.0, font.pointSizeF() - 1))
        painter.setFont(font)
        show_text = self.narration_shows_text()
        for box, segment in boxes:
            hit = self.segment_matches(segment.text)
            fill = COLOR_MATCH if hit else self.colors["narration"]
            edge = COLOR_MATCH_EDGE if hit else self.colors["narration_edge"]
            text_color = COLOR_MATCH_TEXT if hit else self.colors["text"]
            self._paint_box(painter, box, fill, edge)
            if show_text:
                self._paint_box_text(painter, box, segment.text, text_color)
        if proposal is not None:
            fill = COLOR_PREVIEW if proposal.valid else COLOR_PREVIEW_BAD
            edge = COLOR_PREVIEW_EDGE if proposal.valid else COLOR_PREVIEW_BAD_EDGE
            for segment in proposal.new:
                box = self.segment_box(segment)
                self._paint_box(painter, box, fill, edge)
                if show_text:
                    self._paint_box_text(painter, box, segment.text, QColor("#ffffff"))
        painter.restore()

    @staticmethod
    def _paint_box(painter: QPainter, box: QRectF, fill: QColor, edge: QColor) -> None:
        """A narration box: slightly rounded corners, a 2 px edge on every side (the
        pen runs 1 px inside the whole-pixel box, so no side gets thicker)."""
        if box.width() < 2 * BOX_EDGE + 1 or box.height() < 2 * BOX_EDGE + 1:
            painter.fillRect(box, edge)
            return
        painter.setPen(QPen(edge, BOX_EDGE))
        painter.setBrush(QBrush(fill))
        half = BOX_EDGE / 2
        radius = min(BOX_RADIUS, box.width() / 2 - half, box.height() / 2 - half)
        painter.drawRoundedRect(box.adjusted(half, half, -half, -half), radius, radius)

    @staticmethod
    def _paint_box_text(painter: QPainter, box: QRectF, text: str, color: QColor) -> None:
        if box.width() < NARRATION_TEXT_MIN_W:
            return
        inner = box.adjusted(4, 0, -4, 0)
        shown = painter.fontMetrics().elidedText(
            " ".join(text.split()), Qt.TextElideMode.ElideRight, int(inner.width())
        )
        painter.setPen(QPen(color))
        align = int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        painter.drawText(inner, align, shown)

    def _paint_lock(self, painter: QPainter, rect: QRect) -> None:
        """A padlock: closed while the tracks are locked, the shackle lifted and open
        while they are unlocked, dim while it cannot be opened."""
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        unlocked = self._narration_unlocked
        color = QColor(self.colors["lock"] if unlocked else self.colors["text_dim"])
        if not self._narration_editable:
            color.setAlpha(110)
        body = QRectF(rect.x(), rect.y() + rect.height() * 0.45, rect.width(),
                      rect.height() * 0.55)  # fmt: skip
        lift = rect.height() * 0.3 if unlocked else 0.0
        shackle = QRectF(rect.x() + rect.width() * 0.2, rect.y() + 0.75 - lift,
                         rect.width() * 0.6, rect.height() * 0.8)  # fmt: skip
        painter.setPen(QPen(color, 1.5))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawArc(shackle, 0, 180 * 16)
        middle = shackle.center().y()
        painter.drawLine(QPointF(shackle.left(), middle), QPointF(shackle.left(), body.top()))
        # Unlocked, the right leg hangs free above the body.
        right_end = middle + lift * 0.4 if unlocked else body.top()
        painter.drawLine(QPointF(shackle.right(), middle), QPointF(shackle.right(), right_end))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(color))
        painter.drawRoundedRect(body, 1.5, 1.5)
        painter.restore()

    def _paint_fit_button(self, painter: QPainter) -> None:
        """Four corner brackets: pointing out (full screen) while the waveform shows its
        real level, pointing in (back to normal) while it is stretched."""
        rect = self.fit_button_rect()
        if rect is None:
            return
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        if self._audio_fit:
            highlight = QColor(COLOR_SELECTED)
            highlight.setAlpha(70)
            painter.fillRect(rect, highlight)
        pen = QPen(self.colors["text"], 1.6)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        r = QRectF(rect).adjusted(3.5, 3.5, -3.5, -3.5)
        arm = r.width() * (0.30 if self._audio_fit else 0.38)
        corners = (
            (r.topLeft(), 1, 1),
            (r.topRight(), -1, 1),
            (r.bottomLeft(), 1, -1),
            (r.bottomRight(), -1, -1),
        )
        for corner, dx, dy in corners:
            if self._audio_fit:  # brackets pointing in: their corner sits inside
                tip = QPointF(corner.x() + dx * arm, corner.y() + dy * arm)
                painter.drawLine(QPointF(corner.x(), tip.y()), tip)
                painter.drawLine(QPointF(tip.x(), corner.y()), tip)
            else:
                painter.drawLine(corner, QPointF(corner.x() + dx * arm, corner.y()))
                painter.drawLine(corner, QPointF(corner.x(), corner.y() + dy * arm))
        painter.restore()

    def _paint_crop(self, painter: QPainter, canvas: QRect) -> None:
        top = self._tracks_top()
        bottom = self.tracks_bottom() + 1
        x_in = self._x(self.model.in_ms)
        x_out = self._x(self.model.out_ms)
        shade = self.colors["shade"]
        painter.setClipRect(QRect(HEADER_W, top, self._body_width(), bottom - top))
        if x_in > HEADER_W:
            painter.fillRect(QRectF(HEADER_W, top, x_in - HEADER_W, bottom - top), shade)
        if x_out < canvas.right():
            painter.fillRect(QRectF(x_out, top, canvas.right() - x_out + 1, bottom - top), shade)
        painter.setPen(QPen(COLOR_HANDLE, 2))
        painter.setBrush(QBrush(COLOR_HANDLE))
        last_column = self.width() - 1  # _x(duration) is width at Fit: draw it there
        for x, direction in ((x_in, 1), (min(x_out, last_column), -1)):
            painter.drawLine(QPointF(x, top), QPointF(x, bottom))
            for y in (top, bottom):
                tri = QPolygonF(
                    [
                        QPointF(x, y),
                        QPointF(x + direction * 8, y),
                        QPointF(x, y + (8 if y == top else -8)),
                    ]
                )
                painter.drawPolygon(tri)
        painter.setClipping(False)

    def keypoint_x(self, time_ms: int) -> float | None:
        """Where the keypoint line is drawn; ``None`` when it is scrolled out of view."""
        x = self._x(time_ms)
        if x < HEADER_W or x > self.width() - 1:
            return None
        return min(x, float(self.width() - 1))

    def _paint_pauses(self, painter: QPainter) -> None:
        """Gray lines with a gray arrow on the ruler where the recording was paused, drawn
        like the keypoints (below them when both fall on one spot)."""
        if not self.model.pauses:
            return
        bottom = self.tracks_bottom() + 1
        painter.setClipRect(QRect(HEADER_W, 0, self._body_width(), bottom))
        pen = QPen(COLOR_PAUSE, KEYPOINT_W)
        painter.setBrush(QBrush(COLOR_PAUSE))
        for at_ms, _length in self.model.pauses:
            x = self.keypoint_x(at_ms)
            if x is None:
                continue
            pen.setWidth(KEYPOINT_W + 1 if at_ms == self._hover_pause else KEYPOINT_W)
            painter.setPen(pen)
            painter.drawLine(QPointF(x, 0), QPointF(x, bottom))
            painter.setPen(QPen(COLOR_PAUSE, 1))
            painter.drawPolygon(
                QPolygonF(
                    [
                        QPointF(x - KEYPOINT_ARROW_HALF_W, 0),
                        QPointF(x + KEYPOINT_ARROW_HALF_W, 0),
                        QPointF(x, KEYPOINT_ARROW_H),
                    ]
                )
            )
        painter.setClipping(False)

    def _paint_keypoints(self, painter: QPainter) -> None:
        """Thin green lines from the ruler down through the screen and audio tracks, each
        with a green arrow at the top of the ruler, the size and place of the playhead's
        red one (no handle, not movable). The hovered keypoint is drawn a little bolder."""
        if not self.model.keypoints:
            return
        bottom = self.tracks_bottom() + 1
        painter.setClipRect(QRect(HEADER_W, 0, self._body_width(), bottom))
        pen = QPen(COLOR_KEYPOINT, KEYPOINT_W)
        painter.setBrush(QBrush(COLOR_KEYPOINT))
        for keypoint in self.model.keypoints:
            x = self.keypoint_x(keypoint)
            if x is None:
                continue
            pen.setWidth(KEYPOINT_W + 1 if keypoint == self._hover_keypoint else KEYPOINT_W)
            painter.setPen(pen)
            painter.drawLine(QPointF(x, 0), QPointF(x, bottom))
            painter.setPen(QPen(COLOR_KEYPOINT, 1))
            painter.drawPolygon(
                QPolygonF(
                    [
                        QPointF(x - KEYPOINT_ARROW_HALF_W, 0),
                        QPointF(x + KEYPOINT_ARROW_HALF_W, 0),
                        QPointF(x, KEYPOINT_ARROW_H),
                    ]
                )
            )
        painter.setClipping(False)

    def play_marker_x(self) -> float | None:
        """Where the white Play arrow sits on the ruler (``None`` without a marker)."""
        marker = self.model.play_marker_ms
        if marker is None:
            return None
        x = self._x(marker)
        if x < HEADER_W or x > self.width():
            return None
        return min(x, float(self.width() - 1))

    def _paint_play_marker(self, painter: QPainter) -> None:
        """A white arrow in the ruler where Play was last pressed: Stop returns there."""
        x = self.play_marker_x()
        if x is None:
            return
        color = self.colors["play_marker"]
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(QPen(color, 1))
        painter.setBrush(QBrush(color))
        top = RULER_H - 11
        painter.drawPolygon(
            QPolygonF([QPointF(x - 5, top), QPointF(x + 5, top), QPointF(x, RULER_H - 2)])
        )
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)

    def _paint_transcription(self, painter: QPainter) -> None:
        """Green arrow under the audio track at the transcription front, its label
        below; the audio whisper has not reached yet is shaded."""
        if self._transcription is None or self.recording is None:
            return
        state, reached = self._transcription
        lane = self.progress_rect()
        body_left = HEADER_W
        body_right = self.width() - 1
        font = QFont(painter.font())
        font.setPointSizeF(max(7.0, font.pointSizeF() - 1))
        painter.setFont(font)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        if state == "transcribing":
            audio = self.audio_rect()
            x_reached = self._x(reached)
            if x_reached < body_right:
                painter.setClipRect(QRect(body_left, audio.y(), self._body_width(), audio.height()))
                painter.fillRect(
                    QRectF(max(x_reached, float(body_left)), audio.y(), body_right - x_reached + 1,
                           audio.height()),
                    self.colors["untranscribed"],
                )  # fmt: skip
                painter.setClipping(False)
            x = self.transcription_arrow_x()
            assert x is not None
            top = lane.y() + 1
            green = self.colors["transcribing"]
            painter.setPen(QPen(green, 2))
            painter.setBrush(QBrush(green))
            painter.drawPolygon(
                QPolygonF(
                    [
                        QPointF(x, top),
                        QPointF(x - ARROW_HALF_W, top + ARROW_H),
                        QPointF(x + ARROW_HALF_W, top + ARROW_H),
                    ]
                )
            )
            painter.drawLine(QPointF(x, top + ARROW_H), QPointF(x, top + ARROW_H + 4))
            label = transcribing_label(self._eta_s)
            metrics = painter.fontMetrics()
            width = metrics.horizontalAdvance(label) + 8
            left = min(max(x - width / 2, float(body_left)), float(body_right - width))
            text_rect = QRectF(left, top + ARROW_H + 4, width, lane.height() - ARROW_H - 5)
            painter.setPen(QPen(green))
            painter.drawText(
                text_rect, Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop, label
            )
        else:
            painter.setPen(QPen(self.colors["text_dim"]))
            painter.drawText(
                QRect(body_left + 6, lane.y(), self._body_width() - 12, lane.height()),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                TRANSCRIPTION_LABELS.get(state, state),
            )
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)

    def _paint_playhead(self, painter: QPainter, canvas: QRect) -> None:
        x = self._x(self.model.playhead_ms)
        if x < HEADER_W or x > self.width():
            return
        x = min(x, self.width() - 1)  # the end of the recording lands on the last column
        painter.setPen(QPen(COLOR_PLAYHEAD, 1))
        painter.drawLine(QPointF(x, 0), QPointF(x, self.tracks_bottom() + 1))
        painter.setBrush(QBrush(COLOR_PLAYHEAD))
        painter.drawPolygon(QPolygonF([QPointF(x - 6, 0), QPointF(x + 6, 0), QPointF(x, 9)]))


__all__ = [
    "AUDIO_H",
    "AUDIO_ICONS",
    "AUDIO_ICON_TOOLTIPS",
    "COLOR_KEYPOINT",
    "COLOR_MATCH",
    "COLOR_NARRATION",
    "COLOR_PLAY_MARKER",
    "COLOR_TRANSCRIBING",
    "COLOR_TRANSCRIBING_LIGHT",
    "DARK_COLORS",
    "HEADER_W",
    "KEYPOINT_ARROW_H",
    "KEYPOINT_ARROW_HALF_W",
    "KEYPOINT_HIT_PX",
    "KEYPOINT_W",
    "LIGHT_COLORS",
    "LOCK_TOOLTIP_LOCKED",
    "LOCK_TOOLTIP_UNAVAILABLE",
    "LOCK_TOOLTIP_UNLOCKED",
    "NARRATION_H",
    "NARRATION_LANE_H",
    "NARRATION_TEXT_MIN_ZOOM",
    "NARRATION_TRACKS",
    "PLAYHEAD_HIT_PX",
    "PROGRESS_H",
    "RULER_H",
    "TRACK_H",
    "TRANSCRIBE",
    "TRANSCRIBING_LABEL",
    "TimelineView",
    "format_eta",
    "tick_label",
    "tick_step_ms",
    "transcribing_label",
]
