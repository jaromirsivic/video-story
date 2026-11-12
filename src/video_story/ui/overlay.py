"""On-screen drawing overlay ("Draw").

One frameless, translucent, always-on-top window is created per connected screen so
the user can draw annotations (brush strokes, rectangles, arrows) anywhere on the
desktop (rectangles, ellipses, lines, brush strokes, arrows). The windows are real,
visible top-level windows that accept mouse input, so the drawing is part of the
composited screen and therefore shows up in screenshots taken by ``mss``. Drawing is a
mode of its own: while the overlay is shown, clicks do not reach the applications
below it.

A small floating toolbar (top-left of the primary screen at first; drag its title bar
to move it whatever tool is selected, the cross closes drawing mode; black outline,
always sized to its contents) offers Sketch/Screen / Undo / Clear on the first row, the
tools Rectangle / Ellipse / Line / Brush / Arrow / Eraser (icon and key only; rectangle
is the default) on the second and the 9-color palette in one row below; every button
names its shortcut in brackets. The left mouse button draws with the selected tool (the
eraser removes every shape whose line it touches or crosses; Undo brings them back),
the right mouse button always draws an arrow. Other windows (the control window) can
be kept above the overlay with :meth:`DrawingOverlay.keep_above`; like the toolbar
they stay movable and clickable.

Sketch: the Sketch/Screen button (``S``) switches the background of every screen
between the screens as they are (the default whenever drawing mode is entered) and a
plain white canvas to sketch on; the shapes stay where they are either way. The
background is reported for the frame sidecars (:meth:`DrawingOverlay.background`) and
stored with every shape.

Keyboard shortcuts, in every Video Story window while drawing: ``Esc`` close,
``Ctrl+Z`` undo, ``C`` clear, ``S`` sketch/screen, ``R`` / ``E`` / ``L`` / ``B`` /
``A`` / ``U`` select a tool, ``1``-``9`` a color. On the overlay windows also ``0``
resets the zoom.

Zoom: drawing mode works like a canvas. When it is entered, every screen is grabbed
once; the mouse wheel zooms that screen's picture in (up to 800 %) around the cursor
and back out to 100 %, and the middle mouse button pans while zoomed. At 100 % the live
desktop shows through; zoomed, the frozen picture is shown magnified (and that is what
the recording captures). On the sketch canvas the zoom works the same way on white.
Shapes stay attached to the picture and keep their on-screen stroke width.

All geometry is kept in Qt logical "canvas" coordinates of the window the shape was
drawn in (the unzoomed screen); Qt maps them to physical pixels per screen (Windows
per-monitor DPI is handled by Qt).

For the frame sidecars the overlay also keeps a JSON-ready snapshot of the finished
shapes (:meth:`DrawingOverlay.drawings`), with every point converted to pixels of the
recorded screen image (that is, of the zoomed view while zoomed), of the shape being
drawn right now (:meth:`DrawingOverlay.drawing_now`: tool, color, screen and start time
only, no geometry) and of the zoom of every screen (:meth:`DrawingOverlay.zoom`): the
recorder reads them from its capture thread at every tick. A shape therefore enters
``drawings`` only when its mouse button is released, with its ``id`` and the
``started_ms`` / ``finished_ms`` of the press and the release (recording timeline, from
:meth:`DrawingOverlay.set_clock`); while it is dragged the frame images already show it.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from enum import StrEnum

from PySide6.QtCore import (
    QEvent,
    QLineF,
    QObject,
    QPoint,
    QPointF,
    QRectF,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QBrush,
    QColor,
    QContextMenuEvent,
    QCursor,
    QGuiApplication,
    QKeyEvent,
    QKeySequence,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPainterPathStroker,
    QPaintEvent,
    QPen,
    QPixmap,
    QPolygonF,
    QScreen,
    QShortcut,
    QWheelEvent,
)
from PySide6.QtWidgets import (
    QAbstractButton,
    QApplication,
    QButtonGroup,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLayout,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from video_story.core.screens import Screen

from .icons import (
    arrow_icon,
    draw_icon,
    ellipse_icon,
    eraser_icon,
    line_icon,
    rectangle_icon,
)
from .palette import DEFAULT_COLOR, PALETTE, PaletteColor

log = logging.getLogger(__name__)

STROKE_WIDTH = 4.0  # logical pixels
ARROW_HEAD_LENGTH = 18.0  # logical pixels
ARROW_HEAD_HALF_ANGLE = 26.0  # degrees between the shaft and each side of the head
TOOLBAR_MARGIN = 16  # logical pixels from the top-left corner of the primary screen
HIT_TEST_FILL = QColor(0, 0, 0, 1)  # see OverlayWindow.paintEvent
MIN_ZOOM = 1.0
MAX_ZOOM = 8.0  # 800 %
ZOOM_STEP = 1.25  # zoom factor per wheel notch (120 units of angle delta)
TOOLBAR_RAISE_DELAYS_MS = (0, 150)  # re-raise the toolbar after window activation settles
PALETTE_COLUMNS = 9  # the 9 colors are shown in one row
TOOL_ICON_SIZE = 18  # logical pixels of the tool icons
TOOL_ICON_INK = QColor("#000000")  # the toolbar has fixed light colors in every theme
ERASER_TOLERANCE = 6.0  # on-screen pixels around a shape's line that the eraser hits
SKETCH_COLOR = QColor("#FFFFFF")  # the sketch canvas
UNDO_SHORTCUT = "Ctrl+Z"
CLEAR_SHORTCUT = "C"
SKETCH_SHORTCUT = "S"
SKETCH_LABEL = "Sketch"  # the button while the screens are shown (switches to sketch)
SCREEN_LABEL = "Screen"  # the button while sketching (switches back to the screens)


class Tool(StrEnum):
    BRUSH = "brush"
    RECTANGLE = "rectangle"
    ELLIPSE = "ellipse"
    LINE = "line"
    ARROW = "arrow"
    ERASER = "eraser"  # removes shapes; never a shape itself


class Background(StrEnum):
    """What the shapes are drawn on: the screens as they are, or the white canvas."""

    SCREEN = "screen"
    SKETCH = "sketch"


DEFAULT_TOOL = Tool.RECTANGLE  # the tool selected when the overlay is created
# Tool buttons, left to right: (tool, name, key, tooltip text).
TOOL_BUTTONS: tuple[tuple[Tool, str, str, str], ...] = (
    (Tool.RECTANGLE, "Rectangle", "R", "drag from corner to corner"),
    (Tool.ELLIPSE, "Ellipse", "E", "drag from corner to corner of its bounding box"),
    (Tool.LINE, "Line", "L", "drag from start to end"),
    (Tool.BRUSH, "Brush", "B", "freehand line"),
    (Tool.ARROW, "Arrow", "A", "drag from tail to head"),
    (Tool.ERASER, "Eraser", "U", "drag across the line of a shape to remove it (Ctrl+Z undoes)"),
)
TOOL_KEYS = {key: tool for tool, _name, key, _tip in TOOL_BUTTONS}


@dataclass
class Shape:
    """One drawn annotation in the logical coordinates of its overlay window.

    ``points`` holds the polyline for a brush stroke and ``[start, end]`` for a
    rectangle or an ellipse (two opposite corners of the bounding box), a line (start,
    end) or an arrow (tail, head). ``id`` (1, 2, ... in the order the shapes were
    finished; 0 until then), ``started_ms`` / ``finished_ms`` (the press and the release
    on the recording timeline; ``None`` without a clock) and ``background`` (what it was
    finished on) are filled in by :class:`DrawingOverlay`.
    """

    tool: Tool
    color: QColor
    points: list[QPointF] = field(default_factory=list)
    screen_index: int = 0
    id: int = 0
    started_ms: int | None = None
    finished_ms: int | None = None
    background: Background = Background.SCREEN

    def is_degenerate(self) -> bool:
        if self.tool is Tool.BRUSH:
            return not self.points
        if len(self.points) < 2:
            return True
        return (self.points[0] - self.points[-1]).manhattanLength() < 1.0

    def bounding_rect(self) -> QRectF:
        xs = [p.x() for p in self.points]
        ys = [p.y() for p in self.points]
        return QRectF(QPointF(min(xs), min(ys)), QPointF(max(xs), max(ys)))


def _screen_for_window(qscreen: QScreen, screens: list[Screen]) -> Screen | None:
    """The recorded screen shown by ``qscreen``: same top-left origin, else the nearest.

    Qt keeps the native position of every screen's top-left corner in its logical
    geometry (Windows, X11) or uses the same points as CoreGraphics (macOS), so origins
    match the platform layer's screen list; sizes may differ by the device pixel ratio.
    """
    if not screens:
        return None
    origin = qscreen.geometry().topLeft()
    for screen in screens:
        if screen.x == origin.x() and screen.y == origin.y():
            return screen
    return min(screens, key=lambda s: abs(s.x - origin.x()) + abs(s.y - origin.y()))


def shape_to_json(shape: Shape, screen: Screen, device_pixel_ratio: float) -> dict[str, object]:
    """Describe one shape in pixels of ``screen``'s image (origin top-left).

    Rectangles, ellipses, lines and arrows carry ``from`` and ``to`` (two opposite
    corners of the bounding box, start and end, or tail and head); brush strokes carry
    ``path``. Every point is ``{screen, x, y}`` like the cursor entry of a sidecar.
    ``id``, ``started_ms`` and ``finished_ms`` say which shape it is and when it was
    drawn (press and release on the recording timeline); ``background`` whether it was
    drawn on the screens (``screen``) or on the white sketch canvas (``sketch``).
    """

    def point(p: QPointF) -> dict[str, object]:
        x = min(max(0, round(p.x() * device_pixel_ratio)), screen.image_width - 1)
        y = min(max(0, round(p.y() * device_pixel_ratio)), screen.image_height - 1)
        return {"screen": screen.id, "x": x, "y": y}

    data: dict[str, object] = {
        "id": shape.id,
        "tool": str(shape.tool),
        "color": shape.color.name().upper(),
        "screen": screen.id,
        "stroke_width": round(STROKE_WIDTH * device_pixel_ratio),
        "started_ms": shape.started_ms,
        "finished_ms": shape.finished_ms,
        "background": str(shape.background),
    }
    if shape.tool is Tool.BRUSH:
        path: list[dict[str, object]] = []
        for p in shape.points:
            entry = point(p)
            if not path or path[-1] != entry:  # a press stores its point twice
                path.append(entry)
        data["path"] = path
    else:
        data["from"] = point(shape.points[0])
        data["to"] = point(shape.points[-1])
    return data


def _arrow_head(tail: QPointF, head: QPointF) -> QPolygonF | None:
    shaft = QLineF(tail, head)
    if shaft.length() < 1.0:
        return None
    angle = shaft.angle()
    left = QLineF.fromPolar(ARROW_HEAD_LENGTH, angle + 180 - ARROW_HEAD_HALF_ANGLE).p2()
    right = QLineF.fromPolar(ARROW_HEAD_LENGTH, angle + 180 + ARROW_HEAD_HALF_ANGLE).p2()
    return QPolygonF([head, head + left, head + right])


def paint_shape(painter: QPainter, shape: Shape) -> None:
    """Paint one shape with the overlay's stroke style (4 px, round caps/joins)."""
    if shape.is_degenerate():
        return
    pen = QPen(shape.color, STROKE_WIDTH)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    if shape.tool is Tool.BRUSH:
        if len(shape.points) == 1:
            painter.drawPoint(shape.points[0])
        else:
            painter.drawPolyline(QPolygonF(shape.points))
    elif shape.tool is Tool.RECTANGLE:
        painter.drawRect(QRectF(shape.points[0], shape.points[-1]).normalized())
    elif shape.tool is Tool.ELLIPSE:
        painter.drawEllipse(QRectF(shape.points[0], shape.points[-1]).normalized())
    elif shape.tool is Tool.LINE:
        painter.drawLine(QLineF(shape.points[0], shape.points[-1]))
    elif shape.tool is Tool.ARROW:
        tail, head = shape.points[0], shape.points[-1]
        polygon = _arrow_head(tail, head)
        if polygon is None:
            return
        shaft = QLineF(tail, head)
        # Shorten the shaft so its round cap does not poke out of the head's tip.
        shaft.setLength(max(0.0, shaft.length() - ARROW_HEAD_LENGTH * 0.6))
        painter.drawLine(shaft)
        painter.setBrush(QBrush(shape.color))
        painter.drawPolygon(polygon)


def shape_outline(shape: Shape) -> QPainterPath:
    """The line of ``shape`` as an unfilled path (canvas coordinates): what the eraser hits.

    Rectangles and ellipses are their outlines only, so a stroke inside one leaves it
    alone; an arrow includes its head; a brush stroke is its polyline.
    """
    path = QPainterPath()
    points = shape.points
    if not points:
        return path
    if shape.tool is Tool.BRUSH:
        path.moveTo(points[0])
        for point in points[1:]:
            path.lineTo(point)
        if len(points) == 1 or all(p == points[0] for p in points):
            path.lineTo(points[0] + QPointF(0.01, 0.0))  # a dot still has a line
    elif shape.tool is Tool.RECTANGLE:
        path.addRect(QRectF(points[0], points[-1]).normalized())
    elif shape.tool is Tool.ELLIPSE:
        path.addEllipse(QRectF(points[0], points[-1]).normalized())
    else:  # line, arrow
        path.moveTo(points[0])
        path.lineTo(points[-1])
        if shape.tool is Tool.ARROW:
            head = _arrow_head(points[0], points[-1])
            if head is not None:
                path.addPolygon(head)
                path.closeSubpath()
    return path


def _stroked(path: QPainterPath, width: float) -> QPainterPath:
    stroker = QPainterPathStroker()
    stroker.setWidth(max(width, 0.01))
    stroker.setCapStyle(Qt.PenCapStyle.RoundCap)
    stroker.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    return stroker.createStroke(path)


def erase_hits(shapes: list[Shape], start: QPointF, end: QPointF, reach: float) -> list[Shape]:
    """The shapes whose line the eraser segment ``start``-``end`` touches or crosses.

    ``reach`` is the half width of the band around every shape's line that counts as a
    hit (canvas units): half the painted stroke plus the eraser's tolerance.
    """
    segment = QPainterPath(start)
    segment.lineTo(end if end != start else start + QPointF(0.01, 0.0))
    eraser = _stroked(segment, 1.0)
    hits: list[Shape] = []
    for shape in shapes:
        if shape.is_degenerate():
            continue
        band = _stroked(shape_outline(shape), 2.0 * reach)
        if band.intersects(eraser):
            hits.append(shape)
    return hits


@dataclass
class _FloatingDrag:
    """A left-button press on the spot of a floating window lying below the overlay."""

    window: QWidget
    press: QPoint  # global position of the press
    offset: QPoint  # press position relative to the window's frame
    moved: bool = False  # False while it is still a click


def _overlay_window_flags() -> Qt.WindowType:
    flags = (
        Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
    )
    if sys.platform.startswith("linux"):
        flags |= Qt.WindowType.X11BypassWindowManagerHint
    return flags


class OverlayWindow(QWidget):
    """One transparent full-screen drawing surface; owns the shapes drawn on it."""

    def __init__(self, controller: DrawingOverlay, screen_index: int) -> None:
        super().__init__(None, _overlay_window_flags())
        self._controller = controller
        self.screen_index = screen_index
        self._shapes: list[Shape] = []  # canvas coordinates
        self._current: Shape | None = None
        self._snapshot: QPixmap | None = None  # the screen as it was when drawing started
        self._zoom = MIN_ZOOM
        self._offset = QPointF(0.0, 0.0)  # canvas point shown at the window's top-left
        self._pan_anchor: tuple[QPointF, QPointF] | None = None  # (view press, offset)
        self._draw_button = Qt.MouseButton.NoButton  # the button dragging self._current
        self._floating_drag = False  # a press on a floating window's spot moves that window
        self._erase_last: QPointF | None = None  # canvas point of an eraser drag so far
        self._erased: list[tuple[int, Shape]] = []  # (index, shape) removed by that drag
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.restore_cursor()
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setWindowTitle(f"Video Story drawing overlay {screen_index + 1}")

    def restore_cursor(self) -> None:
        """The cursor of the selected tool: a cross, or the eraser's circle."""
        if self._controller.tool is Tool.ERASER:
            self.setCursor(_eraser_cursor())
        else:
            self.setCursor(Qt.CursorShape.CrossCursor)

    @property
    def erasing(self) -> bool:
        return self._erase_last is not None

    # -- shapes -------------------------------------------------------------------

    @property
    def shapes(self) -> list[Shape]:
        return list(self._shapes)

    @property
    def painted_shapes(self) -> list[Shape]:
        """Committed shapes plus the one being dragged (everything visible on screen)."""
        shapes = list(self._shapes)
        if self._current is not None:
            shapes.append(self._current)
        return shapes

    @property
    def current_shape(self) -> Shape | None:
        """The shape being dragged right now (not finished, so not in :attr:`shapes`)."""
        return self._current

    def view_shapes(self, include_current: bool = True) -> list[Shape]:
        """Visible shapes mapped to window (view) coordinates; off-view shapes are left out.

        ``include_current=False`` leaves out the shape still being dragged (the sidecars
        describe finished shapes only).
        """
        shapes = self.painted_shapes if include_current else self.shapes
        if self._zoom == MIN_ZOOM:
            return shapes
        margin = STROKE_WIDTH + 1.0
        view = QRectF(self.rect()).adjusted(-margin, -margin, margin, margin)
        result: list[Shape] = []
        for shape in shapes:
            if not shape.points:
                continue
            points = [self.to_view(p) for p in shape.points]
            mapped = replace(shape, points=points)
            if mapped.bounding_rect().adjusted(-1, -1, 1, 1).intersects(view):
                result.append(mapped)
        return result

    # -- zoom -----------------------------------------------------------------------

    @property
    def zoom(self) -> float:
        return self._zoom

    @property
    def offset(self) -> QPointF:
        return QPointF(self._offset)

    def visible_canvas_rect(self) -> QRectF:
        """The part of the (unzoomed) screen shown in the window, in canvas coordinates."""
        return QRectF(
            self._offset.x(),
            self._offset.y(),
            self.width() / self._zoom,
            self.height() / self._zoom,
        )

    def to_canvas(self, pos: QPointF) -> QPointF:
        return QPointF(
            pos.x() / self._zoom + self._offset.x(), pos.y() / self._zoom + self._offset.y()
        )

    def to_view(self, point: QPointF) -> QPointF:
        return QPointF(
            (point.x() - self._offset.x()) * self._zoom,
            (point.y() - self._offset.y()) * self._zoom,
        )

    def set_snapshot(self, pixmap: QPixmap | None) -> None:
        """The frozen picture of the screen shown while zoomed (``None``: zoom disabled)."""
        self._snapshot = pixmap if pixmap is not None and not pixmap.isNull() else None
        if self._snapshot is None:
            self.reset_zoom()

    def has_snapshot(self) -> bool:
        return self._snapshot is not None

    def can_zoom(self) -> bool:
        """Zooming needs a picture to magnify: the screen grab, or the white sketch canvas."""
        return self._snapshot is not None or self._controller.sketch

    def reset_zoom(self) -> None:
        self.set_zoom(MIN_ZOOM)

    def set_zoom(self, zoom: float, anchor: QPointF | None = None) -> None:
        """Zoom to ``zoom`` keeping the canvas point under ``anchor`` (view coords) in place."""
        zoom = min(max(zoom, MIN_ZOOM), MAX_ZOOM)
        if not self.can_zoom():
            zoom = MIN_ZOOM
        if anchor is None:
            anchor = QPointF(self.width() / 2, self.height() / 2)
        fixed = self.to_canvas(anchor)
        self._zoom = zoom
        self._set_offset(QPointF(fixed.x() - anchor.x() / zoom, fixed.y() - anchor.y() / zoom))
        if zoom == MIN_ZOOM and self._pan_anchor is not None:
            self._pan_anchor = None
            self.restore_cursor()
        self.update()
        self._controller._refresh_drawings()

    def _set_offset(self, offset: QPointF) -> None:
        max_x = max(0.0, self.width() - self.width() / self._zoom)
        max_y = max(0.0, self.height() - self.height() / self._zoom)
        self._offset = QPointF(min(max(offset.x(), 0.0), max_x), min(max(offset.y(), 0.0), max_y))

    def add_shape(self, shape: Shape) -> None:
        shape.screen_index = self.screen_index
        self._shapes.append(shape)
        self.update()
        self._controller._refresh_drawings()

    def remove_shape(self, shape: Shape) -> bool:
        """Take ``shape`` (this very object) off the window; False when it is not here."""
        for index, candidate in enumerate(self._shapes):
            if candidate is shape:
                del self._shapes[index]
                self.update()
                self._controller._refresh_drawings()
                return True
        return False

    def restore_shapes(self, removed: list[tuple[int, Shape]]) -> None:
        """Put back shapes removed as ``(index, shape)`` in that order (the eraser's undo)."""
        for index, shape in reversed(removed):
            self._shapes.insert(min(index, len(self._shapes)), shape)
        if removed:
            self.update()
            self._controller._refresh_drawings()

    def _erase_to(self, pos: QPointF) -> None:
        """Remove every shape whose line the eraser crossed from the last point to ``pos``."""
        start = self._erase_last if self._erase_last is not None else pos
        self._erase_last = pos
        reach = (STROKE_WIDTH / 2 + ERASER_TOLERANCE) / self._zoom
        hits = erase_hits(self._shapes, start, pos, reach)
        if not hits:
            return
        for shape in hits:
            index = next(i for i, s in enumerate(self._shapes) if s is shape)
            del self._shapes[index]
            self._erased.append((index, shape))
        self.update()
        self._controller._refresh_drawings()

    def _finish_erase(self) -> None:
        erased, self._erased = self._erased, []
        self._erase_last = None
        self._draw_button = Qt.MouseButton.NoButton
        if erased:
            self._controller._record_erase(self, erased)

    def cancel_gesture(self) -> None:
        """Drop a drag in progress (the overlay is hidden before the button is released)."""
        self._floating_drag = False
        if self._erase_last is not None:
            self._finish_erase()  # what the eraser removed so far stays removed (undoable)
        self._draw_button = Qt.MouseButton.NoButton
        if self._pan_anchor is not None:
            self._pan_anchor = None
            self.restore_cursor()
        if self._current is not None:
            self._current = None
            self.update()
            self._controller._refresh_drawings()

    def clear_shapes(self) -> None:
        self._erase_last = None
        self._erased = []
        if self._shapes or self._current is not None:
            self._shapes.clear()
            self._current = None
            self._draw_button = Qt.MouseButton.NoButton
            self.update()
            self._controller._refresh_drawings()

    # -- Qt events ----------------------------------------------------------------

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        painter = QPainter(self)
        # Windows hit-tests layered (translucent) windows per pixel: fully transparent
        # pixels would let clicks fall through to the applications below. An alpha of
        # 1/255 keeps every pixel "owned" by the overlay while being invisible to the
        # eye and to screen capture (at most +-1 on a channel).
        painter.fillRect(self.rect(), HIT_TEST_FILL)
        if self._controller.sketch:
            # Sketch: a plain white canvas hides the screen (zoomed or not).
            painter.fillRect(self.rect(), SKETCH_COLOR)
        elif self._zoom > MIN_ZOOM and self._snapshot is not None:
            # Zoomed: show the frozen picture magnified, with crisp (unsmoothed) pixels.
            scale_x = self._snapshot.width() / max(1, self.width())
            scale_y = self._snapshot.height() / max(1, self.height())
            visible = self.visible_canvas_rect()
            source = QRectF(
                visible.x() * scale_x,
                visible.y() * scale_y,
                visible.width() * scale_x,
                visible.height() * scale_y,
            )
            painter.drawPixmap(QRectF(self.rect()), self._snapshot, source)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        for shape in self.view_shapes():
            paint_shape(painter, shape)
        painter.end()

    def event(self, event: QEvent) -> bool:
        if event.type() == QEvent.Type.WindowActivate:
            # Activating an overlay window puts it above the toolbar (both stay on top);
            # the overlay is invisible but would swallow the clicks meant for the toolbar.
            self._controller._schedule_toolbar_raise()
        return super().event(event)

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802
        steps = event.angleDelta().y() / 120.0
        if not steps or not self.can_zoom() or self._current is not None or self.erasing:
            event.ignore()
            return
        self.set_zoom(self._zoom * (ZOOM_STEP**steps), event.position())
        event.accept()

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        button = event.button()
        busy = (
            self._current is not None
            or self._floating_drag
            or self._pan_anchor is not None
            or self.erasing
        )
        if button == Qt.MouseButton.MiddleButton:
            if self._zoom > MIN_ZOOM and not busy:
                self._pan_anchor = (event.position(), QPointF(self._offset))
                self.setCursor(Qt.CursorShape.ClosedHandCursor)
                event.accept()
            else:
                event.ignore()
            return
        if button not in (Qt.MouseButton.LeftButton, Qt.MouseButton.RightButton) or busy:
            event.ignore()
            return
        global_pos = event.globalPosition().toPoint()
        floating = self._controller._floating_at(global_pos)
        if floating is not None:
            # A floating window (toolbar, control window) sits right here but below this
            # window: never draw under it. The left button moves it (or clicks the
            # button under the cursor), whatever tool is selected.
            if button == Qt.MouseButton.LeftButton:
                self._floating_drag = True
                self._controller._begin_floating_drag(floating, global_pos)
            event.accept()
            return
        self._controller._raise_floating()
        # The left button draws with the selected tool, the right button always an arrow.
        tool = self._controller.tool if button == Qt.MouseButton.LeftButton else Tool.ARROW
        pos = self.to_canvas(event.position())
        self._draw_button = button
        if tool is Tool.ERASER:
            self._erased = []
            self._erase_to(pos)  # a click right on a line removes that shape too
            event.accept()
            return
        self._current = Shape(
            tool,
            QColor(self._controller.color),
            [pos, pos],
            self.screen_index,
            started_ms=self._controller._now_ms(),
        )
        self.update()
        self._controller._refresh_drawings()  # drawing_now: a shape is being drawn
        event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._pan_anchor is not None and event.buttons() & Qt.MouseButton.MiddleButton:
            press, offset = self._pan_anchor
            delta = event.position() - press
            self._set_offset(
                QPointF(offset.x() - delta.x() / self._zoom, offset.y() - delta.y() / self._zoom)
            )
            self.update()
            self._controller._refresh_drawings()
            event.accept()
            return
        if self._floating_drag and event.buttons() & Qt.MouseButton.LeftButton:
            self._controller._drag_floating_to(event.globalPosition().toPoint())
            event.accept()
            return
        if self.erasing:
            if event.buttons() & self._draw_button:
                self._erase_to(self.to_canvas(event.position()))
                event.accept()
            else:
                event.ignore()
            return
        if self._current is None or not (event.buttons() & self._draw_button):
            event.ignore()
            return
        pos = self.to_canvas(event.position())
        if self._current.tool is Tool.BRUSH:
            self._current.points.append(pos)
        else:
            self._current.points[-1] = pos
        # Only the screen changes: the sidecars describe the shape once it is finished.
        self.update()
        event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.MiddleButton and self._pan_anchor is not None:
            self._pan_anchor = None
            self.restore_cursor()
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton and self._floating_drag:
            self._floating_drag = False
            self._controller._end_floating_drag(event.globalPosition().toPoint())
            event.accept()
            return
        if self.erasing and event.button() == self._draw_button:
            self._erase_to(self.to_canvas(event.position()))
            self._finish_erase()
            event.accept()
            return
        if self._current is None or event.button() != self._draw_button:
            event.ignore()
            return
        shape = self._current
        self._current = None
        self._draw_button = Qt.MouseButton.NoButton
        pos = self.to_canvas(event.position())
        if shape.tool is Tool.BRUSH:
            if pos != shape.points[-1]:
                shape.points.append(pos)
        else:
            shape.points[-1] = pos
        shape.finished_ms = self._controller._now_ms()
        if shape.is_degenerate():
            self.update()
            self._controller._refresh_drawings()
        else:
            self._controller._commit_shape(self, shape)
        event.accept()

    def contextMenuEvent(self, event: QContextMenuEvent) -> None:  # noqa: N802
        event.accept()  # the right button draws arrows; there is no context menu

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        # The shortcuts are application-wide shortcuts of the controller (they work
        # whichever Video Story window has the focus); they are handled here too in case
        # a key event reaches the overlay without passing the shortcut map.
        key = event.key()
        modifiers = event.modifiers()
        ctrl = bool(modifiers & Qt.KeyboardModifier.ControlModifier)
        alt = bool(modifiers & Qt.KeyboardModifier.AltModifier)
        plain = not ctrl and not alt
        letter = chr(key) if ord("A") <= key <= ord("Z") else ""
        if key == Qt.Key.Key_Escape:
            self._controller._user_close()
        elif key == Qt.Key.Key_Z and ctrl:
            self._controller.undo()
        elif letter == CLEAR_SHORTCUT and plain:
            self._controller.clear()
        elif letter == SKETCH_SHORTCUT and plain:
            self._controller.toggle_sketch()
        elif letter in TOOL_KEYS and plain:
            self._controller.set_tool(TOOL_KEYS[letter])
        elif key == Qt.Key.Key_0 and plain:
            self.reset_zoom()
        elif Qt.Key.Key_1 <= key < Qt.Key.Key_1 + len(PALETTE) and plain:
            self._controller.set_color(PALETTE[key - Qt.Key.Key_1])
        else:
            super().keyPressEvent(event)
            return
        event.accept()

    def closeEvent(self, event) -> None:  # noqa: N802
        # Alt+F4 and similar: leave drawing mode like Esc instead of destroying the window.
        if self._controller.is_visible():
            event.ignore()
            self._controller._user_close()
        else:
            event.accept()


class OverlayToolbar(QWidget):
    """Floating tool window: Sketch/Screen / Undo / Clear, the tools and the palette."""

    tool_selected = Signal(object)  # Tool
    color_selected = Signal(object)  # PaletteColor
    sketch_requested = Signal()
    undo_requested = Signal()
    clear_requested = Signal()
    close_requested = Signal()

    def __init__(self) -> None:
        super().__init__(
            None,
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowDoesNotAcceptFocus,
        )
        self.setObjectName("overlayToolbar")
        self.setWindowTitle("Video Story drawing tools")
        # We paint the toolbar ourselves (frameless): the style sheet must draw its
        # background and the black outline that keeps it visible on white backgrounds.
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        # Explicit colors: the toolbar must look the same in light and dark system themes.
        self.setStyleSheet(
            "QWidget#overlayToolbar { background: #F4F4F4; border: 2px solid #000000; }"
            "QWidget#overlayToolbar QToolButton, QWidget#overlayToolbar QPushButton {"
            " background: #FFFFFF; color: #000000; border: 1px solid #909090;"
            " border-radius: 3px; padding: 3px 8px; }"
            "QWidget#overlayToolbar QToolButton:hover, QWidget#overlayToolbar QPushButton:hover {"
            " background: #E8F0FF; }"
            "QWidget#overlayToolbar QToolButton:checked {"
            " background: #FFD27F; border: 1px solid #A06000; }"
            "QWidget#overlayToolbarHeader { background: #DCDCDC;"
            " border-bottom: 1px solid #B0B0B0; }"
            "QLabel#overlayToolbarTitle { color: #303030; font-weight: bold; }"
            "QWidget#overlayToolbar QToolButton#overlayToolbarCross {"
            " background: transparent; border: none; padding: 1px 6px; color: #404040; }"
            "QWidget#overlayToolbar QToolButton#overlayToolbarCross:hover {"
            " background: #E81123; color: #FFFFFF; }"
        )

        self._tool_buttons: dict[Tool, QToolButton] = {}
        self._swatches: dict[str, QToolButton] = {}
        self._current_hex = DEFAULT_COLOR.hex.upper()
        self._drag_offset: QPoint | None = None
        self.user_moved = False  # True once the user dragged the toolbar somewhere

        outer = QVBoxLayout(self)
        outer.setContentsMargins(2, 2, 2, 2)
        outer.setSpacing(0)
        # The toolbar is always exactly as large as its contents. Without this, moving
        # it between screens with different DPI scaling lets Windows' suggested
        # (scaled) geometry stick, and the toolbar grows a little more on every move.
        outer.setSizeConstraint(QLayout.SizeConstraint.SetFixedSize)

        # Title bar: a grab strip across the whole top; drag it (or any free spot of the
        # toolbar) to move the toolbar whatever tool is selected; the cross leaves
        # drawing mode like Close. QWidget and QLabel pass presses on to the toolbar.
        self.header = QWidget(self)
        self.header.setObjectName("overlayToolbarHeader")
        self.header.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.header.setCursor(Qt.CursorShape.SizeAllCursor)
        self.header.setToolTip("Drag to move the toolbar")
        header = QHBoxLayout(self.header)
        header.setContentsMargins(6, 2, 2, 2)
        header.setSpacing(4)
        title = QLabel("Drawing tools", self.header)
        title.setObjectName("overlayToolbarTitle")
        header.addWidget(title, 1)
        self.close_cross = QToolButton(self.header)
        self.close_cross.setObjectName("overlayToolbarCross")
        self.close_cross.setText("✕")
        self.close_cross.setToolTip("Leave drawing mode (Esc)")
        self.close_cross.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.close_cross.setAutoRaise(True)
        self.close_cross.clicked.connect(self.close_requested.emit)
        self.close_cross.setCursor(Qt.CursorShape.ArrowCursor)
        header.addWidget(self.close_cross)
        outer.addWidget(self.header)

        root = QVBoxLayout()
        root.setContentsMargins(5, 4, 5, 5)
        root.setSpacing(4)
        outer.addLayout(root)

        # Every button shows its keyboard shortcut in brackets (the tools only that).
        # Row 1: Sketch/Screen / Undo / Clear, each two columns wide; row 2: the six
        # tools, one column each.
        buttons = QGridLayout()
        buttons.setSpacing(4)
        self.sketch_button = QPushButton(self)
        self.set_sketch(False)
        actions: list[tuple[QPushButton, Signal]] = [(self.sketch_button, self.sketch_requested)]
        for label, shortcut, tip, signal in (
            ("Undo", UNDO_SHORTCUT, "Undo the last shape or eraser stroke", self.undo_requested),
            ("Clear", CLEAR_SHORTCUT, "Remove all drawings", self.clear_requested),
        ):
            button = QPushButton(f"{label} ({shortcut})", self)
            button.setToolTip(f"{tip} ({shortcut})")
            actions.append((button, signal))
        for column, (button, signal) in enumerate(actions):
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            button.clicked.connect(signal.emit)
            buttons.addWidget(button, 0, 2 * column, 1, 2)
        icons = {
            Tool.RECTANGLE: rectangle_icon,
            Tool.ELLIPSE: ellipse_icon,
            Tool.LINE: line_icon,
            Tool.BRUSH: draw_icon,
            Tool.ARROW: arrow_icon,
            Tool.ERASER: eraser_icon,
        }
        group = QButtonGroup(self)
        group.setExclusive(True)
        for column, (tool, name, key, tip) in enumerate(TOOL_BUTTONS):
            button = QToolButton(self)
            button.setText(f"({key})")
            button.setIcon(icons[tool](TOOL_ICON_SIZE, TOOL_ICON_INK))
            button.setIconSize(QSize(TOOL_ICON_SIZE, TOOL_ICON_SIZE))
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            button.setToolTip(
                f"{name} ({key}): {tip}; the right mouse button always draws an arrow"
            )
            button.setCheckable(True)
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            button.clicked.connect(lambda _checked=False, t=tool: self.tool_selected.emit(t))
            group.addButton(button)
            buttons.addWidget(button, 1, column)
            self._tool_buttons[tool] = button
        # Equal columns: wide enough for every tool and for half of every action button
        # (the Sketch button with either of its texts).
        widest_tool = max(b.sizeHint().width() for b in self._tool_buttons.values())
        widest_action = max(b.sizeHint().width() for b, _signal in actions)
        self.set_sketch(True)
        widest_action = max(widest_action, self.sketch_button.sizeHint().width())
        self.set_sketch(False)
        column_width = max(widest_tool, (widest_action - buttons.spacing() + 1) // 2)
        for column in range(len(TOOL_BUTTONS)):
            buttons.setColumnStretch(column, 1)
            buttons.setColumnMinimumWidth(column, column_width)
        root.addLayout(buttons)

        # The palette: one row of nine swatches, each labelled with its number key.
        grid = QGridLayout()
        grid.setSpacing(4)
        for index, entry in enumerate(PALETTE):
            swatch = QToolButton(self)
            swatch.setText(str(index + 1))
            swatch.setMinimumSize(30, 28)
            swatch.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            swatch.setToolTip(f"{entry.name} ({index + 1})")
            swatch.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            swatch.clicked.connect(lambda _checked=False, e=entry: self.color_selected.emit(e))
            grid.addWidget(swatch, index // PALETTE_COLUMNS, index % PALETTE_COLUMNS)
            self._swatches[entry.hex.upper()] = swatch
        root.addLayout(grid)

        self.set_tool(DEFAULT_TOOL)
        self.set_color(DEFAULT_COLOR.hex)

    # -- moving the toolbar ---------------------------------------------------------
    # Clicks on buttons and swatches are consumed by them; a press anywhere else
    # (title bar, margins, gaps) starts a drag.

    def begin_drag(self, global_pos: QPoint) -> None:
        self.raise_()
        self._drag_offset = global_pos - self.frameGeometry().topLeft()

    def drag_to(self, global_pos: QPoint) -> None:
        if self._drag_offset is None:
            return
        self.move(global_pos - self._drag_offset)
        self.user_moved = True

    def end_drag(self) -> None:
        self._drag_offset = None

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        self.begin_drag(event.globalPosition().toPoint())
        event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._drag_offset is None or not (event.buttons() & Qt.MouseButton.LeftButton):
            super().mouseMoveEvent(event)
            return
        self.drag_to(event.globalPosition().toPoint())
        event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and self._drag_offset is not None:
            self.end_drag()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def set_tool(self, tool: Tool) -> None:
        button = self._tool_buttons[tool]
        if not button.isChecked():
            button.setChecked(True)

    def set_sketch(self, sketch: bool) -> None:
        """The button names what a click switches to: Sketch on the screens, Screen while
        sketching."""
        if sketch:
            self.sketch_button.setText(f"{SCREEN_LABEL} ({SKETCH_SHORTCUT})")
            self.sketch_button.setToolTip(
                f"Show the screens again instead of the white canvas ({SKETCH_SHORTCUT}); "
                "the drawings stay"
            )
        else:
            self.sketch_button.setText(f"{SKETCH_LABEL} ({SKETCH_SHORTCUT})")
            self.sketch_button.setToolTip(
                f"Sketch on a white canvas on every screen ({SKETCH_SHORTCUT}); the drawings stay"
            )

    def set_color(self, hex_value: str) -> None:
        self._current_hex = hex_value.upper()
        for swatch_hex, swatch in self._swatches.items():
            selected = swatch_hex == self._current_hex
            border = "3px solid #000000" if selected else "1px solid #808080"
            if selected and swatch_hex in ("#000000", "#000080", "#8B0000"):
                border = "3px solid #FFFFFF"
            text = "#FFFFFF" if _is_dark(swatch_hex) else "#000000"
            swatch.setStyleSheet(
                f"QToolButton {{ background: {swatch_hex}; color: {text}; border: {border};"
                " font-weight: bold; padding: 0px; }"
            )


@dataclass
class _Action:
    """One undoable step: ``shape`` drawn on ``window``, or the ``erased`` (index, shape)
    pairs one eraser drag removed from it."""

    window: OverlayWindow
    shape: Shape | None = None
    erased: list[tuple[int, Shape]] = field(default_factory=list)


_ERASER_CURSOR: QCursor | None = None


def _eraser_cursor() -> QCursor:
    """A small circle the size of the eraser's reach, centred on the hot spot."""
    global _ERASER_CURSOR
    if _ERASER_CURSOR is None:
        radius = round(STROKE_WIDTH / 2 + ERASER_TOLERANCE)
        size = 2 * radius + 4
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        center = QPointF(size / 2, size / 2)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor("#FFFFFF"), 3.0))
        painter.drawEllipse(center, radius, radius)
        painter.setPen(QPen(QColor("#000000"), 1.5))
        painter.drawEllipse(center, radius, radius)
        painter.end()
        _ERASER_CURSOR = QCursor(pixmap, size // 2, size // 2)
    return _ERASER_CURSOR


def _is_dark(hex_value: str) -> bool:
    """True when white text reads better than black on ``hex_value``."""
    color = QColor(hex_value)
    luminance = 0.299 * color.red() + 0.587 * color.green() + 0.114 * color.blue()
    return luminance < 140


class DrawingOverlay(QObject):
    """Controller for the per-screen overlay windows and the floating toolbar.

    Create it after ``QApplication``. :meth:`show` opens one window per screen (and
    rebuilds them if the number of screens changed since the last show); :meth:`hide`
    keeps the drawings for the next :meth:`show`; :meth:`close` destroys everything.
    ``closed`` is emitted only when the user leaves drawing mode (Esc, the toolbar's
    cross, Alt+F4), never for :meth:`hide` / :meth:`close` called from code.
    """

    closed = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._tool = DEFAULT_TOOL
        self._palette_color: PaletteColor | None = DEFAULT_COLOR
        self._color = QColor(DEFAULT_COLOR.hex)
        self._sketch = False  # the white canvas instead of the screens
        self._windows: list[OverlayWindow] = []
        # What Undo takes back, oldest first: a shape drawn, or what one eraser drag removed.
        self._history: list[_Action] = []
        self._toolbar: OverlayToolbar | None = None
        self._keep_above: list[QWidget] = []  # other windows kept above the overlay
        self._floating: _FloatingDrag | None = None
        self._shortcuts: list[QShortcut] = []
        self._visible = False
        self._screens: list[Screen] = []
        self._drawings: list[dict[str, object]] = []
        self._drawing_now: dict[str, object] | None = None
        self._zoom: list[dict[str, object]] = []
        self._clock: Callable[[], int] | None = None
        self._next_id = 0  # id of the last finished shape

    # -- public API ---------------------------------------------------------------

    @property
    def tool(self) -> Tool:
        return self._tool

    @property
    def color(self) -> QColor:
        return QColor(self._color)

    @property
    def sketch(self) -> bool:
        """True while every screen shows the white sketch canvas instead of the screen."""
        return self._sketch

    def background(self) -> str | None:
        """``"screen"`` or ``"sketch"`` while drawing, ``None`` while the overlay is hidden.

        Read by the recorder from its capture thread for the frame sidecars (a plain
        attribute read, like :meth:`drawings`).
        """
        if not self._visible:
            return None
        return str(Background.SKETCH if self._sketch else Background.SCREEN)

    @property
    def windows(self) -> list[OverlayWindow]:
        return list(self._windows)

    def is_visible(self) -> bool:
        return self._visible

    def keep_above(self, window: QWidget) -> None:
        """Keep ``window`` (e.g. the control window) above the overlay while it is shown.

        Activating an overlay window (every click on it) puts it above the other
        always-on-top windows; ``window`` is raised again like the toolbar, and a press
        on its spot while it lies below the overlay moves it (or clicks its button)
        instead of drawing under it.
        """
        if window in self._keep_above:
            return
        self._keep_above.append(window)
        window.destroyed.connect(lambda _obj=None, w=window: self._forget_window(w))
        self._raise_floating()

    def _forget_window(self, window: QWidget) -> None:
        if window in self._keep_above:
            self._keep_above.remove(window)
        if self._floating is not None and self._floating.window is window:
            self._floating = None

    def show(self) -> None:
        screens = QGuiApplication.screens()
        if not screens:
            raise RuntimeError("no screens available")
        if len(self._windows) != len(screens):
            self._destroy_windows()
            self._windows = [OverlayWindow(self, index) for index in range(len(screens))]
        if not self._visible:
            # Every drawing session starts on the screens (the Sketch button).
            self._set_sketch_state(False)
            # Grab every screen before the overlay covers it: the picture to zoom into.
            for window, screen in zip(self._windows, screens, strict=True):
                window.set_snapshot(self._grab_screen(screen))
                window.reset_zoom()
        for window, screen in zip(self._windows, screens, strict=True):
            self._place_window(window, screen)
            window.show()
            window.raise_()
        toolbar = self._ensure_toolbar()
        primary = QGuiApplication.primaryScreen() or screens[0]
        toolbar.adjustSize()
        # Keep the spot the user dragged the toolbar to, as long as it is still on a screen.
        if not toolbar.user_moved or QGuiApplication.screenAt(toolbar.geometry().center()) is None:
            toolbar.setScreen(primary)
            toolbar.move(primary.geometry().topLeft() + QPoint(TOOLBAR_MARGIN, TOOLBAR_MARGIN))
            toolbar.user_moved = False
        toolbar.show()
        toolbar.raise_()
        self._visible = True
        self._enable_shortcuts(True)
        focus_window = self._window_for_screen(primary) or self._windows[0]
        focus_window.activateWindow()
        focus_window.setFocus(Qt.FocusReason.OtherFocusReason)
        # Activation raises the overlay window above the toolbar and the control window:
        # put them back on top now and again once the (asynchronous) activation settled.
        self._raise_floating()
        self._schedule_toolbar_raise()
        self._refresh_drawings()

    def hide(self) -> None:
        self._visible = False
        self._enable_shortcuts(False)
        self._floating = None
        self._set_sketch_state(False)
        if self._toolbar is not None:
            self._toolbar.end_drag()
            self._toolbar.hide()
        for window in self._windows:
            window.cancel_gesture()
            window.hide()
            window.set_snapshot(None)  # a fresh picture is grabbed at the next show()
        self._refresh_drawings()

    def close(self) -> None:
        self.hide()
        self._destroy_windows()
        if self._toolbar is not None:
            self._toolbar.deleteLater()  # deletes the shortcuts with it
            self._toolbar = None
            self._shortcuts = []

    def clear(self) -> None:
        for window in self._windows:
            window.clear_shapes()
        self._history.clear()

    def undo(self) -> None:
        """Take back the last step: remove the last shape drawn, or bring back every
        shape the last eraser drag removed (same id, same place in the drawing order)."""
        while self._history:
            action = self._history.pop()
            if action.erased:
                action.window.restore_shapes(action.erased)
                return
            if action.shape is not None and action.window.remove_shape(action.shape):
                return

    def set_tool(self, tool: Tool) -> None:
        self._tool = Tool(tool)
        if self._toolbar is not None:
            self._toolbar.set_tool(self._tool)
        for window in self._windows:
            if not window.erasing and window._pan_anchor is None:
                window.restore_cursor()

    def set_sketch(self, sketch: bool) -> None:
        """Show the white sketch canvas (``True``) or the screens on every screen.

        Nothing is removed: the shapes stay where they are on either background.
        """
        if not self._visible or sketch == self._sketch:
            return
        self._set_sketch_state(sketch)
        for window in self._windows:
            if not window.can_zoom():
                window.reset_zoom()  # back on a screen that could not be grabbed
            window.update()
        self._refresh_drawings()

    def toggle_sketch(self) -> None:
        self.set_sketch(not self._sketch)

    def _set_sketch_state(self, sketch: bool) -> None:
        self._sketch = sketch
        if self._toolbar is not None:
            self._toolbar.set_sketch(sketch)

    def set_color(self, color: QColor | PaletteColor | str) -> None:
        if isinstance(color, PaletteColor):
            self._palette_color = color
            self._color = QColor(color.hex)
        else:
            self._color = QColor(color)
            self._palette_color = None
        if self._toolbar is not None:
            self._toolbar.set_color(self._color.name().upper())

    def shapes(self) -> list[Shape]:
        """All shapes on all windows (each tagged with its window's screen index)."""
        result: list[Shape] = []
        for window in self._windows:
            result.extend(window.shapes)
        return result

    def set_screens(self, screens: list[Screen]) -> None:
        """Recorded screens (from the recorder) used to express drawings in image pixels."""
        self._screens = list(screens)
        self._refresh_drawings()

    def set_clock(self, clock: Callable[[], int] | None) -> None:
        """Time source for ``started_ms`` / ``finished_ms`` (the recording timeline)."""
        self._clock = clock

    def drawings(self) -> list[dict[str, object]]:
        """JSON-ready snapshot of the finished shapes visible on the overlay windows.

        A shape appears here only once its mouse button was released (the one being
        dragged is reported by :meth:`drawing_now`, without geometry), ordered by ``id``.
        Safe to call from any thread: the list is rebuilt on the GUI thread whenever
        the drawing changes and never mutated afterwards. Empty while the overlay is
        hidden (nothing is on screen then) or until :meth:`set_screens` was called.
        """
        return self._drawings

    def drawing_now(self) -> dict[str, object] | None:
        """``{tool, color, screen, started_ms}`` while a shape is being drawn, else ``None``.

        Thread-safety as for :meth:`drawings`.
        """
        return self._drawing_now

    def zoom(self) -> list[dict[str, object]]:
        """JSON-ready zoom state of every zoomed screen (empty when nothing is zoomed).

        Each entry is ``{screen, factor, x, y, width, height}``: the recorded image of
        that screen shows the region ``x, y, width, height`` (pixels of the unzoomed
        screen) magnified ``factor`` times. Thread-safety as for :meth:`drawings`.
        """
        return self._zoom

    def add_shape(self, shape: Shape) -> None:
        """Add a shape programmatically to the window of ``shape.screen_index``."""
        if not self._windows:
            raise RuntimeError("overlay windows do not exist; call show() first")
        if not 0 <= shape.screen_index < len(self._windows):
            raise IndexError(f"no overlay window for screen index {shape.screen_index}")
        self._commit_shape(self._windows[shape.screen_index], shape)

    # -- internals ----------------------------------------------------------------

    @staticmethod
    def _grab_screen(screen: QScreen) -> QPixmap | None:
        try:
            pixmap = screen.grabWindow(0)
        except Exception:  # pragma: no cover - zoom is optional
            return None
        return None if pixmap.isNull() else pixmap

    def _ensure_toolbar(self) -> OverlayToolbar:
        if self._toolbar is None:
            toolbar = OverlayToolbar()
            toolbar.tool_selected.connect(self.set_tool)
            toolbar.color_selected.connect(self.set_color)
            toolbar.sketch_requested.connect(self.toggle_sketch)
            toolbar.undo_requested.connect(self.undo)
            toolbar.clear_requested.connect(self.clear)
            toolbar.close_requested.connect(self._user_close)
            toolbar.set_tool(self._tool)
            toolbar.set_color(self._color.name().upper())
            toolbar.set_sketch(self._sketch)
            self._create_shortcuts(toolbar)
            self._toolbar = toolbar
        return self._toolbar

    @staticmethod
    def _place_window(window: OverlayWindow, screen: QScreen) -> None:
        window.setScreen(screen)
        window.setGeometry(screen.geometry())

    def _window_for_screen(self, screen: QScreen) -> OverlayWindow | None:
        screens = QGuiApplication.screens()
        for window, candidate in zip(self._windows, screens, strict=False):
            if candidate is screen:
                return window
        return None

    def _destroy_windows(self) -> None:
        for window in self._windows:
            window.hide()
            window.deleteLater()
        self._windows = []
        self._history.clear()

    def _now_ms(self) -> int | None:
        """Now on the recording timeline (``None`` without a clock or when it fails)."""
        clock = self._clock
        if clock is None:
            return None
        try:
            return int(clock())
        except Exception:  # pragma: no cover - a timestamp is never worth a crash
            log.exception("drawing clock failed")
            return None

    def _commit_shape(self, window: OverlayWindow, shape: Shape) -> None:
        if not shape.id:
            self._next_id += 1
            shape.id = self._next_id
        if shape.finished_ms is None:
            shape.finished_ms = self._now_ms()
        if shape.started_ms is None:
            shape.started_ms = shape.finished_ms
        shape.background = Background.SKETCH if self._sketch else Background.SCREEN
        window.add_shape(shape)
        self._history.append(_Action(window, shape=shape))

    def _record_erase(self, window: OverlayWindow, erased: list[tuple[int, Shape]]) -> None:
        """One eraser drag removed ``erased`` from ``window``: one undoable step."""
        self._history.append(_Action(window, erased=list(erased)))

    def _refresh_drawings(self) -> None:
        if not self._visible or not self._screens:
            self._drawings = []
            self._drawing_now = None
            self._zoom = []
            return
        qscreens = QGuiApplication.screens()
        result: list[dict[str, object]] = []
        drawing_now: dict[str, object] | None = None
        zoom: list[dict[str, object]] = []
        for window in self._windows:
            qscreen = (
                qscreens[window.screen_index]
                if window.screen_index < len(qscreens)
                else window.screen()
            )
            screen = _screen_for_window(qscreen, self._screens) if qscreen else None
            if screen is None:
                continue
            ratio = float(qscreen.devicePixelRatio())
            for shape in window.view_shapes(include_current=False):
                if shape.points:
                    result.append(shape_to_json(shape, screen, ratio))
            current = window.current_shape
            if current is not None and drawing_now is None:
                drawing_now = {
                    "tool": str(current.tool),
                    "color": current.color.name().upper(),
                    "screen": screen.id,
                    "started_ms": current.started_ms,
                }
            if window.zoom > MIN_ZOOM:
                visible = window.visible_canvas_rect()
                zoom.append(
                    {
                        "screen": screen.id,
                        "factor": round(window.zoom, 3),
                        "x": round(visible.x() * ratio),
                        "y": round(visible.y() * ratio),
                        "width": round(visible.width() * ratio),
                        "height": round(visible.height() * ratio),
                    }
                )
        result.sort(key=lambda d: int(d["id"]))  # type: ignore[call-overload]
        self._drawings = result  # in the order the shapes were finished
        self._drawing_now = drawing_now
        self._zoom = zoom

    def _floating_windows(self) -> list[QWidget]:
        """Visible windows kept above the overlay, bottom to top (the toolbar last)."""
        windows = [w for w in self._keep_above if w.isVisible()]
        if self._toolbar is not None and self._toolbar.isVisible():
            windows.append(self._toolbar)
        return windows

    def _raise_floating(self) -> None:
        if not self._visible:
            return
        for window in self._floating_windows():
            window.raise_()

    def _schedule_toolbar_raise(self) -> None:
        for delay in TOOLBAR_RAISE_DELAYS_MS:
            QTimer.singleShot(delay, self, self._raise_floating)

    def _floating_at(self, global_pos: QPoint) -> QWidget | None:
        """The topmost floating window whose frame contains ``global_pos``."""
        for window in reversed(self._floating_windows()):
            if window.frameGeometry().contains(global_pos):
                return window
        return None

    def _begin_floating_drag(self, window: QWidget, global_pos: QPoint) -> None:
        window.raise_()
        if window is not self._toolbar:
            self._raise_floating()  # keep the toolbar above the other floating windows
        offset = global_pos - window.frameGeometry().topLeft()
        self._floating = _FloatingDrag(window, QPoint(global_pos), offset)

    def _drag_floating_to(self, global_pos: QPoint) -> None:
        drag = self._floating
        if drag is None:
            return
        if not drag.moved:
            if (global_pos - drag.press).manhattanLength() < QApplication.startDragDistance():
                return  # still a click, not a drag
            drag.moved = True
        drag.window.move(global_pos - drag.offset)
        if drag.window is self._toolbar:
            self._toolbar.user_moved = True

    def _end_floating_drag(self, global_pos: QPoint) -> None:
        drag, self._floating = self._floating, None
        if drag is None or drag.moved:
            return
        # A click (no drag): the window is on top again now; pass the click on to the
        # button under the cursor so the user does not have to click twice.
        local = drag.window.mapFromGlobal(global_pos)
        child = drag.window.childAt(local)
        while child is not None and not isinstance(child, QAbstractButton):
            child = child.parentWidget()
        if isinstance(child, QAbstractButton) and child.isEnabled():
            QTimer.singleShot(0, child.click)  # after this mouse event has been handled

    def _create_shortcuts(self, parent: QWidget) -> None:
        """Drawing shortcuts for every Video Story window (enabled while shown).

        Esc close, Ctrl+Z undo, C clear, S sketch/screen, R / E / L / B / A / U select a
        tool and 1-9 a palette color.
        """
        bindings: list[tuple[QKeySequence, object]] = [
            (QKeySequence(Qt.Key.Key_Escape), self._user_close),
            (QKeySequence(UNDO_SHORTCUT), self.undo),
            (QKeySequence(CLEAR_SHORTCUT), self.clear),
            (QKeySequence(SKETCH_SHORTCUT), self.toggle_sketch),
        ]
        for key, tool in TOOL_KEYS.items():
            bindings.append((QKeySequence(key), lambda t=tool: self.set_tool(t)))
        for index, entry in enumerate(PALETTE):
            bindings.append((QKeySequence(str(index + 1)), lambda e=entry: self.set_color(e)))
        for sequence, slot in bindings:
            shortcut = QShortcut(sequence, parent)
            shortcut.setContext(Qt.ShortcutContext.ApplicationShortcut)
            shortcut.setEnabled(self._visible)
            shortcut.activated.connect(slot)
            self._shortcuts.append(shortcut)

    def _enable_shortcuts(self, enabled: bool) -> None:
        for shortcut in self._shortcuts:
            shortcut.setEnabled(enabled)

    def _user_close(self) -> None:
        if not self._visible:
            return
        self.hide()
        self.closed.emit()


__all__ = [
    "ARROW_HEAD_LENGTH",
    "DEFAULT_TOOL",
    "ERASER_TOLERANCE",
    "MAX_ZOOM",
    "STROKE_WIDTH",
    "Background",
    "DrawingOverlay",
    "OverlayToolbar",
    "OverlayWindow",
    "Shape",
    "Tool",
    "erase_hits",
    "paint_shape",
    "shape_outline",
    "shape_to_json",
]
