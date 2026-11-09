"""Custom-painted timeline: ruler, one video track per screen, audio track, crop handles.

Below the audio track there is a narrow lane for the background transcription: while
the recording is still being transcribed, a green arrow under the waveform marks how
far the transcription got (the audio beyond it is shaded) with "transcribing" and the
estimated time to the end written under the arrow; the lane stays empty once the
transcript is complete.

Keypoints (Insert keypoint while recording) are thin green lines across the tracks
with a green arrow at the top of the ruler, drawn like the red playhead: they cannot be
moved, show "Keypoint N" on hover and act as a magnet for the crop handles and the
playhead. A white arrow on the ruler marks where Play was last pressed
(Stop returns there). The +/- zoom keeps the playhead in the middle of the view.

A full screen / normal toggle in the audio header stretches the waveform vertically so
that the loudest sound of the recording reaches the full height of the track: quiet
speech becomes visible. Only the drawing is scaled; the audio itself is unchanged.
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPen,
    QPolygonF,
    QResizeEvent,
    QWheelEvent,
)
from PySide6.QtWidgets import QScrollBar, QToolTip, QWidget

from video_story.core.timeline import format_timestamp
from video_story.editor.recording import LoadedRecording

from .model import TimelineModel
from .thumbnails import ThumbnailCache
from .waveform import BLOCK_MS, WaveformLoader, column_peaks

RULER_H = 26
HEADER_W = 70
TRACK_H = 60
AUDIO_H = TRACK_H  # the audio track is as tall as each screen track
PROGRESS_H = 30  # lane under the audio track: transcription arrow and its label
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
COLOR_TRANSCRIBING = QColor("#3ddc84")
COLOR_UNTRANSCRIBED = QColor(0, 0, 0, 90)  # audio whisper has not reached yet

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
        self._transcription: tuple[str, int] | None = None  # (state, transcribed_ms)
        self._eta_s: float | None = None  # estimated seconds until the transcription ends
        self._hover_keypoint: int | None = None  # keypoint under the mouse (tooltip shown)
        self._audio_fit = False  # the waveform is stretched to the loudest sound
        self._audio_peak: float | None = None  # loudest sample of the loaded audio (0..1)
        self.colors = dict(DARK_COLORS)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMinimumHeight(RULER_H + TRACK_H + AUDIO_H + PROGRESS_H + 4 * GAP + 20)
        self.setMinimumWidth(HEADER_W + 200)

    # Public API -------------------------------------------------------------

    def set_recording(self, recording: LoadedRecording | None) -> None:
        self.recording = recording
        self._transcription = None
        self._eta_s = None
        self._hover_keypoint = None
        self.thumbnails.clear()
        self.model.set_duration(recording.duration_ms if recording else 0)
        self.model.set_screens(recording.screen_ids if recording else [])
        self.model.set_keypoints(recording.keypoints if recording else [])
        self.model.fit(self._body_width())
        audio = recording.audio if recording else None
        self._audio_peak = None
        self.waveform.load(audio.path if audio else None)
        # Every track must stay visible: the splitter may not shrink the timeline below it.
        self.setMinimumHeight(self.sizeHint().height())
        self.updateGeometry()
        self._after_view_change()

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
        height = RULER_H + GAP + tracks * (TRACK_H + GAP) + AUDIO_H + GAP + PROGRESS_H + GAP
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

    def progress_rect(self) -> QRect:
        """The lane under the audio track that holds the transcription arrow."""
        audio = self.audio_rect()
        return QRect(0, audio.bottom() + 1 + GAP, self.width(), PROGRESS_H)

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
        inside = 0 <= pos.y() <= self.audio_rect().bottom()  # its arrow on the ruler too
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

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
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
        self.setFocus()
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
        screen_id = self.track_at(pos)
        if screen_id is not None and self.model.select_screen(screen_id):
            self.screen_selected.emit(screen_id)
        if pos.x() >= HEADER_W:
            self._drag = "playhead"
            self._move_playhead_to(pos.x())
        self.update()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        pos = event.position().toPoint()
        if self._drag in ("in", "out"):
            self._drag_handle_to(pos.x())
        elif self._drag == "playhead":
            self._move_playhead_to(pos.x())
        else:
            fit = self._on_fit_button(pos)
            if fit is not None:
                self.setCursor(Qt.CursorShape.PointingHandCursor)
                self._show_fit_tooltip(event.globalPosition().toPoint(), fit)
                return
            near_handle = self._handle_at(pos) is not None
            self.setCursor(
                Qt.CursorShape.SizeHorCursor if near_handle else Qt.CursorShape.ArrowCursor
            )
            self._update_keypoint_tooltip(pos, event.globalPosition().toPoint())

    def _show_fit_tooltip(self, global_pos: QPoint, rect: QRect) -> None:
        text = FIT_TOOLTIP_OFF if self._audio_fit else FIT_TOOLTIP_ON
        QToolTip.showText(global_pos, text, self, rect.adjusted(-2, -2, 2, 2))

    def _update_keypoint_tooltip(self, pos: QPoint, global_pos: QPoint) -> None:
        keypoint = self.keypoint_at(pos)
        if keypoint == self._hover_keypoint:
            return
        self._hover_keypoint = keypoint
        if keypoint is None:
            QToolTip.hideText()
            return
        index = self.model.keypoints.index(keypoint) + 1
        QToolTip.showText(global_pos, self.keypoint_tooltip(index, keypoint), self)

    def leaveEvent(self, event) -> None:  # noqa: N802
        if self._hover_keypoint is not None:
            self._hover_keypoint = None
            QToolTip.hideText()
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            drag, self._drag = self._drag, None
            if drag == "playhead":
                self.playhead_drag_finished.emit()

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
        self._paint_crop(painter, canvas)
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
            if ref is None:
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
        bottom = self.audio_rect().bottom() + 1
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

    def _paint_keypoints(self, painter: QPainter) -> None:
        """Thin green lines from the ruler down through the screen and audio tracks, each
        with a green arrow at the top of the ruler, the size and place of the playhead's
        red one (no handle, not movable). The hovered keypoint is drawn a little bolder."""
        if not self.model.keypoints:
            return
        bottom = self.audio_rect().bottom() + 1
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
            painter.setPen(QPen(COLOR_TRANSCRIBING, 2))
            painter.setBrush(QBrush(COLOR_TRANSCRIBING))
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
            painter.setPen(QPen(COLOR_TRANSCRIBING))
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
        painter.drawLine(QPointF(x, 0), QPointF(x, self.audio_rect().bottom() + 1))
        painter.setBrush(QBrush(COLOR_PLAYHEAD))
        painter.drawPolygon(QPolygonF([QPointF(x - 6, 0), QPointF(x + 6, 0), QPointF(x, 9)]))


__all__ = [
    "AUDIO_H",
    "COLOR_KEYPOINT",
    "COLOR_PLAY_MARKER",
    "COLOR_TRANSCRIBING",
    "DARK_COLORS",
    "HEADER_W",
    "KEYPOINT_ARROW_H",
    "KEYPOINT_ARROW_HALF_W",
    "KEYPOINT_HIT_PX",
    "KEYPOINT_W",
    "LIGHT_COLORS",
    "PLAYHEAD_HIT_PX",
    "PROGRESS_H",
    "RULER_H",
    "TRACK_H",
    "TRANSCRIBING_LABEL",
    "TimelineView",
    "format_eta",
    "tick_label",
    "tick_step_ms",
    "transcribing_label",
]
