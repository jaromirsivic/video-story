"""Preview of the selected screen at the playhead, transport buttons and subtitle."""

from __future__ import annotations

import logging
from collections import OrderedDict
from pathlib import Path

from PySide6.QtCore import QObject, QPointF, QRect, QRunnable, QSize, Qt, QThreadPool, Signal
from PySide6.QtGui import QBrush, QColor, QImage, QPainter, QPaintEvent, QPen, QPixmap, QPolygonF
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from video_story.core.cursor import CursorPosition
from video_story.core.timeline import format_timestamp
from video_story.host.runner import FrameRef

log = logging.getLogger(__name__)

IMAGE_CACHE_SIZE = 6
CURSOR_ARROW = [(0, 0), (0, 17), (4, 13), (7, 20), (10, 19), (7, 12), (12, 12)]


class _Signals(QObject):
    decoded = Signal(str, object)  # path, QImage


class _DecodeJob(QRunnable):
    def __init__(self, path: Path, signals: _Signals) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self._path = path
        self._signals = signals

    def run(self) -> None:
        try:
            image = QImage(str(self._path))
        except Exception:  # noqa: BLE001
            log.exception("decoding %s failed", self._path)
            image = QImage()
        self._signals.decoded.emit(str(self._path), image)


class PreviewWidget(QWidget):
    """Shows one frame (aspect ratio kept) plus the cursor arrow; decodes in a worker."""

    frame_shown = Signal(str)  # path of the frame that became visible

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
        self.setMinimumSize(160, 100)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    # API --------------------------------------------------------------------

    def set_draw_cursor(self, enabled: bool) -> None:
        self._draw_cursor = enabled
        self.update()

    def set_message(self, text: str | None) -> None:
        self._message = text
        self.update()

    def clear(self) -> None:
        self._wanted = self._shown = None
        self._scaled = None
        self._images.clear()
        self.update()

    def set_frame(
        self, ref: FrameRef | None, cursor: CursorPosition | None, screen_id: str | None
    ) -> None:
        self._cursor = cursor
        self._screen_id = screen_id
        if ref is None:
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
        self.update()

    def prefetch(self, ref: FrameRef | None) -> None:
        if ref is not None:
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
        painter.fillRect(self.rect(), QColor("#101010"))
        image = self._images.get(self._shown) if self._shown else None
        if image is None:
            if self._message:
                painter.setPen(QPen(QColor("#9a9a9a")))
                painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self._message)
            painter.end()
            return
        pixmap = self._scaled_pixmap(self._shown, image)  # type: ignore[arg-type]
        x = (self.width() - pixmap.width()) // 2
        y = (self.height() - pixmap.height()) // 2
        painter.drawPixmap(x, y, pixmap)
        cursor = self._cursor
        if (
            self._draw_cursor
            and cursor is not None
            and cursor.screen_id == self._screen_id
            and image.width() > 0
        ):
            scale = pixmap.width() / image.width()
            self._paint_cursor(painter, x + cursor.x * scale, y + cursor.y * scale)
        painter.end()

    @staticmethod
    def _paint_cursor(painter: QPainter, x: float, y: float) -> None:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        polygon = QPolygonF([QPointF(x + px * 1.2, y + py * 1.2) for px, py in CURSOR_ARROW])
        painter.setPen(QPen(QColor("black"), 1.5))
        painter.setBrush(QBrush(QColor("white")))
        painter.drawPolygon(polygon)


class PreviewPanel(QWidget):
    """Preview widget, transport row and the subtitle label below."""

    play_pause = Signal()
    stop = Signal()
    go_in = Signal()
    go_out = Signal()
    step = Signal(int)  # +1 / -1 output frames

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)
        self.view = PreviewWidget(self)
        layout.addWidget(self.view, 1)

        row = QHBoxLayout()
        row.setSpacing(4)
        self.in_button = self._button("|<", "Go to In (Home)", self.go_in.emit, row)
        self.back_button = self._button(
            "<", "Step back 1/24 s (Left)", lambda: self.step.emit(-1), row
        )
        self.play_button = self._button("Play", "Play / Pause (Space)", self.play_pause.emit, row)
        self.play_button.setMinimumWidth(70)
        self.stop_button = self._button("Stop", "Stop and go to In (Esc)", self.stop.emit, row)
        self.forward_button = self._button(
            ">", "Step forward 1/24 s (Right)", lambda: self.step.emit(1), row
        )
        self.out_button = self._button(">|", "Go to Out (End)", self.go_out.emit, row)
        row.addSpacing(12)
        self.time_label = QLabel("00:00:00.000 / 00:00:00.000")
        font = self.time_label.font()
        font.setFamily("Consolas")
        font.setStyleHint(font.StyleHint.Monospace)
        self.time_label.setFont(font)
        row.addWidget(self.time_label)
        row.addStretch(1)
        self.subtitles_check = QCheckBox("Subtitles")
        self.subtitles_check.setChecked(True)
        self.subtitles_check.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        row.addWidget(self.subtitles_check)
        layout.addLayout(row)

        self.subtitle_label = QLabel("")
        self.subtitle_label.setWordWrap(True)
        self.subtitle_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sub_font = self.subtitle_label.font()
        sub_font.setPointSizeF(sub_font.pointSizeF() + 2)
        self.subtitle_label.setFont(sub_font)
        self.subtitle_label.setMinimumHeight(self.subtitle_label.fontMetrics().height() * 2 + 8)
        layout.addWidget(self.subtitle_label)
        self._subtitle_text = ""
        self.subtitles_check.toggled.connect(lambda _on: self._apply_subtitle())

    @staticmethod
    def _button(text: str, tooltip: str, slot, row: QHBoxLayout) -> QPushButton:
        button = QPushButton(text)
        button.setToolTip(tooltip)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        button.clicked.connect(slot)
        row.addWidget(button)
        return button

    def set_playing(self, playing: bool) -> None:
        self.play_button.setText("Pause" if playing else "Play")

    def set_time(self, position_ms: int, total_ms: int) -> None:
        self.time_label.setText(
            f"{format_timestamp(max(0, position_ms))} / {format_timestamp(max(0, total_ms))}"
        )

    def set_subtitle(self, text: str) -> None:
        self._subtitle_text = text
        self._apply_subtitle()

    def _apply_subtitle(self) -> None:
        shown = self._subtitle_text if self.subtitles_check.isChecked() else ""
        if self.subtitle_label.text() != shown:
            self.subtitle_label.setText(shown)

    def paint_rect(self) -> QRect:
        return self.view.rect()


__all__ = ["PreviewPanel", "PreviewWidget"]
