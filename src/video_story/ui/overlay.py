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
the right mouse button always draws an arrow. The eraser removes a whole group at
once: all connected lines, every piece of a line or arrow split at screen edges. Other
windows (the control window) can be kept above the overlay with
:meth:`DrawingOverlay.keep_above`; like the toolbar they stay movable and clickable.

Sketch: the Sketch/Screen button (``S``) switches the background of every screen
between the screens as they are (the default whenever drawing mode is entered) and a
plain white canvas to sketch on; the shapes stay where they are either way. The
background is reported for the frame sidecars (:meth:`DrawingOverlay.background`) and
stored with every shape.

Keyboard shortcuts, in every Video Story window while drawing: ``Esc`` close,
``Ctrl+Z`` undo (nothing while Shift is held), ``Ctrl+Alt+C`` clear, ``S``
sketch/screen, ``R`` / ``E`` / ``L`` / ``B`` / ``A`` / ``U`` select a tool, ``1``-``9``
a color. On the overlay windows also ``0`` resets the zoom.

Shift (left button only; the right-button arrow and the eraser ignore it): the
rectangle becomes a square and the ellipse a circle while Shift is held; a brush
stroke is a closed shape (its end joins its first point) when Shift is held at the press
or pressed during the stroke (``closed_ms`` then says when); it is finished by releasing
Shift or the button. Lines: releasing the button with Shift
held leaves the line's end following the cursor; every press while Shift is held ends
it there and starts the next line from that point (connected lines, one ``group``);
releasing Shift ends the line at once when no button is held, else at the button's
release. Shift and Ctrl are read from the mouse and key events and polled while the
overlay is shown (it need not have the keyboard focus).

Ctrl (grid): while Ctrl is held, the points the user places snap to a grid of
:meth:`DrawingOverlay.set_grid_size` logical pixels (Settings > Drawing Grid, 16 by
default) starting at each screen's top-left corner: both corners of a rectangle or an
ellipse (with Shift a square or circle whose side is a multiple of the spacing), the
start and end of a line, every vertex of connected lines, the tail and the head of an
arrow, but not where a line or an arrow is cut at a screen edge. Brush and eraser never
snap. Ctrl is read at every point, so it can come or go in the middle of a drag. On the
sketch canvas the grid is painted in light grey while Ctrl is held (it shows in the
recording); on the screens it stays invisible. A shape with a snapped point carries
``grid``; :meth:`DrawingOverlay.grid` tells the recorder while snapping is on.

Lines and arrows can cross screens: while drawn they live in global coordinates in
:class:`DrawingOverlay` and every window paints its part; when finished they are split
at the screen edges into one shape per screen (the same ``group``; the pieces of an
arrow without the head are lines with ``part_of: "arrow"``). Undo and the eraser treat
the pieces of one split shape as one.

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
import math
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
GRID_COLOR = QColor("#E2E2E2")  # the snapping grid on the sketch canvas (Ctrl held)
DEFAULT_GRID_PX = 16  # grid spacing in logical pixels (Settings > Drawing Grid)
UNDO_SHORTCUT = "Ctrl+Z"
CLEAR_SHORTCUT = "Ctrl+Alt+C"
SHIFT_POLL_MS = 30  # how often Shift and Ctrl are polled while the overlay is shown
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
    (Tool.RECTANGLE, "Rectangle", "R", "drag from corner to corner; Shift: a square"),
    (
        Tool.ELLIPSE,
        "Ellipse",
        "E",
        "drag from corner to corner of its bounding box; Shift: a circle",
    ),
    (
        Tool.LINE,
        "Line",
        "L",
        "drag from start to end; release the button with Shift held for connected lines "
        "(every click starts the next one, releasing Shift ends)",
    ),
    (
        Tool.BRUSH,
        "Brush",
        "B",
        "freehand line; Shift (before or while drawing) closes the shape, releasing Shift "
        "finishes it",
    ),
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

    ``shift``: drawn in a Shift mode (a square, a circle, a closed brush shape, one of
    connected lines). ``group``: connected lines, and the pieces of a line or an arrow
    split at screen edges, share it (``None`` otherwise); ``segment`` numbers the
    connected lines 1, 2, ...; ``part_of`` is ``Tool.ARROW`` for the pieces of a split
    arrow without its head. ``split`` (internal, not exported) is shared by the pieces of
    one split shape, 0 for a shape that was not split. ``closed_ms``: when Shift closed a
    brush stroke already being drawn (``None`` when it was held from the press).
    ``grid``: the grid spacing (canvas units) its points snapped to while Ctrl was held.
    """

    tool: Tool
    color: QColor
    points: list[QPointF] = field(default_factory=list)
    screen_index: int = 0
    id: int = 0
    started_ms: int | None = None
    finished_ms: int | None = None
    background: Background = Background.SCREEN
    shift: bool = False
    group: int | None = None
    segment: int | None = None
    part_of: Tool | None = None
    split: int = 0
    closed_ms: int | None = None
    grid: int | None = None

    @property
    def closed(self) -> bool:
        """A brush stroke drawn with Shift: its end joins its first point."""
        return self.tool is Tool.BRUSH and self.shift

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
    if shape.shift:
        data["shift"] = True
    if shape.closed:
        data["closed"] = True
        if shape.closed_ms is not None:
            data["closed_ms"] = shape.closed_ms
    if shape.grid is not None:
        data["grid"] = round(shape.grid * device_pixel_ratio)
    if shape.group is not None:
        data["group"] = shape.group
    if shape.segment is not None:
        data["segment"] = shape.segment
    if shape.part_of is not None:
        data["part_of"] = str(shape.part_of)
    return data


def constrain_square(start: QPointF, end: QPointF) -> QPointF:
    """The corner opposite ``start`` of the square dragged towards ``end``.

    The side is the larger of the two distances, so the square grows into the quadrant
    of the cursor.
    """
    dx, dy = end.x() - start.x(), end.y() - start.y()
    side = max(abs(dx), abs(dy))
    return QPointF(start.x() + math.copysign(side, dx), start.y() + math.copysign(side, dy))


def snap_to_grid(point: QPointF, step: float) -> QPointF:
    """``point`` moved to the nearest grid point (multiples of ``step``, from 0,0; half
    way rounds up)."""
    return QPointF(_round_half_up(point.x() / step) * step, _round_half_up(point.y() / step) * step)


def _round_half_up(value: float) -> int:
    return math.floor(value + 0.5)


def snap_square(start: QPointF, end: QPointF, step: float) -> QPointF:
    """The corner opposite ``start`` of the square dragged towards ``end`` whose side is a
    multiple of ``step`` (a square stays a square; with ``start`` on the grid so is the
    corner)."""
    corner = constrain_square(start, end)
    dx, dy = corner.x() - start.x(), corner.y() - start.y()
    side = _round_half_up(abs(dx) / step) * step
    return QPointF(start.x() + math.copysign(side, dx), start.y() + math.copysign(side, dy))


def clip_segment(a: QPointF, b: QPointF, rect: QRectF) -> tuple[float, float] | None:
    """The part of the segment ``a``-``b`` inside ``rect`` as parameters ``(t0, t1)``.

    ``a + (b - a) * t`` for ``t0 <= t <= t1`` lies inside (Liang-Barsky); ``None`` when the
    segment misses the rectangle or only touches it in a point.
    """
    dx, dy = b.x() - a.x(), b.y() - a.y()
    t0, t1 = 0.0, 1.0
    for p, q in (
        (-dx, a.x() - rect.left()),
        (dx, rect.right() - a.x()),
        (-dy, a.y() - rect.top()),
        (dy, rect.bottom() - a.y()),
    ):
        if p == 0:
            if q < 0:
                return None
            continue
        t = q / p
        if p < 0:
            if t > t1:
                return None
            t0 = max(t0, t)
        else:
            if t < t0:
                return None
            t1 = min(t1, t)
    if t1 < t0 or (t1 == t0 and (dx or dy)):
        return None
    return t0, t1


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
        elif shape.closed:
            painter.drawPolygon(QPolygonF(shape.points))  # no brush: the outline only
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
        elif shape.closed:
            path.closeSubpath()  # the eraser also hits the joining stroke
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


def _shift_in(event: QMouseEvent) -> bool:
    return bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)


def _ctrl_in(event: QMouseEvent) -> bool:
    return bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)


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
        # Pieces of the erased split shapes that were on other windows (window, index, shape).
        self._erased_other: list[tuple[OverlayWindow, int, Shape]] = []
        self._raw_end: QPointF | None = None  # the cursor of a rectangle/ellipse drag
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        # Moves without a button reach the window too: connected lines follow the cursor.
        self.setMouseTracking(True)
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
            if not any(s is shape for s in self._shapes):
                continue  # already removed with its group
            if shape.group is not None:
                # Connected lines and the pieces of a line or arrow split at screen edges
                # share a group: the whole group goes, on every screen.
                group = shape.group
                self._erased.extend(self._take_matching(lambda s, g=group: s.group == g))
                self._erased_other.extend(self._controller._take_group_pieces(group, self))
                continue
            index = next(i for i, s in enumerate(self._shapes) if s is shape)
            del self._shapes[index]
            self._erased.append((index, shape))
            if shape.split:
                # The other pieces of a split shape go with it.
                self._erased_other.extend(self._controller._take_split_pieces(shape, self))
        self.update()
        self._controller._refresh_drawings()

    def _finish_erase(self) -> None:
        erased, self._erased = self._erased, []
        other, self._erased_other = self._erased_other, []
        self._erase_last = None
        self._draw_button = Qt.MouseButton.NoButton
        if erased or other:
            self._controller._record_erase(self, erased, other)

    def _take_split(self, split: int) -> list[tuple[int, Shape]]:
        """Remove the pieces of split shape ``split`` from this window: ``(index, shape)``."""
        return self._take_matching(lambda s: s.split == split)

    def _take_matching(self, predicate: Callable[[Shape], bool]) -> list[tuple[int, Shape]]:
        """Remove every shape matching ``predicate`` from this window: ``(index, shape)``
        in the order removed (each index as it was at its removal, for the undo)."""
        removed: list[tuple[int, Shape]] = []
        index = 0
        while index < len(self._shapes):
            if predicate(self._shapes[index]):
                removed.append((index, self._shapes.pop(index)))
            else:
                index += 1
        if removed:
            self.update()
            self._controller._refresh_drawings()
        return removed

    def _shaped_end(self, shape: Shape) -> QPointF:
        """The dragged corner: the cursor, or the square's corner while Shift is held; on
        the grid while Ctrl is held."""
        end = self._raw_end if self._raw_end is not None else shape.points[-1]
        controller = self._controller
        if shape.tool not in (Tool.RECTANGLE, Tool.ELLIPSE):
            return QPointF(end)
        step = controller.grid_px if controller.ctrl_held else None
        if step is not None:
            shape.grid = step
        if controller.shift_held:
            if step is not None:
                return snap_square(shape.points[0], end, step)
            return constrain_square(shape.points[0], end)
        if step is not None:
            return snap_to_grid(end, step)
        return QPointF(end)

    def _on_ctrl_changed(self) -> None:
        """Ctrl went down or up: the dragged corner snaps to the grid or follows the cursor
        again, and the grid on the sketch canvas appears or disappears."""
        shape = self._current
        if shape is not None and shape.tool in (Tool.RECTANGLE, Tool.ELLIPSE):
            shape.points[-1] = self._shaped_end(shape)
        self.update()

    def _on_shift_changed(self, held: bool) -> None:
        """Shift went down or up while this window's shape is being dragged."""
        shape = self._current
        if shape is None:
            return
        if shape.tool in (Tool.RECTANGLE, Tool.ELLIPSE):
            shape.shift = held  # a square / circle while held, free again when released
            shape.points[-1] = self._shaped_end(shape)
            self.update()
        elif shape.tool is Tool.BRUSH and held and not shape.shift:
            # Shift pressed in the middle of a stroke closes it at once, as if it had been
            # held from the press (releasing Shift then finishes it).
            shape.shift = True
            shape.closed_ms = self._controller._now_ms()
            self.update()
        elif shape.closed and not held:
            self._finish_current()  # releasing Shift finishes a closed brush shape

    def _finish_current(self) -> None:
        """Finish the dragged shape: store it, or drop it when it has no size."""
        shape = self._current
        if shape is None:
            return
        self._current = None
        self._raw_end = None
        self._draw_button = Qt.MouseButton.NoButton
        shape.finished_ms = self._controller._now_ms()
        if shape.is_degenerate():
            self.update()
            self._controller._refresh_drawings()
        else:
            self._controller._commit_shape(self, shape)
        self._controller._update_shift_timer()

    def cancel_gesture(self) -> None:
        """Drop a drag in progress (the overlay is hidden before the button is released)."""
        self._floating_drag = False
        self._raw_end = None
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
        self._erased_other = []
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
            if self._controller.ctrl_held:
                self._paint_grid(painter)
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
        span = self._controller._span_view_shape(self)
        if span is not None:
            paint_shape(painter, span)  # the line or arrow being drawn (maybe from elsewhere)
        painter.end()

    def _paint_grid(self, painter: QPainter) -> None:
        """The snapping grid (Ctrl held on the sketch canvas): 1 px lines on the multiples
        of the grid spacing, magnified with the zoom like the shapes."""
        step = float(self._controller.grid_px)
        visible = self.visible_canvas_rect()
        pen = QPen(GRID_COLOR, 1)
        pen.setCosmetic(True)
        painter.setPen(pen)
        x = math.ceil(visible.left() / step) * step
        while x <= visible.right():
            vx = round(self.to_view(QPointF(x, 0.0)).x())
            painter.drawLine(vx, 0, vx, self.height())
            x += step
        y = math.ceil(visible.top() / step) * step
        while y <= visible.bottom():
            vy = round(self.to_view(QPointF(0.0, y)).y())
            painter.drawLine(0, vy, self.width(), vy)
            y += step

    def event(self, event: QEvent) -> bool:
        if event.type() == QEvent.Type.WindowActivate:
            # Activating an overlay window puts it above the toolbar (both stay on top);
            # the overlay is invisible but would swallow the clicks meant for the toolbar.
            self._controller._schedule_toolbar_raise()
        return super().event(event)

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802
        steps = event.angleDelta().y() / 120.0
        if (
            not steps
            or not self.can_zoom()
            or self._current is not None
            or self.erasing
            or self._controller._span is not None
        ):
            event.ignore()
            return
        self.set_zoom(self._zoom * (ZOOM_STEP**steps), event.position())
        event.accept()

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        button = event.button()
        controller = self._controller
        controller._note_ctrl(_ctrl_in(event))
        if button in (Qt.MouseButton.LeftButton, Qt.MouseButton.RightButton):
            controller._note_shift(_shift_in(event))  # may end connected lines
        span = controller._span
        if span is not None and span.waiting:
            # Connected lines: a left press ends the line here and starts the next one;
            # the other buttons do nothing meanwhile.
            if button == Qt.MouseButton.LeftButton:
                controller._chain_press(self, event.globalPosition())
            event.accept()
            return
        busy = (
            self._current is not None
            or self._floating_drag
            or self._pan_anchor is not None
            or self.erasing
            or span is not None
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
        if tool in (Tool.LINE, Tool.ARROW):
            # Lines and arrows are kept by the controller: they may cross screens.
            controller._begin_span(self, tool, button, event.globalPosition())
            event.accept()
            return
        pos = self.to_canvas(event.position())
        self._draw_button = button
        if tool is Tool.ERASER:
            self._erased = []
            self._erased_other = []
            self._erase_to(pos)  # a click right on a line removes that shape too
            event.accept()
            return
        shift = controller.shift_held
        snap = tool in (Tool.RECTANGLE, Tool.ELLIPSE) and controller.ctrl_held
        if snap:
            pos = snap_to_grid(pos, controller.grid_px)
        self._current = Shape(
            tool,
            QColor(controller.color),
            [pos, pos],
            self.screen_index,
            started_ms=controller._now_ms(),
            # A square / circle, or a closed brush shape (Shift may also come later).
            shift=shift and tool in (Tool.RECTANGLE, Tool.ELLIPSE, Tool.BRUSH),
            grid=controller.grid_px if snap else None,
        )
        self._raw_end = pos
        self.update()
        controller._refresh_drawings()  # drawing_now: a shape is being drawn
        controller._update_shift_timer()
        event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        self._controller._note_ctrl(_ctrl_in(event))
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
        controller = self._controller
        if controller._span is not None:
            # A line or an arrow, dragged here or (connected lines) followed from any screen.
            controller._move_span(event.globalPosition())
            controller._note_shift(_shift_in(event))  # releasing Shift may end the lines
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
        controller._note_shift(_shift_in(event))  # may finish a closed brush shape
        if self._current is None:
            event.accept()
            return
        pos = self.to_canvas(event.position())
        if self._current.tool is Tool.BRUSH:
            self._current.points.append(pos)
        else:
            self._raw_end = pos
            self._current.points[-1] = self._shaped_end(self._current)
        # Only the screen changes: the sidecars describe the shape once it is finished.
        self.update()
        event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        self._controller._note_ctrl(_ctrl_in(event))
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
        controller = self._controller
        span = controller._span
        if span is not None and not span.waiting and event.button() == span.button:
            controller._move_span(event.globalPosition())
            controller._release_span(_shift_in(event))
            event.accept()
            return
        if self._current is None or event.button() != self._draw_button:
            event.ignore()
            return
        controller._note_shift(_shift_in(event))  # may finish a closed brush shape
        shape = self._current
        if shape is None:
            event.accept()
            return
        pos = self.to_canvas(event.position())
        if shape.tool is Tool.BRUSH:
            if pos != shape.points[-1]:
                shape.points.append(pos)
        else:
            self._raw_end = pos
            shape.points[-1] = self._shaped_end(shape)
        self._finish_current()
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
        shift = bool(modifiers & Qt.KeyboardModifier.ShiftModifier)
        plain = not ctrl and not alt
        letter = chr(key) if ord("A") <= key <= ord("Z") else ""
        if key == Qt.Key.Key_Shift:
            self._controller._note_shift(True)
        elif key == Qt.Key.Key_Control:
            self._controller._note_ctrl(True)
        elif key == Qt.Key.Key_Escape:
            self._controller._user_close()
        elif key == Qt.Key.Key_Z and ctrl and not alt and not shift:
            self._controller.undo()
        elif key == Qt.Key.Key_C and ctrl and alt and not shift:
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

    def keyReleaseEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Shift and not event.isAutoRepeat():
            self._controller._note_shift(False)
            event.accept()
            return
        if event.key() == Qt.Key.Key_Control and not event.isAutoRepeat():
            self._controller._note_ctrl(False)
            event.accept()
            return
        super().keyReleaseEvent(event)

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
    """One undoable step: the shapes ``added`` (one, or the pieces of a line or an arrow
    split at screen edges), or the ``(window, index, shape)`` entries one eraser drag
    removed (in the order they were removed)."""

    added: list[tuple[OverlayWindow, Shape]] = field(default_factory=list)
    erased: list[tuple[OverlayWindow, int, Shape]] = field(default_factory=list)


@dataclass
class _Span:
    """A line or an arrow being drawn, in global coordinates (it may cross screens)."""

    tool: Tool  # LINE or ARROW
    color: QColor
    start: QPointF
    end: QPointF
    window: OverlayWindow  # where it was started (the screen ``drawing_now`` reports)
    button: Qt.MouseButton  # the button dragging it; NoButton while connected lines wait
    started_ms: int | None
    group: int | None = None  # set for connected lines (from the second line on)
    segment: int = 1
    raw_end: QPointF | None = None  # the cursor (global), before snapping to the grid
    start_snapped: bool = False  # the start lies on the grid (Ctrl at the press)
    end_snapped: bool = False  # the end lies on the grid (Ctrl at the last move)

    @property
    def waiting(self) -> bool:
        """Connected lines: the end follows the cursor without a button."""
        return self.button == Qt.MouseButton.NoButton


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
        self._next_group = 0  # the last group of connected lines / split shapes
        self._next_split = 0  # the last split shape
        self._span: _Span | None = None  # the line or arrow being drawn
        self._shift = False  # Shift as last seen (mouse events, key events, polling)
        self._ctrl = False  # Ctrl as last seen: points snap to the grid while held
        self._grid_px = DEFAULT_GRID_PX
        self._grid: dict[str, int] | None = None  # grid spacing per screen for the sidecars
        # Where Shift is polled while a shape is being drawn (replaceable in tests).
        self._shift_source: Callable[[], Qt.KeyboardModifier] = (
            QGuiApplication.queryKeyboardModifiers
        )
        self._shift_timer = QTimer(self)
        self._shift_timer.setInterval(SHIFT_POLL_MS)
        self._shift_timer.timeout.connect(self._poll_shift)

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

    @property
    def shift_held(self) -> bool:
        """Shift as last seen by the overlay (it is polled while the overlay is shown)."""
        return self._shift

    @property
    def ctrl_held(self) -> bool:
        """Ctrl as last seen by the overlay: points snap to the grid while it is held."""
        return self._ctrl

    @property
    def grid_px(self) -> int:
        """The grid spacing in logical pixels (canvas units)."""
        return self._grid_px

    def set_grid_size(self, px: int) -> None:
        """The spacing of the snapping grid in logical pixels (Settings > Drawing Grid)."""
        self._grid_px = max(1, int(px))
        for window in self._windows:
            window.update()
        self._refresh_drawings()

    def grid(self) -> dict[str, int] | None:
        """``{screen id: spacing in pixels of that screen's image}`` while Ctrl is held in
        drawing mode (points snap to the grid), else ``None``. Thread-safety as for
        :meth:`drawings`."""
        return self._grid

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
        self._update_shift_timer()

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
        self._cancel_span()
        self._ctrl = False
        self._refresh_drawings()
        self._update_shift_timer()

    def close(self) -> None:
        self.hide()
        self._destroy_windows()
        if self._toolbar is not None:
            self._toolbar.deleteLater()  # deletes the shortcuts with it
            self._toolbar = None
            self._shortcuts = []

    def clear(self) -> None:
        self._cancel_span()
        for window in self._windows:
            window.clear_shapes()
        self._history.clear()

    def undo(self) -> None:
        """Take back the last step: remove the last shape drawn (every piece of a split
        line or arrow), or bring back every shape the last eraser drag removed (same id,
        same place in the drawing order). Nothing while Shift is held."""
        if self._shift_source() & Qt.KeyboardModifier.ShiftModifier:
            return  # Ctrl+Z in the middle of a Shift action makes no sense
        while self._history:
            action = self._history.pop()
            if action.erased:
                per_window: list[tuple[OverlayWindow, list[tuple[int, Shape]]]] = []
                for window, index, shape in action.erased:
                    for known, entries in per_window:
                        if known is window:
                            entries.append((index, shape))
                            break
                    else:
                        per_window.append((window, [(index, shape)]))
                for window, entries in per_window:
                    window.restore_shapes(entries)
                return
            removed = False
            for window, shape in action.added:
                removed = window.remove_shape(shape) or removed
            if removed:
                return

    def set_tool(self, tool: Tool) -> None:
        if self._span is not None and self._span.waiting:
            self._cancel_span()  # another tool ends connected lines
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
        self._span = None
        self._shift_timer.stop()
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
        self._commit_shapes([(window, shape)])

    def _commit_shapes(self, parts: list[tuple[OverlayWindow, Shape]]) -> None:
        """Store finished shapes (one, or the pieces of a split one): one undoable step."""
        for window, shape in parts:
            if not shape.id:
                self._next_id += 1
                shape.id = self._next_id
            if shape.finished_ms is None:
                shape.finished_ms = self._now_ms()
            if shape.started_ms is None:
                shape.started_ms = shape.finished_ms
            shape.background = Background.SKETCH if self._sketch else Background.SCREEN
            window.add_shape(shape)
        if parts:
            self._history.append(_Action(added=list(parts)))

    def _record_erase(
        self,
        window: OverlayWindow,
        erased: list[tuple[int, Shape]],
        other: list[tuple[OverlayWindow, int, Shape]] | None = None,
    ) -> None:
        """One eraser drag removed ``erased`` from ``window`` (and the ``other`` pieces of
        split shapes from other windows): one undoable step."""
        entries = [(window, index, shape) for index, shape in erased] + list(other or [])
        self._history.append(_Action(erased=entries))

    def _take_split_pieces(
        self, shape: Shape, origin: OverlayWindow
    ) -> list[tuple[OverlayWindow, int, Shape]]:
        """Remove the pieces of ``shape``'s split shape from the windows other than ``origin``."""
        taken: list[tuple[OverlayWindow, int, Shape]] = []
        for window in self._windows:
            if window is not origin:
                taken.extend(
                    (window, index, piece) for index, piece in window._take_split(shape.split)
                )
        return taken

    def _take_group_pieces(
        self, group: int, origin: OverlayWindow
    ) -> list[tuple[OverlayWindow, int, Shape]]:
        """Remove every shape of ``group`` from the windows other than ``origin``."""
        taken: list[tuple[OverlayWindow, int, Shape]] = []
        for window in self._windows:
            if window is not origin:
                taken.extend(
                    (window, index, piece)
                    for index, piece in window._take_matching(lambda s, g=group: s.group == g)
                )
        return taken

    # -- Shift ----------------------------------------------------------------------

    def _note_shift(self, held: bool) -> None:
        """Shift is ``held`` (seen in a mouse or key event, or polled)."""
        if held == self._shift:
            return
        self._shift = held
        for window in list(self._windows):
            window._on_shift_changed(held)
        span = self._span
        if span is not None and span.waiting and not held:
            self._finish_span()  # releasing Shift without a button ends connected lines

    def _note_ctrl(self, held: bool) -> None:
        """Ctrl is ``held`` (seen in a mouse or key event, or polled)."""
        if held == self._ctrl:
            return
        self._ctrl = held
        span = self._span
        if span is not None and span.raw_end is not None:
            self._move_span(span.raw_end)  # the end snaps to the grid or is free again
        for window in list(self._windows):
            window._on_ctrl_changed()
        self._refresh_drawings()

    def _poll_shift(self) -> None:
        modifiers = self._shift_source()
        self._note_shift(bool(modifiers & Qt.KeyboardModifier.ShiftModifier))
        self._note_ctrl(bool(modifiers & Qt.KeyboardModifier.ControlModifier))
        self._update_shift_timer()

    def _update_shift_timer(self) -> None:
        """Poll Shift and Ctrl while the overlay is shown or a shape is being drawn (the
        overlay may lack the focus; Ctrl shows the grid without moving the mouse)."""
        active = (
            self._visible
            or self._span is not None
            or any(w.current_shape is not None for w in self._windows)
        )
        if active and not self._shift_timer.isActive():
            self._shift_timer.start()
        elif not active and self._shift_timer.isActive():
            self._shift_timer.stop()

    # -- lines and arrows (they may cross screens) ------------------------------------

    def _begin_span(
        self, window: OverlayWindow, tool: Tool, button: Qt.MouseButton, global_pos: QPointF
    ) -> None:
        start, snapped = self._snapped_global(global_pos, window)
        self._span = _Span(
            tool,
            QColor(self._color),
            start,
            QPointF(start),
            window,
            button,
            self._now_ms(),
            raw_end=QPointF(global_pos),
            start_snapped=snapped,
            end_snapped=snapped,
        )
        self._span_changed()

    def _snapped_global(
        self, global_pos: QPointF, window: OverlayWindow | None = None
    ) -> tuple[QPointF, bool]:
        """``global_pos`` on the grid of the screen under it while Ctrl is held, and
        whether it was snapped. The grid is in canvas units of that window (its zoom)."""
        if not self._ctrl:
            return QPointF(global_pos), False
        if window is None:
            point = global_pos.toPoint()
            window = next((w for w in self._windows if w.geometry().contains(point)), None)
        if window is None:
            return QPointF(global_pos), False
        local = QPointF(window.mapFromGlobal(global_pos))
        snapped = window.to_view(snap_to_grid(window.to_canvas(local), self._grid_px))
        return QPointF(window.mapToGlobal(snapped)), True

    def _move_span(self, global_pos: QPointF) -> None:
        if self._span is None:
            return
        self._span.raw_end = QPointF(global_pos)
        self._span.end, self._span.end_snapped = self._snapped_global(global_pos)
        for window in self._windows:
            window.update()  # only the screens change; the sidecars get the finished shape

    def _release_span(self, shift: bool) -> None:
        """The button dragging the line or arrow was released (the end is up to date)."""
        span = self._span
        if span is None:
            return
        if shift and span.tool is Tool.LINE and span.button == Qt.MouseButton.LeftButton:
            span.button = Qt.MouseButton.NoButton  # connected lines: the end follows the cursor
            self._note_shift(True)
            return
        self._finish_span()

    def _chain_press(self, window: OverlayWindow, global_pos: QPointF) -> None:
        """A left press while connected lines wait: end the line here, start the next."""
        span = self._span
        if span is None:
            return
        span.raw_end = QPointF(global_pos)
        span.end, span.end_snapped = self._snapped_global(global_pos, window)
        pieces = self._span_pieces(span)
        if not pieces:
            span.button = Qt.MouseButton.LeftButton  # a press on the start: nothing to end
            self._span_changed()
            return
        if span.group is None:
            self._next_group += 1
            span.group = self._next_group
        now = self._now_ms()  # the end of this line is the start of the next one
        self._commit_span(span, pieces, now)
        start = QPointF(span.end)
        self._span = _Span(
            Tool.LINE,
            QColor(span.color),
            start,
            QPointF(start),
            window,
            Qt.MouseButton.LeftButton,
            now,
            group=span.group,
            segment=span.segment + 1,
            raw_end=QPointF(start),
            start_snapped=span.end_snapped,
            end_snapped=span.end_snapped,
        )
        self._span_changed()

    def _finish_span(self) -> None:
        span, self._span = self._span, None
        if span is None:
            return
        pieces = self._span_pieces(span)
        if pieces:
            self._commit_span(span, pieces, self._now_ms())
        self._span_changed()

    def _cancel_span(self) -> None:
        """Drop the line or arrow being drawn (the lines already finished stay)."""
        if self._span is None:
            return
        self._span = None
        self._span_changed()

    def _span_changed(self) -> None:
        for window in self._windows:
            window.update()
        self._refresh_drawings()
        self._update_shift_timer()

    def _span_pieces(self, span: _Span) -> list[tuple[OverlayWindow, QPointF, QPointF]]:
        """The parts of ``span`` on every screen, from its start to its end, as canvas points
        of that screen's window (cut at the screen edges; parts over no screen are lost)."""
        found: list[tuple[float, OverlayWindow, QPointF, QPointF]] = []
        for window in self._windows:
            a = QPointF(window.mapFromGlobal(span.start))
            b = QPointF(window.mapFromGlobal(span.end))
            clipped = clip_segment(a, b, QRectF(window.rect()))
            if clipped is None:
                continue
            t0, t1 = clipped
            first = window.to_canvas(a + (b - a) * t0)
            last = window.to_canvas(a + (b - a) * t1)
            if (last - first).manhattanLength() < 1.0:
                continue
            found.append((t0, window, first, last))
        found.sort(key=lambda entry: entry[0])
        return [(window, first, last) for _t, window, first, last in found]

    def _commit_span(
        self,
        span: _Span,
        pieces: list[tuple[OverlayWindow, QPointF, QPointF]],
        finished: int | None,
    ) -> None:
        """Store ``span`` as one shape per screen it crosses (one undoable step)."""
        connected = span.group is not None
        group = span.group
        split = 0
        if len(pieces) > 1:
            self._next_split += 1
            split = self._next_split
            if group is None:
                self._next_group += 1
                group = self._next_group
        parts: list[tuple[OverlayWindow, Shape]] = []
        for index, (window, first, last) in enumerate(pieces):
            head = span.tool is Tool.ARROW and index == len(pieces) - 1
            shape = Shape(
                Tool.ARROW if head else Tool.LINE,
                QColor(span.color),
                [first, last],
                window.screen_index,
                started_ms=span.started_ms,
                finished_ms=finished,
                shift=connected,
                group=group,
                segment=span.segment if connected else None,
                part_of=Tool.ARROW if span.tool is Tool.ARROW and not head else None,
                split=split,
                grid=self._grid_px if span.start_snapped or span.end_snapped else None,
            )
            parts.append((window, shape))
        self._commit_shapes(parts)

    def _span_view_shape(self, window: OverlayWindow) -> Shape | None:
        """The line or arrow being drawn in ``window``'s view coordinates (for painting)."""
        span = self._span
        if span is None:
            return None
        return Shape(
            span.tool,
            span.color,
            [QPointF(window.mapFromGlobal(span.start)), QPointF(window.mapFromGlobal(span.end))],
            window.screen_index,
        )

    def _refresh_drawings(self) -> None:
        if not self._visible or not self._screens:
            self._drawings = []
            self._drawing_now = None
            self._zoom = []
            self._grid = None
            return
        grid: dict[str, int] = {}
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
            if self._ctrl:
                grid[screen.id] = round(self._grid_px * ratio)
            for shape in window.view_shapes(include_current=False):
                if shape.points:
                    result.append(shape_to_json(shape, screen, ratio))
            current = window.current_shape
            if current is None and self._span is not None and self._span.window is window:
                current = Shape(self._span.tool, self._span.color, started_ms=self._span.started_ms)
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
        self._grid = grid or None

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

        Esc close, Ctrl+Z undo, Ctrl+Alt+C clear, S sketch/screen, R / E / L / B / A / U
        select a tool and 1-9 a palette color.
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
    "DEFAULT_GRID_PX",
    "DEFAULT_TOOL",
    "GRID_COLOR",
    "ERASER_TOLERANCE",
    "MAX_ZOOM",
    "STROKE_WIDTH",
    "Background",
    "DrawingOverlay",
    "OverlayToolbar",
    "OverlayWindow",
    "Shape",
    "Tool",
    "clip_segment",
    "constrain_square",
    "snap_square",
    "snap_to_grid",
    "erase_hits",
    "paint_shape",
    "shape_outline",
    "shape_to_json",
]
