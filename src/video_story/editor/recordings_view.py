"""The recordings list: one row per recording with a preview that follows the mouse.

Each row shows, from the left, a thumbnail of one screen, then the start date and time,
the length (minutes and seconds) and the words spoken, and in the top right corner one
dot per screen (at most four; the filled dot is the screen in the thumbnail - click
another dot to switch; hovering a dot names its screen and size, e.g. ``screen2
1920x1080``). Rows are separated by a thin grey line.

Moving the mouse over the text part of a row scrubs through that recording, like the
hover scrub of DaVinci Resolve's media pool: the horizontal position maps to a moment of
the recording (left edge = start, right edge = end), a thin red line marks it, the
thumbnail shows the selected screen at that moment and the text shows what was being
said. Leaving the row returns it to the start.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass

from PySide6.QtCore import (
    QEvent,
    QModelIndex,
    QPersistentModelIndex,
    QPoint,
    QPointF,
    QRect,
    QRectF,
    QSize,
    Qt,
)
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetrics,
    QHelpEvent,
    QImage,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPalette,
    QPen,
    QTextLayout,
)
from PySide6.QtWidgets import (
    QListView,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QToolTip,
    QWidget,
)

from video_story.host.runner import FrameIndex

from .recordings_model import ItemRole, RecordingItem
from .timeline.thumbnails import ThumbnailCache

PAD = 6
THUMB_W = 112  # thumbnail box (16:10); the frame is fitted into it
THUMB_H = 70
TEXT_GAP = 8  # between the thumbnail and the text
DOT_D = 10  # screen dots
DOT_GAP = 5
MAX_DOTS = 4
TEXT_LINES = 2  # lines of spoken text under the date and the length
SCRUB_COLOR = QColor("#ff4a4a")  # the red of the timeline's playhead
SEPARATOR_DARK = QColor("#505050")
SEPARATOR_LIGHT = QColor("#cfcfcf")
THUMB_BG = QColor("#101010")
MIN_LIST_WIDTH = 300  # narrower than this and the splitter hides the list completely


@dataclass
class _Hover:
    row: int
    fraction: float  # 0..1 across the scrub area


class _Frames:
    """Frame names of one recording, per screen, with their times for bisecting."""

    def __init__(self, item: RecordingItem) -> None:
        frames_dir = item.paths.frames_dir(item.paths.final_dir)
        try:
            self.index: FrameIndex | None = FrameIndex(frames_dir) if frames_dir.is_dir() else None
        except OSError:
            self.index = None
        by_screen = self.index.by_screen if self.index is not None else {}
        self.times = {sid: [n.time_ms for n in names] for sid, names in by_screen.items()}

    def image_at(self, screen_id: str, time_ms: int):
        times = self.times.get(screen_id)
        if not times or self.index is None:
            return None
        position = max(0, bisect.bisect_right(times, time_ms) - 1)
        return self.index._ref(self.index.by_screen[screen_id][position]).image


def separator_color(palette: QPalette) -> QColor:
    dark = palette.color(QPalette.ColorRole.Base).lightness() < 128
    return SEPARATOR_DARK if dark else SEPARATOR_LIGHT


def elided_lines(text: str, font: QFont, width: int, max_lines: int) -> list[str]:
    """``text`` word-wrapped to ``width``, at most ``max_lines`` lines, the last elided."""
    if not text or width <= 0:
        return []
    layout = QTextLayout(text, font)
    layout.beginLayout()
    lines: list[str] = []
    while len(lines) < max_lines:
        line = layout.createLine()
        if not line.isValid():
            break
        line.setLineWidth(width)
        start, length = line.textStart(), line.textLength()
        if len(lines) == max_lines - 1:
            rest = text[start:]
            lines.append(QFontMetrics(font).elidedText(rest, Qt.TextElideMode.ElideRight, width))
            break
        lines.append(text[start : start + length].rstrip())
    layout.endLayout()
    return lines


class RecordingDelegate(QStyledItemDelegate):
    """Paints a row of :class:`RecordingsListView` (see the module docstring)."""

    def __init__(self, view: RecordingsListView) -> None:
        super().__init__(view)
        self.view = view

    # Geometry (also used by the view for the mouse) --------------------------

    @staticmethod
    def thumb_rect(rect: QRect) -> QRect:
        top = rect.top() + (rect.height() - THUMB_H) // 2
        return QRect(rect.left() + PAD, top, THUMB_W, THUMB_H)

    @staticmethod
    def text_rect(rect: QRect) -> QRect:
        left = rect.left() + PAD + THUMB_W + TEXT_GAP
        width = max(1, rect.right() - PAD - left)
        return QRect(left, rect.top() + PAD, width, rect.height() - 2 * PAD)

    @staticmethod
    def scrub_rect(rect: QRect) -> QRect:
        """Where the mouse scrubs: the whole text part of the row."""
        left = rect.left() + PAD + THUMB_W + TEXT_GAP
        return QRect(left, rect.top(), max(1, rect.right() - PAD - left), rect.height())

    @staticmethod
    def dot_rects(rect: QRect, count: int, line_height: int) -> list[QRect]:
        count = min(MAX_DOTS, count)
        y = rect.top() + PAD + (line_height - DOT_D) // 2
        right = rect.right() - PAD
        first = right - count * DOT_D - (count - 1) * DOT_GAP + 1
        return [QRect(first + k * (DOT_D + DOT_GAP), y, DOT_D, DOT_D) for k in range(count)]

    def sizeHint(  # noqa: N802
        self, option: QStyleOptionViewItem, index: QModelIndex | QPersistentModelIndex
    ) -> QSize:
        line = QFontMetrics(option.font).lineSpacing()
        text_h = (2 + TEXT_LINES) * line
        return QSize(MIN_LIST_WIDTH, max(THUMB_H, text_h) + 2 * PAD + 1)

    # Painting -----------------------------------------------------------------

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionViewItem,
        index: QModelIndex | QPersistentModelIndex,
    ) -> None:
        item = index.data(ItemRole)
        if not isinstance(item, RecordingItem):
            super().paint(painter, option, index)
            return
        rect = option.rect
        palette = option.palette
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        painter.save()
        painter.setClipRect(rect)
        if selected:
            painter.fillRect(rect, palette.color(QPalette.ColorRole.Highlight))
        elif option.state & QStyle.StateFlag.State_MouseOver:
            hover = QColor(palette.color(QPalette.ColorRole.Highlight))
            hover.setAlpha(40)
            painter.fillRect(rect, hover)
        text_color = palette.color(
            QPalette.ColorRole.HighlightedText if selected else QPalette.ColorRole.Text
        )
        time_ms = self.view.hover_time(index.row(), item)
        screen_id = self.view.screen_of(item)
        self._paint_thumbnail(painter, rect, item, screen_id, time_ms)

        font = QFont(option.font)
        metrics = QFontMetrics(font)
        line = metrics.lineSpacing()
        text = self.text_rect(rect)
        dots = self.dot_rects(rect, len(self.view.screens_of(item)), line)
        title_right = (dots[0].left() - 6) if dots else text.right()
        painter.setFont(font)
        painter.setPen(QPen(text_color))
        title = metrics.elidedText(
            item.title, Qt.TextElideMode.ElideRight, max(1, title_right - text.left())
        )
        align = int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        painter.drawText(
            QRect(text.left(), text.top(), title_right - text.left(), line), align, title
        )
        details = metrics.elidedText(item.details, Qt.TextElideMode.ElideRight, text.width())
        painter.drawText(QRect(text.left(), text.top() + line, text.width(), line), align, details)
        spoken = item.text_at(time_ms) if time_ms is not None else item.first_line
        for k, text_line in enumerate(elided_lines(spoken, font, text.width(), TEXT_LINES)):
            painter.drawText(
                QRect(text.left(), text.top() + (2 + k) * line, text.width(), line),
                align,
                text_line,
            )
        self._paint_dots(painter, dots, item, screen_id, text_color)
        if time_ms is not None and self.view.hover_row == index.row():
            self._paint_scrub_line(painter, rect)
        painter.setPen(QPen(separator_color(palette), 1))
        painter.drawLine(rect.left(), rect.bottom(), rect.right(), rect.bottom())
        painter.restore()

    def _paint_thumbnail(
        self,
        painter: QPainter,
        rect: QRect,
        item: RecordingItem,
        screen_id: str | None,
        time_ms: int | None,
    ) -> None:
        box = self.thumb_rect(rect)
        painter.fillRect(box, THUMB_BG)
        image = self.view.thumbnail(item, screen_id, time_ms or 0)
        if image is None:
            return
        ratio = image.devicePixelRatio() or 1.0
        width, height = image.width() / ratio, image.height() / ratio
        scale = min(box.width() / width, box.height() / height)
        w, h = width * scale, height * scale
        left = box.left() + (box.width() - w) / 2
        target = QRectF(left, box.top() + (box.height() - h) / 2, w, h)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.drawImage(target, image)

    @staticmethod
    def _paint_dots(
        painter: QPainter,
        dots: list[QRect],
        item: RecordingItem,
        screen_id: str | None,
        color: QColor,
    ) -> None:
        if not dots:
            return
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        screens = list(item.screen_ids)
        for k, dot in enumerate(dots):
            filled = k < len(screens) and screens[k] == screen_id
            painter.setPen(QPen(color, 1.5))
            painter.setBrush(QBrush(color) if filled else Qt.BrushStyle.NoBrush)
            painter.drawEllipse(QRectF(dot).adjusted(0.75, 0.75, -0.75, -0.75))
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)

    def _paint_scrub_line(self, painter: QPainter, rect: QRect) -> None:
        area = self.scrub_rect(rect)
        x = area.left() + self.view.hover_fraction * (area.width() - 1)
        painter.setPen(QPen(SCRUB_COLOR, 1))
        painter.drawLine(QPointF(x, rect.top() + 1), QPointF(x, rect.bottom() - 1))


class RecordingsListView(QListView):
    """The recordings list with thumbnail rows and the hover scrub (module docstring)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.delegate = RecordingDelegate(self)
        self.setItemDelegate(self.delegate)
        self.setUniformItemSizes(True)
        self.setSpacing(0)
        self.setEditTriggers(QListView.EditTrigger.NoEditTriggers)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollMode(QListView.ScrollMode.ScrollPerPixel)
        self.setMouseTracking(True)
        self.viewport().setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.thumbnails = ThumbnailCache(self, capacity=300)
        self.thumbnails.ready.connect(self.viewport().update)
        self._hover: _Hover | None = None
        self._screen: dict[str, str] = {}  # session id -> screen shown in the thumbnail
        self._frames: dict[str, _Frames] = {}  # session id -> frame index (lazy)
        self._last_image: dict[str, QImage] = {}  # session id -> thumbnail drawn last

    # Model --------------------------------------------------------------------

    def setModel(self, model) -> None:  # noqa: N802
        old = self.model()
        if old is not None:
            old.modelReset.disconnect(self._on_model_reset)
        super().setModel(model)
        if model is not None:
            model.modelReset.connect(self._on_model_reset)

    def _on_model_reset(self) -> None:
        """Refresh: frames may have changed; the chosen screens stay."""
        self._frames.clear()
        self._last_image.clear()
        self._hover = None

    # State used by the delegate -------------------------------------------------

    @property
    def hover_row(self) -> int | None:
        return self._hover.row if self._hover else None

    @property
    def hover_fraction(self) -> float:
        return self._hover.fraction if self._hover else 0.0

    def hover_time(self, row: int, item: RecordingItem) -> int | None:
        """The moment the row previews: under the mouse, else ``None`` (the start)."""
        if self._hover is None or self._hover.row != row:
            return None
        return int(round(self._hover.fraction * max(0, item.duration_ms)))

    def screens_of(self, item: RecordingItem) -> list[str]:
        screens = list(item.screen_ids)
        if not screens:
            frames = self._frames_of(item)
            screens = sorted(frames.times, key=lambda s: int(s.removeprefix("screen") or 0))
        return screens[:MAX_DOTS]

    def screen_of(self, item: RecordingItem) -> str | None:
        screens = self.screens_of(item)
        chosen = self._screen.get(item.session_id)
        if chosen in screens:
            return chosen
        return screens[0] if screens else None

    def set_screen(self, session_id: str, screen_id: str) -> None:
        self._screen[session_id] = screen_id
        self._last_image.pop(session_id, None)
        self.viewport().update()

    def _frames_of(self, item: RecordingItem) -> _Frames:
        frames = self._frames.get(item.session_id)
        if frames is None:
            frames = self._frames[item.session_id] = _Frames(item)
        return frames

    def thumbnail(self, item: RecordingItem, screen_id: str | None, time_ms: int) -> QImage | None:
        """The frame of ``screen_id`` at ``time_ms``, decoded in the background; until
        it is ready the row keeps the picture it showed last."""
        if screen_id is None:
            return None
        path = self._frames_of(item).image_at(screen_id, time_ms)
        if path is None:
            return None
        ratio = self.devicePixelRatioF() or 1.0
        image = self.thumbnails.get(path, int(round(THUMB_H * ratio)))
        if image is not None:
            image.setDevicePixelRatio(ratio)
            self._last_image[item.session_id] = image
            return image
        return self._last_image.get(item.session_id)

    # Mouse --------------------------------------------------------------------

    def dot_at(self, pos: QPoint) -> tuple[RecordingItem, str] | None:
        """The screen dot under ``pos`` (viewport coordinates)."""
        hit = self._dot_hit(pos)
        return (hit[0], hit[1]) if hit is not None else None

    def _dot_hit(self, pos: QPoint) -> tuple[RecordingItem, str, QRect] | None:
        index = self.indexAt(pos)
        item = index.data(ItemRole) if index.isValid() else None
        if not isinstance(item, RecordingItem):
            return None
        line = QFontMetrics(self.font()).lineSpacing()
        screens = self.screens_of(item)
        dots = self.delegate.dot_rects(self.visualRect(index), len(screens), line)
        for screen_id, rect in zip(screens, dots, strict=False):
            area = rect.adjusted(-3, -3, 3, 3)
            if area.contains(pos):
                return item, screen_id, area
        return None

    def scrub_to(self, pos: QPoint) -> None:
        """Preview the row under ``pos`` at the moment its x position stands for."""
        index = self.indexAt(pos)
        if not index.isValid():
            self.clear_hover()
            return
        rect = self.visualRect(index)
        area = self.delegate.scrub_rect(rect)
        previous = self._hover
        if previous is not None and previous.row == index.row() and not area.contains(pos):
            return  # over the thumbnail: keep the moment chosen last
        fraction = 0.0
        if area.contains(pos):
            fraction = (pos.x() - area.left()) / max(1, area.width() - 1)
        self._hover = _Hover(index.row(), min(1.0, max(0.0, fraction)))
        if previous is not None and previous.row != index.row():
            self.update(self.model().index(previous.row, 0))
        self.update(index)

    def clear_hover(self) -> None:
        if self._hover is None:
            return
        row = self._hover.row
        self._hover = None
        model = self.model()
        if model is not None and 0 <= row < model.rowCount():
            self.update(model.index(row, 0))

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        pos = event.position().toPoint()
        self.scrub_to(pos)
        on_dot = self.dot_at(pos) is not None
        self.viewport().setCursor(
            Qt.CursorShape.PointingHandCursor if on_dot else Qt.CursorShape.ArrowCursor
        )
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        hit = self.dot_at(event.position().toPoint())
        if hit is not None and event.button() == Qt.MouseButton.LeftButton:
            item, screen_id = hit
            self.set_screen(item.session_id, screen_id)
            event.accept()
            return  # a dot only switches the thumbnail; it does not open the recording
        super().mousePressEvent(event)

    def dot_tooltip(self, pos: QPoint) -> str | None:
        """Tooltip of the screen dot under ``pos``: ``screen1 2560x1600``."""
        hit = self.dot_at(pos)
        return hit[0].screen_label(hit[1]) if hit is not None else None

    def viewportEvent(self, event: QEvent) -> bool:  # noqa: N802
        if event.type() == QEvent.Type.ToolTip and isinstance(event, QHelpEvent):
            hit = self._dot_hit(event.pos())
            if hit is not None:
                item, screen_id, area = hit
                # Bound to the dot: leaving it hides the tooltip, the next dot shows its own.
                text = item.screen_label(screen_id)
                QToolTip.showText(event.globalPos(), text, self.viewport(), area)
                return True
        if event.type() == QEvent.Type.Leave:
            self.clear_hover()
            self.viewport().unsetCursor()
        return super().viewportEvent(event)

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        self.thumbnails.begin_paint()  # only the thumbnails of the visible rows are decoded
        super().paintEvent(event)

    def shutdown(self) -> None:
        self.thumbnails.shutdown()


__all__ = [
    "MIN_LIST_WIDTH",
    "RecordingDelegate",
    "RecordingsListView",
    "THUMB_H",
    "THUMB_W",
    "elided_lines",
    "separator_color",
]
