"""Preview of the selected screen at the playhead, transport buttons and subtitle.

The transport row holds icon buttons (go to In, step back, play/pause, stop, step
forward, go to Out) centred under the picture and, in its right corner above the
subtitle's **Edit** button, the light/dark theme toggle. The subtitle under the time
grows up to :data:`SUBTITLE_MAX_LINES` lines and scrolls vertically beyond that.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from pathlib import Path

from PySide6.QtCore import (
    QObject,
    QPointF,
    QRect,
    QRectF,
    QRunnable,
    QSize,
    Qt,
    QThreadPool,
    Signal,
)
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QImage,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPen,
    QPixmap,
    QPolygonF,
    QResizeEvent,
    QTextOption,
    QWheelEvent,
)
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from video_story.core.cursor import CursorPosition
from video_story.core.timeline import format_timestamp
from video_story.host.runner import FrameRef
from video_story.ui.icons import (
    moon_icon,
    pause_icon,
    pencil_icon,
    play_icon,
    skip_end_icon,
    skip_start_icon,
    step_back_icon,
    step_forward_icon,
    stop_square_icon,
    sun_icon,
)

from .workers import automatic_gc_paused

log = logging.getLogger(__name__)

IMAGE_CACHE_SIZE = 6
CURSOR_ARROW = [(0, 0), (0, 17), (4, 13), (7, 20), (10, 19), (7, 12), (12, 12)]
MAX_ZOOM = 8.0  # 800 % of the original image pixels (logical widget pixels per image pixel)
ZOOM_STEP = 1.25  # factor per wheel notch
SMOOTH_BELOW = 2.0  # smooth scaling under 200 %, crisp pixels above
TRANSPORT_BUTTON_SIZE = QSize(36, 28)
TRANSPORT_ICON_SIZE = QSize(20, 20)
SUBTITLE_MIN_LINES = 2
SUBTITLE_MAX_LINES = 5  # taller subtitles get a vertical scrollbar
SUBTITLE_TEXT_DARK = "#e6e6e6"  # the text colours of theme.dark_palette / light_palette
SUBTITLE_TEXT_LIGHT = "#1e1e1e"
PREVIEW_BG_DARK = QColor("#101010")
PREVIEW_BG_LIGHT = QColor("#d6d6d6")


class _Signals(QObject):
    decoded = Signal(str, object)  # path, QImage


class _DecodeJob(QRunnable):
    def __init__(self, path: Path, signals: _Signals) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self._path = path
        self._signals = signals

    def run(self) -> None:
        with automatic_gc_paused():
            try:
                image = QImage(str(self._path))
            except Exception:  # noqa: BLE001
                log.exception("decoding %s failed", self._path)
                image = QImage()
            self._signals.decoded.emit(str(self._path), image)


class PreviewWidget(QWidget):
    """Shows one frame (aspect ratio kept) plus the cursor arrow; decodes in a worker.

    The mouse wheel zooms the image (anchored at the pointer) between "fit" and
    ``MAX_ZOOM``; above fit the left button pans and a double-click returns to fit.
    Widget point ``w`` and image point ``i`` relate as ``w = origin + i * zoom``,
    where ``origin`` is the widget position of the image's top-left corner (centred
    at fit, the clamped pan offset above fit). Zoom is display only.
    """

    frame_shown = Signal(str)  # path of the frame that became visible
    zoom_changed = Signal(float)  # 1.0 = 100 % of the original image pixels

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._images: OrderedDict[str, QImage] = OrderedDict()
        self._pending: set[str] = set()
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(2)
        self._signals = _Signals(self)
        self._signals.decoded.connect(self._on_decoded)
        self._wanted: str | None = None
        self._shown: str | None = None
        self._scaled: tuple[str, QSize, QPixmap] | None = None
        self._cursor: CursorPosition | None = None
        self._screen_id: str | None = None
        self._draw_cursor = True
        self._message: str | None = "No recording"
        self._message_large = False
        self._image_size: QSize | None = None  # size of the image the zoom state refers to
        self._zoom: float | None = None  # None = fit (whole image visible, centred)
        self._offset = QPointF()  # widget position of the image's top-left when zoomed
        self._drag: tuple[QPointF, QPointF] | None = None  # press position, offset then
        self._background = PREVIEW_BG_DARK
        self.setMinimumSize(160, 100)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    # API --------------------------------------------------------------------

    def set_draw_cursor(self, enabled: bool) -> None:
        self._draw_cursor = enabled
        self.update()

    def set_dark(self, dark: bool) -> None:
        """Background around the picture: near black in the dark theme, grey in the light."""
        self._background = PREVIEW_BG_DARK if dark else PREVIEW_BG_LIGHT
        self.update()

    def set_message(self, text: str | None, large: bool = False) -> None:
        """Text shown instead of a picture; ``large`` for a big bold notice."""
        self._message = text
        self._message_large = large
        self.update()

    @property
    def message(self) -> str | None:
        return self._message

    def clear(self) -> None:
        """Another recording: forget every frame and the zoom state."""
        self.drop_frame()
        self._images.clear()
        old = self.zoom
        self._image_size = None
        self._zoom = None
        self._drag = None
        self._zoom_updated(old)

    def drop_frame(self) -> None:
        """Forget the shown frame but keep the zoom: another screen of the same recording.

        The next frame goes through :meth:`_adopt`, which keeps the zoom and pan for an
        image of the same size and re-fits for a different one (spec 4.5).
        """
        self._wanted = self._shown = None
        self._scaled = None
        self.update()

    def set_frame(
        self, ref: FrameRef | None, cursor: CursorPosition | None, screen_id: str | None
    ) -> None:
        self._cursor = cursor
        self._screen_id = screen_id
        if ref is None or ref.image is None:
            self._wanted = self._shown = None
            self.update()
            return
        key = str(ref.image)
        self._message = None
        if key != self._wanted:
            self._wanted = key
            self._request(ref.image)
        if key in self._images:
            self._shown = key
            self._adopt(self._images[key])
        self.update()

    def prefetch(self, ref: FrameRef | None) -> None:
        if ref is not None and ref.image is not None:
            self._request(ref.image)

    @property
    def shown_path(self) -> str | None:
        return self._shown

    @property
    def pending(self) -> int:
        return len(self._pending)

    def wait(self, timeout_ms: int = 10_000) -> bool:
        return self._pool.waitForDone(timeout_ms)

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(640, 400)

    # Zoom -------------------------------------------------------------------

    @property
    def fit_zoom(self) -> float:
        """Scale at which the whole image fits the widget (1.0 without an image)."""
        size = self._image_size
        if size is None or size.isEmpty() or self.width() <= 0 or self.height() <= 0:
            return 1.0
        return min(self.width() / size.width(), self.height() / size.height())

    @property
    def zoom(self) -> float:
        """Current scale: logical widget pixels per image pixel (1.0 = 100 %)."""
        return self.fit_zoom if self._zoom is None else self._zoom

    @property
    def at_fit(self) -> bool:
        return self._zoom is None

    def image_origin(self) -> QPointF:
        """Widget position of the image's top-left corner at the current zoom."""
        if self._zoom is None:
            size = self._image_size
            if size is None:
                return QPointF()
            fit = self.fit_zoom
            return QPointF(
                (self.width() - size.width() * fit) / 2, (self.height() - size.height() * fit) / 2
            )
        return QPointF(self._offset)

    def map_to_image(self, point: QPointF) -> QPointF:
        """Image coordinates (pixels of the frame) of a widget point."""
        origin = self.image_origin()
        zoom = self.zoom
        return QPointF((point.x() - origin.x()) / zoom, (point.y() - origin.y()) / zoom)

    def map_from_image(self, point: QPointF) -> QPointF:
        """Widget coordinates of an image point."""
        origin = self.image_origin()
        zoom = self.zoom
        return QPointF(origin.x() + point.x() * zoom, origin.y() + point.y() * zoom)

    def zoom_at(self, point: QPointF, factor: float) -> None:
        """Multiply the scale by ``factor`` keeping the image point under ``point`` fixed.

        The result is clamped to [fit, MAX_ZOOM]; at or below fit the image snaps back
        to fit and is centred.
        """
        if self._image_size is None or factor <= 0:
            return
        old = self.zoom
        new = min(old * factor, MAX_ZOOM)
        if new <= self.fit_zoom * (1 + 1e-9):
            self.reset_zoom()
            return
        anchor = self.map_to_image(point)
        self._offset = QPointF(point.x() - anchor.x() * new, point.y() - anchor.y() * new)
        self._zoom = new
        self._clamp_offset()
        self._zoom_updated(old)

    def reset_zoom(self) -> None:
        """Back to fit: the whole image visible and centred."""
        old = self.zoom
        self._zoom = None
        self._drag = None
        self._zoom_updated(old)

    def _adopt(self, image: QImage) -> None:
        """Keep the zoom for images of the same size, re-fit for a different size."""
        if self._image_size == image.size():
            return
        old = self.zoom
        self._image_size = QSize(image.size())
        self._zoom = None
        self._drag = None
        self._zoom_updated(old)

    def _clamp_offset(self) -> None:
        """Never leave a gap beyond an image edge; centre an axis where the image is small."""
        if self._zoom is None or self._image_size is None:
            return
        self._offset = QPointF(
            self._clamp_axis(self._offset.x(), self._image_size.width() * self._zoom, self.width()),
            self._clamp_axis(
                self._offset.y(), self._image_size.height() * self._zoom, self.height()
            ),
        )

    @staticmethod
    def _clamp_axis(value: float, extent: float, available: float) -> float:
        if extent <= available:
            return (available - extent) / 2
        return min(0.0, max(available - extent, value))

    def _zoom_updated(self, old: float) -> None:
        if self._zoom is None:
            self.unsetCursor()
        elif self._drag is None:
            self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.update()
        if abs(self.zoom - old) > 1e-9:
            self.zoom_changed.emit(self.zoom)

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802
        notches = event.angleDelta().y() / 120
        if self._image_size is None or self._shown is None or notches == 0:
            event.ignore()
            return
        self.zoom_at(event.position(), ZOOM_STEP**notches)
        event.accept()

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and self._zoom is not None:
            self._drag = (event.position(), QPointF(self._offset))
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._drag is None or self._zoom is None:
            super().mouseMoveEvent(event)
            return
        start, offset = self._drag
        delta = event.position() - start
        self._offset = QPointF(offset.x() + delta.x(), offset.y() + delta.y())
        self._clamp_offset()
        self.update()
        event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and self._drag is not None:
            self._drag = None
            if self._zoom is not None:
                self.setCursor(Qt.CursorShape.OpenHandCursor)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and self._zoom is not None:
            self.reset_zoom()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802
        super().resizeEvent(event)
        if self._zoom is None:
            return  # fit is recomputed from the widget size at paint time
        old = self._zoom
        if old <= self.fit_zoom * (1 + 1e-9):
            self.reset_zoom()
            return
        self._clamp_offset()
        self._zoom_updated(old)

    # Decoding ---------------------------------------------------------------

    def _request(self, path: Path) -> None:
        key = str(path)
        if key in self._images:
            self._images.move_to_end(key)
            return
        if key in self._pending:
            return
        self._pending.add(key)
        self._pool.start(_DecodeJob(path, self._signals))

    def _on_decoded(self, key: str, image: QImage) -> None:
        self._pending.discard(key)
        if image.isNull():
            return
        self._images[key] = image
        self._images.move_to_end(key)
        self._evict()
        if key == self._wanted:
            self._shown = key
            self._adopt(image)
            self.update()
            self.frame_shown.emit(key)

    def _evict(self) -> None:
        """Drop the oldest entries beyond the cache size, never the shown/wanted frame."""
        keep = {self._shown, self._wanted}
        for key in list(self._images):
            if len(self._images) <= IMAGE_CACHE_SIZE:
                break
            if key not in keep:
                del self._images[key]

    def _scaled_pixmap(self, key: str, image: QImage) -> QPixmap:
        target = image.size().scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio)
        if self._scaled is not None and self._scaled[0] == key and self._scaled[1] == target:
            return self._scaled[2]
        scaled = image.scaled(
            target, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
        )
        pixmap = QPixmap.fromImage(scaled)
        self._scaled = (key, target, pixmap)
        return pixmap

    # Painting ---------------------------------------------------------------

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.fillRect(self.rect(), self._background)
        image = self._images.get(self._shown) if self._shown else None
        if image is None:
            if self._message:
                painter.setPen(QPen(QColor("#9a9a9a" if self._background.lightness() < 128
                                           else "#505050")))  # fmt: skip
                if self._message_large:
                    font = QFont(painter.font())
                    font.setBold(True)
                    size = font.pointSizeF()
                    if size > 0:
                        font.setPointSizeF(size * 2.2)
                    painter.setFont(font)
                painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self._message)
            painter.end()
            return
        if self._image_size != image.size():
            self._adopt(image)
        if self._zoom is None:
            pixmap = self._scaled_pixmap(self._shown, image)  # type: ignore[arg-type]
            x = (self.width() - pixmap.width()) // 2
            y = (self.height() - pixmap.height()) // 2
            painter.drawPixmap(x, y, pixmap)
        else:
            self._paint_zoomed(painter, image)
        cursor = self._cursor
        if (
            self._draw_cursor
            and cursor is not None
            and cursor.screen_id == self._screen_id
            and image.width() > 0
        ):
            at = self.map_from_image(QPointF(cursor.x, cursor.y))
            self._paint_cursor(painter, at.x(), at.y())
        painter.end()

    def _paint_zoomed(self, painter: QPainter, image: QImage) -> None:
        """Draw only the visible part of the source image, scaled by the zoom."""
        zoom = self.zoom
        origin = self._offset
        target = QRectF(origin.x(), origin.y(), image.width() * zoom, image.height() * zoom)
        visible = target.intersected(QRectF(self.rect()))
        if visible.isEmpty():
            return
        source = QRectF(
            (visible.x() - origin.x()) / zoom,
            (visible.y() - origin.y()) / zoom,
            visible.width() / zoom,
            visible.height() / zoom,
        )
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, zoom < SMOOTH_BELOW)
        painter.drawImage(visible, image, source)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)

    @staticmethod
    def _paint_cursor(painter: QPainter, x: float, y: float) -> None:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        polygon = QPolygonF([QPointF(x + px * 1.2, y + py * 1.2) for px, py in CURSOR_ARROW])
        painter.setPen(QPen(QColor("black"), 1.5))
        painter.setBrush(QBrush(QColor("white")))
        painter.drawPolygon(polygon)


class SubtitleBox(QTextEdit):
    """The subtitle under the preview: centred text, read only, at most
    :data:`SUBTITLE_MAX_LINES` lines tall (then a vertical scrollbar appears)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setReadOnly(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        self.setWordWrapMode(QTextOption.WrapMode.WordWrap)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.viewport().setAutoFillBackground(False)
        self.set_dark(True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._text = ""
        self.document().setDocumentMargin(2)
        self._fit_height()

    def set_dark(self, dark: bool) -> None:
        """Text colour of the theme. A style sheet pins the palette the widget had when
        it was polished, so a later palette change of the application would leave the
        text white on the light background: the colour is part of the style sheet."""
        color = SUBTITLE_TEXT_DARK if dark else SUBTITLE_TEXT_LIGHT
        self.setStyleSheet(f"QTextEdit {{ background: transparent; color: {color}; }}")

    def text(self) -> str:
        return self._text

    def setText(self, text: str) -> None:  # noqa: N802 - QLabel-like API
        self._text = text
        self.setPlainText(text)
        self.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.verticalScrollBar().setValue(0)
        self._fit_height()

    def setFont(self, font) -> None:  # noqa: N802
        super().setFont(font)
        self.document().setDefaultFont(font)
        self._fit_height()

    def line_height(self) -> int:
        return self.fontMetrics().lineSpacing()

    def max_height(self) -> int:
        return self.line_height() * SUBTITLE_MAX_LINES + 8

    def visible_lines(self) -> int:
        """How many text lines fit into the current height."""
        return max(1, (self.height() - 8) // max(1, self.line_height()))

    def _fit_height(self) -> None:
        """Grow with the text from two lines up to the maximum, then scroll."""
        doc = self.document()
        width = max(50, self.viewport().width())
        doc.setTextWidth(width)
        needed = int(doc.size().height()) + 6
        lower = self.line_height() * SUBTITLE_MIN_LINES + 8
        self.setFixedHeight(max(lower, min(needed, self.max_height())))

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802
        super().resizeEvent(event)
        doc = self.document()
        if doc.textWidth() != self.viewport().width():
            self._fit_height()


class PreviewPanel(QWidget):
    """Preview widget, transport row and the subtitle label (with its Edit button) below."""

    play_pause = Signal()
    edit_subtitle = Signal()  # the Edit button next to the subtitle was clicked
    stop = Signal()
    go_in = Signal()
    go_out = Signal()
    step = Signal(int)  # +1 / -1 output frames
    theme_toggled = Signal()  # the light/dark button in the corner was clicked

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._dark = True
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)
        self.view = PreviewWidget(self)
        layout.addWidget(self.view, 1)

        # Icon transport buttons centred under the picture, the time centred below
        # them; the theme toggle sits in the right corner of the row (above Edit), with
        # an equal blank on the left so that the transport stays centred.
        row = QHBoxLayout()
        row.setSpacing(4)
        row.addSpacing(TRANSPORT_BUTTON_SIZE.width())
        row.addStretch(1)
        self.in_button = self._button("|<", "Go to In (Home)", self.go_in.emit, row)
        self.back_button = self._button(
            "<", "Step back 1/24 s (Left)", lambda: self.step.emit(-1), row
        )
        self.play_button = self._button("Play", "Play / Pause (Space)", self.play_pause.emit, row)
        self.stop_button = self._button(
            "Stop", "Stop and return to where Play was pressed (Esc)", self.stop.emit, row
        )
        self.forward_button = self._button(
            ">", "Step forward 1/24 s (Right)", lambda: self.step.emit(1), row
        )
        self.out_button = self._button(">|", "Go to Out (End)", self.go_out.emit, row)
        row.addStretch(1)
        self.theme_button = self._button("Theme", "", self.theme_toggled.emit, row)
        layout.addLayout(row)
        self._apply_icons()

        self.time_label = QLabel("00:00:00.000 / 00:00:00.000")
        self.time_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        font = self.time_label.font()
        font.setFamily("Consolas")
        font.setStyleHint(font.StyleHint.Monospace)
        self.time_label.setFont(font)
        layout.addWidget(self.time_label)

        self.subtitle_label = SubtitleBox(self)
        sub_font = self.subtitle_label.font()
        sub_font.setPointSizeF(sub_font.pointSizeF() + 2)
        self.subtitle_label.setFont(sub_font)
        self.edit_subtitle_button = QToolButton(self)
        self.edit_subtitle_button.setText("Edit")
        self.edit_subtitle_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.edit_subtitle_button.setToolTip(
            "Edit the text of this subtitle (its time cannot be changed)"
        )
        self.edit_subtitle_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.edit_subtitle_button.setEnabled(False)
        self.edit_subtitle_button.clicked.connect(self.edit_subtitle.emit)
        self.edit_subtitle_button.setIcon(pencil_icon(20))
        subtitle_row = QHBoxLayout()
        subtitle_row.setSpacing(6)
        subtitle_row.addWidget(self.subtitle_label, 1)
        subtitle_row.addWidget(self.edit_subtitle_button, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addLayout(subtitle_row)
        self._subtitle_text = ""

    @staticmethod
    def _button(text: str, tooltip: str, slot, row: QHBoxLayout) -> QToolButton:
        """An icon-only button; ``text`` names it (tests, accessibility) without showing."""
        button = QToolButton()
        button.setText(text)
        button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        button.setFixedSize(TRANSPORT_BUTTON_SIZE)
        button.setIconSize(TRANSPORT_ICON_SIZE)
        button.setToolTip(tooltip)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        button.setAutoRaise(False)
        button.clicked.connect(slot)
        row.addWidget(button)
        return button

    def _apply_icons(self) -> None:
        """(Re)draw the vector icons in the current button text colour."""
        self.in_button.setIcon(skip_start_icon())
        self.back_button.setIcon(step_back_icon())
        self.play_button.setIcon(
            pause_icon() if self.play_button.text() == "Pause" else play_icon()
        )
        self.stop_button.setIcon(stop_square_icon())
        self.forward_button.setIcon(step_forward_icon())
        self.out_button.setIcon(skip_end_icon())
        if hasattr(self, "edit_subtitle_button"):  # built after the transport row
            self.edit_subtitle_button.setIcon(pencil_icon(20))
        self.theme_button.setIcon(sun_icon() if self._dark else moon_icon())
        target = "light" if self._dark else "dark"
        self.theme_button.setToolTip(f"Switch to the {target} theme (Ctrl+T)")

    @property
    def dark(self) -> bool:
        return self._dark

    def set_dark(self, dark: bool) -> None:
        """The theme in force: picks the sun/moon icon and redraws the icons."""
        self._dark = dark
        self.view.set_dark(dark)
        self.subtitle_label.set_dark(dark)
        self._apply_icons()

    def set_playing(self, playing: bool) -> None:
        self.play_button.setText("Pause" if playing else "Play")
        self.play_button.setIcon(pause_icon() if playing else play_icon())
        self.play_button.setToolTip("Pause (Space)" if playing else "Play (Space)")

    def set_time(self, position_ms: int, total_ms: int) -> None:
        self.time_label.setText(
            f"{format_timestamp(max(0, position_ms))} / {format_timestamp(max(0, total_ms))}"
        )

    def set_subtitle(self, text: str) -> None:
        """Subtitles are always shown (there is no switch to hide them)."""
        self._subtitle_text = text
        if self.subtitle_label.text() != text:
            self.subtitle_label.setText(text)
        self.edit_subtitle_button.setEnabled(bool(text))

    def paint_rect(self) -> QRect:
        return self.view.rect()


__all__ = [
    "SUBTITLE_MAX_LINES",
    "SUBTITLE_MIN_LINES",
    "SUBTITLE_TEXT_DARK",
    "SUBTITLE_TEXT_LIGHT",
    "TRANSPORT_BUTTON_SIZE",
    "PreviewPanel",
    "PreviewWidget",
    "SubtitleBox",
]
