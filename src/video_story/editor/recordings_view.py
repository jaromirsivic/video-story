"""The recordings list: one row per recording with a preview that follows the mouse.

Each row shows, from the left, a thumbnail of one screen, then the start date and time,
the length (minutes and seconds) and the words spoken, and in the top right corner one
dot per screen (at most four; the filled dot is the screen in the thumbnail; hovering a
dot names its screen and size, e.g. ``screen2 1920x1080``). Clicking a dot switches
every row at once: the clicked row shows that screen, every other row the screen at the
same position, or its last screen when it has fewer (click the third dot: rows with
three or four screens show the third, rows with two the second, rows with one the
first). Rows are separated by a thin grey line.

Moving the mouse over the text part of a row scrubs through that recording, like the
hover scrub of DaVinci Resolve's media pool: the horizontal position maps to a moment of
the recording (left edge = start, right edge = end), a thin red line marks it, the
thumbnail shows the selected screen at that moment and the text shows what was being
said. Leaving the row returns it to the start.

A label in the middle of the thumbnail (black capitals on white) tells what happens to
a recording: ``TRANSCRIBING...`` and ``ARCHIVING...`` with the percentage under it
(one decimal; archiving counts all screens together, so one of two screens done is
50 %), ``ARCHIVED`` once it was archived. A recording without a label is not archived.

The recordings are grouped by folder: every folder starts with a header row (an
accordion) - a triangle (open / closed), the name with the number of videos in brackets
(bold for the main folder, where new recordings are saved; a long name is cut with
``…`` so that the count stays visible), a pin (empty; filled for the pinned folder, which
always stands first) and ``⋯`` (the folder's menu). A click on the header opens or closes
the folder, a click on the pin or on ``⋯`` emits :attr:`RecordingsListView.pin_clicked`
or :attr:`RecordingsListView.folder_menu_requested`. Shift and Ctrl select within one
folder only: a Shift or Ctrl click into another folder starts a new selection there.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass

from PySide6.QtCore import (
    QEvent,
    QItemSelection,
    QItemSelectionModel,
    QModelIndex,
    QPersistentModelIndex,
    QPoint,
    QPointF,
    QRect,
    QRectF,
    QSize,
    Qt,
    Signal,
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
    QPainterPath,
    QPaintEvent,
    QPalette,
    QPen,
    QPolygonF,
    QTextLayout,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QListView,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QToolTip,
    QWidget,
)

from video_story.host.runner import FrameIndex

from .recording import StillFrames
from .recordings_model import FolderHeader, FolderRole, ItemRole, RecordingItem
from .timeline.thumbnails import ThumbnailCache
from .timeline.view import COLOR_MATCH

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
BADGE_BG = QColor("#ffffff")
BADGE_FG = QColor("#000000")
BADGE_PAD = 4
BADGE_TRANSCRIBING = "TRANSCRIBING..."
BADGE_ARCHIVING = "ARCHIVING..."
BADGE_ARCHIVED = "ARCHIVED"
MATCH_FRAME_W = 3  # the yellow frame of a recording whose transcript has the searched text
HEADER_BUTTON = 22  # the pin and the menu button of a folder header (square)
HEADER_GAP = 6
CHEVRON = 9
PIN_TOOLTIP = "Pin to top"
UNPIN_TOOLTIP = "Unpin"
FOLDER_MENU_TOOLTIP = "Folder actions"
MAIN_FOLDER_TOOLTIP = "Main folder: new recordings are saved here"


def format_percent(fraction: float) -> str:
    """``37.5 %`` - one decimal place."""
    return f"{max(0.0, min(1.0, fraction)) * 100:.1f} %"


MIN_LIST_WIDTH = 300  # narrower than this and the splitter hides the list completely


@dataclass
class _Hover:
    row: int
    fraction: float  # 0..1 across the scrub area


class _Frames:
    """Frame names of one recording, per screen, with their times for bisecting."""

    def __init__(self, item: RecordingItem) -> None:
        paths, base = item.paths, item.paths.final_dir
        try:
            if item.archived:
                self.index: FrameIndex | None = StillFrames(
                    paths.archive_dir(base), paths.frames_info_dir(base), list(item.screen_ids)
                )
            else:
                self.index = FrameIndex.of(paths, base)
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


def header_background(palette: QPalette) -> QColor:
    """A header row stands out a little from the recording rows (both themes)."""
    base = palette.color(QPalette.ColorRole.Base)
    return base.lighter(135) if base.lightness() < 128 else base.darker(108)


def draw_pin(painter: QPainter, box: QRectF, color: QColor, filled: bool) -> None:
    """A push pin, empty (outline) or filled."""
    w, h = box.width(), box.height()
    cx = box.center().x()
    top = box.top() + h * 0.14
    grip_w, grip_h = w * 0.30, h * 0.30
    collar_w, collar_y = w * 0.56, top + h * 0.44
    body = QPainterPath()
    body.addPolygon(
        QPolygonF(
            [
                QPointF(cx - grip_w / 2, top),
                QPointF(cx + grip_w / 2, top),
                QPointF(cx + grip_w / 2, top + grip_h),
                QPointF(cx + collar_w / 2, collar_y),
                QPointF(cx - collar_w / 2, collar_y),
                QPointF(cx - grip_w / 2, top + grip_h),
            ]
        )
    )
    body.closeSubpath()
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    pen = QPen(color, max(1.2, w / 16))
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(pen)
    painter.setBrush(QBrush(color) if filled else Qt.BrushStyle.NoBrush)
    painter.drawPath(body)
    painter.drawLine(QPointF(cx, collar_y), QPointF(cx, box.bottom() - h * 0.08))
    painter.restore()


def draw_menu_dots(painter: QPainter, box: QRectF, color: QColor) -> None:
    """``⋯``: three dots in a row."""
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QBrush(color))
    radius = max(1.5, box.width() / 13)
    step = box.width() / 4.2
    center = box.center()
    for k in (-1, 0, 1):
        painter.drawEllipse(QPointF(center.x() + k * step, center.y()), radius, radius)
    painter.restore()


def draw_chevron(painter: QPainter, box: QRectF, color: QColor, collapsed: bool) -> None:
    """A small filled triangle: pointing down (open) or right (closed)."""
    c = box.center()
    r = box.width() / 2
    if collapsed:
        points = [
            QPointF(c.x() - r * 0.5, c.y() - r),
            QPointF(c.x() + r * 0.7, c.y()),
            QPointF(c.x() - r * 0.5, c.y() + r),
        ]
    else:
        points = [
            QPointF(c.x() - r, c.y() - r * 0.5),
            QPointF(c.x() + r, c.y() - r * 0.5),
            QPointF(c.x(), c.y() + r * 0.7),
        ]
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QBrush(color))
    painter.drawPolygon(QPolygonF(points))
    painter.restore()


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

    @staticmethod
    def menu_rect(rect: QRect) -> QRect:
        """The ``⋯`` button of a folder header."""
        top = rect.top() + (rect.height() - HEADER_BUTTON) // 2
        return QRect(rect.right() - PAD - HEADER_BUTTON + 1, top, HEADER_BUTTON, HEADER_BUTTON)

    @classmethod
    def pin_rect(cls, rect: QRect) -> QRect:
        """The pin button of a folder header (left of ``⋯``)."""
        return cls.menu_rect(rect).translated(-(HEADER_BUTTON + 2), 0)

    @staticmethod
    def chevron_rect(rect: QRect) -> QRect:
        top = rect.top() + (rect.height() - CHEVRON) // 2
        return QRect(rect.left() + PAD + 2, top, CHEVRON, CHEVRON)

    @classmethod
    def name_rect(cls, rect: QRect) -> QRect:
        left = cls.chevron_rect(rect).right() + 1 + HEADER_GAP
        right = cls.pin_rect(rect).left() - HEADER_GAP
        return QRect(left, rect.top(), max(1, right - left), rect.height())

    @staticmethod
    def header_font(base: QFont, main: bool) -> QFont:
        font = QFont(base)
        font.setBold(main)
        return font

    @classmethod
    def header_texts(cls, header: FolderHeader, font: QFont, width: int) -> tuple[str, str]:
        """The (possibly elided) name and `` (N)``: the count always stays visible."""
        metrics = QFontMetrics(cls.header_font(font, header.main))
        count = f" ({header.count})"
        room = max(1, width - metrics.horizontalAdvance(count))
        return metrics.elidedText(header.name, Qt.TextElideMode.ElideRight, room), count

    def sizeHint(  # noqa: N802
        self, option: QStyleOptionViewItem, index: QModelIndex | QPersistentModelIndex
    ) -> QSize:
        line = QFontMetrics(option.font).lineSpacing()
        if isinstance(index.data(FolderRole), FolderHeader):
            return QSize(MIN_LIST_WIDTH, max(HEADER_BUTTON + 6, line + 2 * PAD) + 1)
        text_h = (2 + TEXT_LINES) * line
        return QSize(MIN_LIST_WIDTH, max(THUMB_H, text_h) + 2 * PAD + 1)

    # Painting -----------------------------------------------------------------

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionViewItem,
        index: QModelIndex | QPersistentModelIndex,
    ) -> None:
        header = index.data(FolderRole)
        if isinstance(header, FolderHeader):
            self._paint_header(painter, option, index.row(), header)
            return
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
        self._paint_badge(painter, self.thumb_rect(rect), self.view.badge_of(item), option.font)

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
        if self.view.is_search_match(item.session_id):
            pen = QPen(COLOR_MATCH, MATCH_FRAME_W)
            pen.setJoinStyle(Qt.PenJoinStyle.MiterJoin)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            half = MATCH_FRAME_W / 2
            painter.drawRect(QRectF(rect).adjusted(half, half, -half - 1, -half - 1))
        painter.setPen(QPen(separator_color(palette), 1))
        painter.drawLine(rect.left(), rect.bottom(), rect.right(), rect.bottom())
        painter.restore()

    def _paint_header(
        self, painter: QPainter, option: QStyleOptionViewItem, row: int, header: FolderHeader
    ) -> None:
        rect = option.rect
        palette = option.palette
        ink = palette.color(QPalette.ColorRole.Text)
        painter.save()
        painter.setClipRect(rect)
        painter.fillRect(rect, header_background(palette))
        draw_chevron(painter, QRectF(self.chevron_rect(rect)), ink, header.collapsed)
        area = self.name_rect(rect)
        font = self.header_font(option.font, header.main)
        name, count = self.header_texts(header, option.font, area.width())
        metrics = QFontMetrics(font)
        painter.setFont(font)
        painter.setPen(QPen(ink))
        align = int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        painter.drawText(area, align, name)
        count_left = area.left() + metrics.horizontalAdvance(name)
        painter.drawText(
            QRect(count_left, area.top(), max(1, area.right() - count_left + 1), area.height()),
            align,
            count,
        )
        hovered = self.view.header_hover
        for part, box in (("pin", self.pin_rect(rect)), ("menu", self.menu_rect(rect))):
            if hovered == (row, part):
                shade = QColor(palette.color(QPalette.ColorRole.Highlight))
                shade.setAlpha(60)
                painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(shade)
                painter.drawRoundedRect(QRectF(box), 4, 4)
        draw_pin(painter, QRectF(self.pin_rect(rect)).adjusted(4, 3, -4, -3), ink, header.pinned)
        draw_menu_dots(painter, QRectF(self.menu_rect(rect)).adjusted(3, 3, -3, -3), ink)
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
    def _paint_badge(
        painter: QPainter, box: QRect, badge: tuple[str, str | None] | None, base: QFont
    ) -> None:
        if badge is None:
            return
        label, percent = badge
        font = QFont(base)
        font.setBold(True)
        size = font.pointSizeF()
        if size > 0:
            font.setPointSizeF(max(6.0, size * 0.85))
        metrics = QFontMetrics(font)
        lines = [label] if percent is None else [label, percent]
        width = max(metrics.horizontalAdvance(text) for text in lines) + 2 * BADGE_PAD
        width = min(width, box.width() - 2)
        height = metrics.height() * len(lines) + 2 * BADGE_PAD
        area = QRect(
            box.left() + (box.width() - width) // 2,
            box.top() + (box.height() - height) // 2,
            width,
            height,
        )
        painter.save()
        painter.fillRect(area, BADGE_BG)
        painter.setFont(font)
        painter.setPen(QPen(BADGE_FG))
        for k, text in enumerate(lines):
            line_rect = QRect(
                area.left(), area.top() + BADGE_PAD + k * metrics.height(), width, metrics.height()
            )
            painter.drawText(line_rect, int(Qt.AlignmentFlag.AlignCenter), text)
        painter.restore()

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
    """The recordings list with folder headers, thumbnail rows and the hover scrub
    (module docstring)."""

    pin_clicked = Signal(str)  # folder name: its pin was clicked
    folder_menu_requested = Signal(str, QPoint)  # folder name, global position
    folder_toggled = Signal(str, bool)  # folder name, collapsed now

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.delegate = RecordingDelegate(self)
        self.setItemDelegate(self.delegate)
        self.setUniformItemSizes(False)  # folder headers are lower than recordings
        self.setSpacing(0)
        self.setEditTriggers(QListView.EditTrigger.NoEditTriggers)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollMode(QListView.ScrollMode.ScrollPerPixel)
        self.setMouseTracking(True)
        self.viewport().setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.thumbnails = ThumbnailCache(self, capacity=300)
        self.thumbnails.ready.connect(self.viewport().update)
        self._hover: _Hover | None = None
        # The dot clicked last, as a position (0 = first screen), shared by every row:
        # a row with fewer screens shows its last one (see screen_of).
        self._screen_position = 0
        self._frames: dict[str, _Frames] = {}  # session id -> frame index (lazy)
        self._last_image: dict[str, QImage] = {}  # session id -> thumbnail drawn last
        # session id -> ("transcribing" | "archiving", fraction): work running right now
        self._activity: dict[str, tuple[str, float]] = {}
        self._matches: set[str] = set()  # session ids with the searched text (yellow frame)
        self._header_hover: tuple[int, str] | None = None  # (row, "pin" | "menu")
        self._clamping = False

    # Model --------------------------------------------------------------------

    def setModel(self, model) -> None:  # noqa: N802
        old = self.model()
        if old is not None:
            old.modelReset.disconnect(self._on_model_reset)
            old.dataChanged.disconnect(self._on_data_changed)
        super().setModel(model)
        if model is not None:
            model.modelReset.connect(self._on_model_reset)
            model.dataChanged.connect(self._on_data_changed)
            self.selectionModel().selectionChanged.connect(self._keep_selection_in_one_folder)
            self.apply_collapsed()

    def _on_model_reset(self) -> None:
        """Refresh: frames may have changed; the chosen screens stay."""
        self._frames.clear()
        self._last_image.clear()
        self._hover = None
        self._header_hover = None
        self.apply_collapsed()

    def _on_data_changed(self, *_args) -> None:
        self.apply_collapsed()

    # Folders ------------------------------------------------------------------

    def _header_at_row(self, row: int) -> FolderHeader | None:
        model = self.model()
        if model is None:
            return None
        value = model.index(row, 0).data(FolderRole)
        return value if isinstance(value, FolderHeader) else None

    def _folder_of_row(self, row: int) -> str | None:
        model = self.model()
        if model is None or not hasattr(model, "folder_of_row"):
            return None
        return model.folder_of_row(row)

    def apply_collapsed(self) -> None:
        """Hide the recordings of collapsed folders, show the others."""
        model = self.model()
        if model is None:
            return
        collapsed = False
        for row in range(model.rowCount()):
            header = self._header_at_row(row)
            if header is not None:
                collapsed = header.collapsed
                hidden = False
            else:
                hidden = collapsed
            if self.isRowHidden(row) != hidden:
                self.setRowHidden(row, hidden)

    def toggle_folder(self, folder: str) -> None:
        """Open a closed folder, close an open one (stored in ``folders.json``)."""
        model = self.model()
        if model is None or not hasattr(model, "set_collapsed"):
            return
        collapsed = not model.is_collapsed(folder)
        if model.set_collapsed(folder, collapsed):
            self.folder_toggled.emit(folder, collapsed)

    def header_hit(self, pos: QPoint) -> tuple[FolderHeader, str, QRect] | None:
        """The folder header under ``pos`` and the part hit: ``"pin"``, ``"menu"`` or
        ``"header"`` (anywhere else on the row), with that part's rectangle."""
        index = self.indexAt(pos)
        header = index.data(FolderRole) if index.isValid() else None
        if not isinstance(header, FolderHeader):
            return None
        rect = self.visualRect(index)
        for part, area in (
            ("pin", self.delegate.pin_rect(rect)),
            ("menu", self.delegate.menu_rect(rect)),
        ):
            if area.contains(pos):
                return header, part, area
        return header, "header", rect

    @property
    def header_hover(self) -> tuple[int, str] | None:
        return self._header_hover

    def _set_header_hover(self, value: tuple[int, str] | None) -> None:
        if value != self._header_hover:
            self._header_hover = value
            self.viewport().update()

    def _keep_selection_in_one_folder(self, *_args) -> None:
        """Bulk actions work within one folder: rows of other folders than the current
        row's are deselected (Shift ranges across a header, Ctrl+A)."""
        if self._clamping:
            return
        selection = self.selectionModel()
        current = self.currentIndex()
        if selection is None or not current.isValid():
            return
        folder = self._folder_of_row(current.row())
        if folder is None:
            return
        foreign = QItemSelection()
        for index in selection.selectedRows():
            if self._folder_of_row(index.row()) != folder:
                foreign.select(index, index)
        if foreign.isEmpty():
            return
        self._clamping = True
        try:
            selection.select(foreign, QItemSelectionModel.SelectionFlag.Deselect)
        finally:
            self._clamping = False

    def moveCursor(  # noqa: N802
        self, action: QAbstractItemView.CursorAction, modifiers: Qt.KeyboardModifier
    ) -> QModelIndex:
        """Keys skip the folder headers and the rows of closed folders; Shift does not
        leave the current folder."""
        index = super().moveCursor(action, modifiers)
        model = self.model()
        current = self.currentIndex()
        if model is None or not index.isValid():
            return index
        backwards = action in (
            QAbstractItemView.CursorAction.MoveUp,
            QAbstractItemView.CursorAction.MovePageUp,
            QAbstractItemView.CursorAction.MovePrevious,
            QAbstractItemView.CursorAction.MoveEnd,
        )
        step = -1 if backwards else 1
        row = index.row()
        while 0 <= row < model.rowCount() and (
            self._header_at_row(row) is not None or self.isRowHidden(row)
        ):
            row += step
        if not 0 <= row < model.rowCount():
            return current
        if (
            modifiers & Qt.KeyboardModifier.ShiftModifier
            and current.isValid()
            and self._folder_of_row(current.row()) != self._folder_of_row(row)
        ):
            return current
        return model.index(row, 0)

    # Labels ---------------------------------------------------------------------

    def set_activity(self, session_id: str, kind: str | None, fraction: float = 0.0) -> None:
        """What runs for a recording now (``"transcribing"``, ``"archiving"``) and how
        far it got; ``None`` clears it."""
        if kind is None:
            changed = self._activity.pop(session_id, None) is not None
        else:
            value = (kind, max(0.0, min(1.0, fraction)))
            changed = self._activity.get(session_id) != value
            self._activity[session_id] = value
        if changed:
            self.viewport().update()

    def activity(self, session_id: str) -> tuple[str, float] | None:
        return self._activity.get(session_id)

    # Search ---------------------------------------------------------------------

    def set_search_matches(self, session_ids) -> None:
        """The recordings framed yellow (their transcript contains the searched text)."""
        matches = set(session_ids)
        if matches != self._matches:
            self._matches = matches
            self.viewport().update()

    def add_search_match(self, session_id: str) -> None:
        if session_id not in self._matches:
            self._matches.add(session_id)
            self.viewport().update()

    def is_search_match(self, session_id: str) -> bool:
        return session_id in self._matches

    @property
    def search_matches(self) -> set[str]:
        return set(self._matches)

    def badge_of(self, item: RecordingItem) -> tuple[str, str | None] | None:
        """The label of the thumbnail: ``(text, percent or None)``, ``None`` for none."""
        active = self._activity.get(item.session_id)
        if active is not None:
            kind, fraction = active
            label = BADGE_TRANSCRIBING if kind == "transcribing" else BADGE_ARCHIVING
            return label, format_percent(fraction)
        if item.archived:
            return BADGE_ARCHIVED, None
        if item.archiving:
            return BADGE_ARCHIVING, format_percent(item.archive_fraction)
        return None

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

    @property
    def screen_position(self) -> int:
        """The dot clicked last (0 = the first screen); every row follows it."""
        return self._screen_position

    def screen_of(self, item: RecordingItem) -> str | None:
        """The screen in the row's thumbnail: the one at :attr:`screen_position`, or the
        row's last screen when it has fewer (two screens and the third dot clicked: the
        second; one screen: always the first)."""
        screens = self.screens_of(item)
        if not screens:
            return None
        return screens[min(self._screen_position, len(screens) - 1)]

    def set_screen_position(self, position: int) -> None:
        """Show the screen at ``position`` (0-based) in every row that has it, the last
        screen in the others."""
        position = max(0, min(int(position), MAX_DOTS - 1))
        if position == self._screen_position:
            return
        self._screen_position = position
        self._last_image.clear()  # no row keeps the picture of the screen shown before
        self.viewport().update()

    def set_screen(self, item: RecordingItem, screen_id: str) -> None:
        """The dot of ``screen_id`` in ``item``'s row was clicked: that row shows the
        screen, and every other row switches to the screen at the same position."""
        screens = self.screens_of(item)
        if screen_id in screens:
            self.set_screen_position(screens.index(screen_id))

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
        if not index.isValid() or isinstance(index.data(FolderRole), FolderHeader):
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
        header = self.header_hit(pos)
        if header is not None:
            row = self.indexAt(pos).row()
            self._set_header_hover((row, header[1]) if header[1] != "header" else None)
            self.viewport().setCursor(Qt.CursorShape.PointingHandCursor)
            return  # no drag selection starts on a header
        self._set_header_hover(None)
        on_dot = self.dot_at(pos) is not None
        self.viewport().setCursor(
            Qt.CursorShape.PointingHandCursor if on_dot else Qt.CursorShape.ArrowCursor
        )
        super().mouseMoveEvent(event)

    def _press_header(self, event: QMouseEvent) -> bool:
        """A press on a folder header: toggle, pin or menu. True when it was one."""
        pos = event.position().toPoint()
        hit = self.header_hit(pos)
        if hit is None:
            return False
        event.accept()
        if event.button() != Qt.MouseButton.LeftButton:
            return True  # the right button opens the folder menu (context menu)
        header, part, area = hit
        if part == "pin":
            self.pin_clicked.emit(header.name)
        elif part == "menu":
            self.folder_menu_requested.emit(
                header.name, self.viewport().mapToGlobal(area.bottomLeft())
            )
        else:
            self.toggle_folder(header.name)
        return True

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._press_header(event):
            return
        hit = self.dot_at(event.position().toPoint())
        if hit is not None and event.button() == Qt.MouseButton.LeftButton:
            item, screen_id = hit
            self.set_screen(item, screen_id)
            event.accept()
            return  # a dot only switches the thumbnail; it does not open the recording
        modifiers = event.modifiers()
        extend = Qt.KeyboardModifier.ShiftModifier | Qt.KeyboardModifier.ControlModifier
        index = self.indexAt(event.position().toPoint())
        current = self.currentIndex()
        if (
            modifiers & extend
            and index.isValid()
            and current.isValid()
            and self._folder_of_row(index.row()) != self._folder_of_row(current.row())
        ):
            # Shift/Ctrl into another folder: a new selection there (one folder only).
            plain = QMouseEvent(
                event.type(),
                event.position(),
                event.globalPosition(),
                event.button(),
                event.buttons(),
                modifiers & ~extend,
            )
            super().mousePressEvent(plain)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._press_header(event):  # a quick second click toggles again
            return
        super().mouseDoubleClickEvent(event)

    def dot_tooltip(self, pos: QPoint) -> str | None:
        """Tooltip of the screen dot under ``pos``: ``screen1 2560x1600``."""
        hit = self.dot_at(pos)
        return hit[0].screen_label(hit[1]) if hit is not None else None

    def header_tooltip(self, pos: QPoint) -> str | None:
        """Tooltip of a folder header: the pin, ``⋯`` or the folder itself."""
        hit = self.header_hit(pos)
        if hit is None:
            return None
        header, part, _area = hit
        if part == "pin":
            return UNPIN_TOOLTIP if header.pinned else PIN_TOOLTIP
        if part == "menu":
            return FOLDER_MENU_TOOLTIP
        text = header.label
        return f"{text}\n{MAIN_FOLDER_TOOLTIP}" if header.main else text

    def viewportEvent(self, event: QEvent) -> bool:  # noqa: N802
        if event.type() == QEvent.Type.ToolTip and isinstance(event, QHelpEvent):
            header = self.header_hit(event.pos())
            if header is not None:
                text = self.header_tooltip(event.pos())
                QToolTip.showText(event.globalPos(), text or "", self.viewport(), header[2])
                return True
            hit = self._dot_hit(event.pos())
            if hit is not None:
                item, screen_id, area = hit
                # Bound to the dot: leaving it hides the tooltip, the next dot shows its own.
                text = item.screen_label(screen_id)
                QToolTip.showText(event.globalPos(), text, self.viewport(), area)
                return True
        if event.type() == QEvent.Type.Leave:
            self.clear_hover()
            self._set_header_hover(None)
            self.viewport().unsetCursor()
        return super().viewportEvent(event)

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        self.thumbnails.begin_paint()  # only the thumbnails of the visible rows are decoded
        super().paintEvent(event)

    def shutdown(self) -> None:
        self.thumbnails.shutdown()


__all__ = [
    "BADGE_ARCHIVED",
    "BADGE_ARCHIVING",
    "BADGE_TRANSCRIBING",
    "FOLDER_MENU_TOOLTIP",
    "PIN_TOOLTIP",
    "UNPIN_TOOLTIP",
    "draw_pin",
    "MIN_LIST_WIDTH",
    "RecordingDelegate",
    "RecordingsListView",
    "THUMB_H",
    "THUMB_W",
    "elided_lines",
    "format_percent",
    "separator_color",
]
