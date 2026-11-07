"""Custom-painted timeline: ruler, one video track per screen, audio track, crop handles."""

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
from PySide6.QtWidgets import QScrollBar, QWidget

from video_story.editor.recording import LoadedRecording

from .model import TimelineModel
from .thumbnails import ThumbnailCache
from .waveform import BLOCK_MS, WaveformLoader, column_peaks

RULER_H = 26
HEADER_W = 70
TRACK_H = 60
AUDIO_H = 48
GAP = 3
HANDLE_HIT_PX = 7
PLAYHEAD_HIT_PX = 2  # the playhead wins over a crop handle this close to its line
SNAP_PX = 8
THUMB_SPACING_PX = 160
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
        self.waveform.ready.connect(lambda _peaks: self.update())
        self.scrollbar = QScrollBar(Qt.Orientation.Horizontal, self)
        self.scrollbar.valueChanged.connect(self._on_scrollbar)
        self._syncing_scrollbar = False
        self._drag: str | None = None
        self._drag_offset = 0.0  # px between the grabbed handle and the press position
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMinimumHeight(RULER_H + TRACK_H + AUDIO_H + 3 * GAP + 20)
        self.setMinimumWidth(HEADER_W + 200)

    # Public API -------------------------------------------------------------

    def set_recording(self, recording: LoadedRecording | None) -> None:
        self.recording = recording
        self.thumbnails.clear()
        self.model.set_duration(recording.duration_ms if recording else 0)
        self.model.set_screens(recording.screen_ids if recording else [])
        self.model.fit(self._body_width())
        audio = recording.audio if recording else None
        self.waveform.load(audio.path if audio else None)
        # Every track must stay visible: the splitter may not shrink the timeline below it.
        self.setMinimumHeight(self.sizeHint().height())
        self.updateGeometry()
        self._after_view_change()

    def sizeHint(self) -> QSize:  # noqa: N802
        tracks = max(1, len(self.model.screens))
        height = RULER_H + GAP + tracks * (TRACK_H + GAP) + AUDIO_H + GAP
        return QSize(900, height + self.scrollbar.sizeHint().height())

    def zoom_in(self) -> None:
        self._zoom_around_playhead(1.5)

    def zoom_out(self) -> None:
        self._zoom_around_playhead(1 / 1.5)

    def fit(self) -> None:
        self.model.fit(self._body_width())
        self._after_view_change()

    def set_zoom_fraction(self, fraction: float) -> None:
        self.model.set_zoom_fraction(fraction)
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

    def _zoom_around_playhead(self, factor: float) -> None:
        x = self.model.time_to_x(self.model.playhead_ms)
        if not 0 <= x < self.model.viewport_width:
            x = self.model.viewport_width / 2
        self.model.zoom_around(x, factor)
        self._after_view_change()

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
            near_handle = self._handle_at(pos) is not None
            self.setCursor(
                Qt.CursorShape.SizeHorCursor if near_handle else Qt.CursorShape.ArrowCursor
            )

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            drag, self._drag = self._drag, None
            if drag == "playhead":
                self.playhead_drag_finished.emit()

    def _move_playhead_to(self, x: float) -> None:
        if self.model.set_playhead(self._time(x)):
            self.playhead_moved.emit(self.model.playhead_ms)
        self.update()

    def _drag_handle_to(self, x: float) -> None:
        x -= self._drag_offset
        time_ms = self._time(x)
        if abs(x - self._x(self.model.playhead_ms)) <= SNAP_PX:
            time_ms = self.model.playhead_ms
        changed = self.model.set_in(time_ms) if self._drag == "in" else self.model.set_out(time_ms)
        if changed:
            self.crop_changed.emit()
        self.update()

    # Painting ---------------------------------------------------------------

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        canvas = QRect(0, 0, self.width(), self._canvas_height())
        painter.fillRect(canvas, COLOR_BG)
        self._paint_ruler(painter)
        self.thumbnails.begin_paint()  # only the thumbnails requested below get decoded
        for screen_id, rect in self.track_rects():
            self._paint_video_track(painter, screen_id, rect)
        self._paint_audio_track(painter, self.audio_rect())
        self._paint_crop(painter, canvas)
        self._paint_playhead(painter, canvas)
        painter.end()

    def _paint_ruler(self, painter: QPainter) -> None:
        rect = QRect(0, 0, self.width(), RULER_H)
        painter.fillRect(rect, COLOR_RULER)
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
            painter.setPen(QPen(COLOR_TICK))
            painter.drawLine(x, RULER_H - 10, x, RULER_H)
            for k in range(1, 5):
                mx = int(self._x(t + k * minor))
                painter.drawLine(mx, RULER_H - 4, mx, RULER_H)
            painter.setPen(QPen(COLOR_TEXT))
            painter.drawText(x + 3, RULER_H - 12, tick_label(int(t), step))
            t += step
        painter.setClipping(False)
        painter.setPen(QPen(COLOR_TICK))
        painter.drawLine(0, RULER_H - 1, self.width(), RULER_H - 1)

    def _paint_header(self, painter: QPainter, rect: QRect, title: str, subtitle: str) -> None:
        header = QRect(rect.x(), rect.y(), HEADER_W, rect.height())
        painter.fillRect(header, COLOR_HEADER)
        painter.setPen(QPen(COLOR_TEXT))
        painter.drawText(
            header.adjusted(6, 4, -4, 0),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
            title,
        )
        if subtitle:
            painter.setPen(QPen(COLOR_TEXT_DIM))
            painter.drawText(
                header.adjusted(6, 0, -4, -4),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom,
                subtitle,
            )

    def _paint_video_track(self, painter: QPainter, screen_id: str, rect: QRect) -> None:
        body = QRect(HEADER_W, rect.y(), self._body_width(), rect.height())
        painter.fillRect(body, COLOR_TRACK)
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
                painter.setPen(QPen(COLOR_TICK))
                painter.drawRect(x, body.y() + 2, int(height * 1.6), height)
                continue
            painter.drawImage(QPoint(x, body.y() + 2), image)

    def _paint_audio_track(self, painter: QPainter, rect: QRect) -> None:
        body = QRect(HEADER_W, rect.y(), self._body_width(), rect.height())
        painter.fillRect(body, COLOR_TRACK)
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
        painter.setPen(QPen(COLOR_WAVE))
        for col in range(columns):
            lo, hi = float(reduced[col, 0]), float(reduced[col, 1])
            if lo == 0.0 and hi == 0.0:
                continue
            x = body.x() + col
            painter.drawLine(QPointF(x, mid - hi * half), QPointF(x, mid - lo * half))
        painter.setPen(QPen(COLOR_TICK))
        painter.drawLine(body.x(), int(mid), body.right(), int(mid))

    def _paint_crop(self, painter: QPainter, canvas: QRect) -> None:
        top = self._tracks_top()
        bottom = self.audio_rect().bottom() + 1
        x_in = self._x(self.model.in_ms)
        x_out = self._x(self.model.out_ms)
        painter.setClipRect(QRect(HEADER_W, top, self._body_width(), bottom - top))
        if x_in > HEADER_W:
            painter.fillRect(QRectF(HEADER_W, top, x_in - HEADER_W, bottom - top), COLOR_SHADE)
        if x_out < canvas.right():
            painter.fillRect(
                QRectF(x_out, top, canvas.right() - x_out + 1, bottom - top), COLOR_SHADE
            )
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

    def _paint_playhead(self, painter: QPainter, canvas: QRect) -> None:
        x = self._x(self.model.playhead_ms)
        if x < HEADER_W or x > self.width():
            return
        x = min(x, self.width() - 1)  # the end of the recording lands on the last column
        painter.setPen(QPen(COLOR_PLAYHEAD, 1))
        painter.drawLine(QPointF(x, 0), QPointF(x, canvas.bottom()))
        painter.setBrush(QBrush(COLOR_PLAYHEAD))
        painter.drawPolygon(QPolygonF([QPointF(x - 6, 0), QPointF(x + 6, 0), QPointF(x, 9)]))


__all__ = [
    "AUDIO_H",
    "HEADER_W",
    "PLAYHEAD_HIT_PX",
    "RULER_H",
    "TRACK_H",
    "TimelineView",
    "tick_label",
    "tick_step_ms",
]
