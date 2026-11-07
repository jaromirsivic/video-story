"""On-screen drawing overlay ("Draw on Screen").

One frameless, translucent, always-on-top window is created per connected screen so
the user can draw annotations (brush strokes, rectangles, arrows) anywhere on the
desktop. The windows are real, visible top-level windows that accept mouse input, so
the drawing is part of the composited screen and therefore shows up in screenshots
taken by ``mss``. Drawing is a mode of its own: while the overlay is shown, clicks do
not reach the applications below it.

A small floating toolbar on the primary screen offers the tools (rectangle is the
default), the 8-color palette and Undo / Clear / Close. Keyboard shortcuts on the overlay windows:
``Esc`` close, ``C`` clear, ``Ctrl+Z`` undo, ``B`` / ``R`` / ``A`` select a tool.

All geometry is kept in Qt logical coordinates of the window the shape was drawn in;
Qt maps them to physical pixels per screen (Windows per-monitor DPI is handled by Qt).

For the frame sidecars the overlay also keeps a JSON-ready snapshot of what is drawn
(:meth:`DrawingOverlay.drawings`), with every point converted to pixels of the recorded
screen image: the recorder reads it from its capture thread at every tick.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from enum import StrEnum

from PySide6.QtCore import QLineF, QObject, QPoint, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QGuiApplication,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPen,
    QPolygonF,
    QScreen,
)
from PySide6.QtWidgets import (
    QButtonGroup,
    QGridLayout,
    QHBoxLayout,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from video_story.core.screens import Screen

from .palette import DEFAULT_COLOR, PALETTE, PaletteColor

STROKE_WIDTH = 4.0  # logical pixels
ARROW_HEAD_LENGTH = 18.0  # logical pixels
ARROW_HEAD_HALF_ANGLE = 26.0  # degrees between the shaft and each side of the head
TOOLBAR_MARGIN = 16  # logical pixels from the top-left corner of the primary screen
HIT_TEST_FILL = QColor(0, 0, 0, 1)  # see OverlayWindow.paintEvent


class Tool(StrEnum):
    BRUSH = "brush"
    RECTANGLE = "rectangle"
    ARROW = "arrow"


DEFAULT_TOOL = Tool.RECTANGLE  # the tool selected when the overlay is created


@dataclass
class Shape:
    """One drawn annotation in the logical coordinates of its overlay window.

    ``points`` holds the polyline for a brush stroke and ``[start, end]`` for a
    rectangle (two opposite corners) or an arrow (tail, head).
    """

    tool: Tool
    color: QColor
    points: list[QPointF] = field(default_factory=list)
    screen_index: int = 0

    def is_degenerate(self) -> bool:
        if self.tool is Tool.BRUSH:
            return not self.points
        if len(self.points) < 2:
            return True
        return (self.points[0] - self.points[-1]).manhattanLength() < 1.0


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

    Rectangles and arrows carry ``from`` and ``to`` (two opposite corners, or tail and
    head); brush strokes carry ``path``. Every point is ``{screen, x, y}`` like the
    cursor entry of a sidecar.
    """

    def point(p: QPointF) -> dict[str, object]:
        x = min(max(0, round(p.x() * device_pixel_ratio)), screen.image_width - 1)
        y = min(max(0, round(p.y() * device_pixel_ratio)), screen.image_height - 1)
        return {"screen": screen.id, "x": x, "y": y}

    data: dict[str, object] = {
        "tool": str(shape.tool),
        "color": shape.color.name().upper(),
        "screen": screen.id,
        "stroke_width": round(STROKE_WIDTH * device_pixel_ratio),
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
        self._shapes: list[Shape] = []
        self._current: Shape | None = None
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setWindowTitle(f"Video Story drawing overlay {screen_index + 1}")

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

    def add_shape(self, shape: Shape) -> None:
        shape.screen_index = self.screen_index
        self._shapes.append(shape)
        self.update()
        self._controller._refresh_drawings()

    def pop_shape(self) -> Shape | None:
        if not self._shapes:
            return None
        shape = self._shapes.pop()
        self.update()
        self._controller._refresh_drawings()
        return shape

    def clear_shapes(self) -> None:
        if self._shapes or self._current is not None:
            self._shapes.clear()
            self._current = None
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
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        for shape in self._shapes:
            paint_shape(painter, shape)
        if self._current is not None:
            paint_shape(painter, self._current)
        painter.end()

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton:
            event.ignore()
            return
        self._controller._raise_toolbar()
        pos = event.position()
        self._current = Shape(
            self._controller.tool, QColor(self._controller.color), [pos, pos], self.screen_index
        )
        self.update()
        self._controller._refresh_drawings()
        event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._current is None or not (event.buttons() & Qt.MouseButton.LeftButton):
            event.ignore()
            return
        pos = event.position()
        if self._current.tool is Tool.BRUSH:
            self._current.points.append(pos)
        else:
            self._current.points[-1] = pos
        self.update()
        self._controller._refresh_drawings()
        event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton or self._current is None:
            event.ignore()
            return
        shape = self._current
        self._current = None
        pos = event.position()
        if shape.tool is Tool.BRUSH:
            if pos != shape.points[-1]:
                shape.points.append(pos)
        else:
            shape.points[-1] = pos
        if shape.is_degenerate():
            self.update()
            self._controller._refresh_drawings()
        else:
            self._controller._commit_shape(self, shape)
        event.accept()

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        key = event.key()
        modifiers = event.modifiers()
        ctrl = bool(modifiers & Qt.KeyboardModifier.ControlModifier)
        if key == Qt.Key.Key_Escape:
            self._controller._user_close()
        elif key == Qt.Key.Key_Z and ctrl:
            self._controller.undo()
        elif key == Qt.Key.Key_C and not ctrl:
            self._controller.clear()
        elif key == Qt.Key.Key_B and not ctrl:
            self._controller.set_tool(Tool.BRUSH)
        elif key == Qt.Key.Key_R and not ctrl:
            self._controller.set_tool(Tool.RECTANGLE)
        elif key == Qt.Key.Key_A and not ctrl:
            self._controller.set_tool(Tool.ARROW)
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
    """Floating tool window with tools, palette and Undo / Clear / Close."""

    tool_selected = Signal(object)  # Tool
    color_selected = Signal(object)  # PaletteColor
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
        # Explicit colors: the toolbar must look the same in light and dark system themes.
        self.setStyleSheet(
            "QWidget#overlayToolbar { background: #F4F4F4; border: 1px solid #707070; }"
            "QWidget#overlayToolbar QToolButton, QWidget#overlayToolbar QPushButton {"
            " background: #FFFFFF; color: #000000; border: 1px solid #909090;"
            " border-radius: 3px; padding: 3px 8px; }"
            "QWidget#overlayToolbar QToolButton:hover, QWidget#overlayToolbar QPushButton:hover {"
            " background: #E8F0FF; }"
            "QWidget#overlayToolbar QToolButton:checked {"
            " background: #FFD27F; border: 1px solid #A06000; }"
        )

        self._tool_buttons: dict[Tool, QToolButton] = {}
        self._swatches: dict[str, QToolButton] = {}
        self._current_hex = DEFAULT_COLOR.hex.upper()

        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(4)

        row = QHBoxLayout()
        row.setSpacing(4)
        group = QButtonGroup(self)
        group.setExclusive(True)
        for tool, label, tip in (
            (Tool.BRUSH, "Brush", "Brush: freehand line (B)"),
            (Tool.RECTANGLE, "Rect", "Rectangle: drag from corner to corner (R)"),
            (Tool.ARROW, "Arrow", "Arrow: drag from tail to head (A)"),
        ):
            button = QToolButton(self)
            button.setText(label)
            button.setToolTip(tip)
            button.setCheckable(True)
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            button.clicked.connect(lambda _checked=False, t=tool: self.tool_selected.emit(t))
            group.addButton(button)
            row.addWidget(button)
            self._tool_buttons[tool] = button
        row.addSpacing(8)
        for label, tip, signal in (
            ("Undo", "Undo the last shape (Ctrl+Z)", self.undo_requested),
            ("Clear", "Remove all drawings (C)", self.clear_requested),
            ("Close", "Leave drawing mode (Esc)", self.close_requested),
        ):
            button = QPushButton(label, self)
            button.setToolTip(tip)
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            button.clicked.connect(signal.emit)
            row.addWidget(button)
        root.addLayout(row)

        grid = QGridLayout()
        grid.setSpacing(3)
        for index, entry in enumerate(PALETTE):
            swatch = QToolButton(self)
            swatch.setFixedSize(22, 22)
            swatch.setToolTip(entry.name)
            swatch.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            swatch.clicked.connect(lambda _checked=False, e=entry: self.color_selected.emit(e))
            grid.addWidget(swatch, index // 8, index % 8)
            self._swatches[entry.hex.upper()] = swatch
        root.addLayout(grid)

        self.set_tool(DEFAULT_TOOL)
        self.set_color(DEFAULT_COLOR.hex)

    def set_tool(self, tool: Tool) -> None:
        button = self._tool_buttons[tool]
        if not button.isChecked():
            button.setChecked(True)

    def set_color(self, hex_value: str) -> None:
        self._current_hex = hex_value.upper()
        for swatch_hex, swatch in self._swatches.items():
            selected = swatch_hex == self._current_hex
            border = "3px solid #000000" if selected else "1px solid #808080"
            if selected and swatch_hex in ("#000000", "#000080", "#8B0000"):
                border = "3px solid #FFFFFF"
            swatch.setStyleSheet(f"QToolButton {{ background: {swatch_hex}; border: {border}; }}")


class DrawingOverlay(QObject):
    """Controller for the per-screen overlay windows and the floating toolbar.

    Create it after ``QApplication``. :meth:`show` opens one window per screen (and
    rebuilds them if the number of screens changed since the last show); :meth:`hide`
    keeps the drawings for the next :meth:`show`; :meth:`close` destroys everything.
    ``closed`` is emitted only when the user leaves drawing mode (Esc, Close button,
    Alt+F4), never for :meth:`hide` / :meth:`close` called from code.
    """

    closed = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._tool = DEFAULT_TOOL
        self._palette_color: PaletteColor | None = DEFAULT_COLOR
        self._color = QColor(DEFAULT_COLOR.hex)
        self._windows: list[OverlayWindow] = []
        self._history: list[OverlayWindow] = []  # window of each committed shape, in order
        self._toolbar: OverlayToolbar | None = None
        self._visible = False
        self._screens: list[Screen] = []
        self._drawings: list[dict[str, object]] = []

    # -- public API ---------------------------------------------------------------

    @property
    def tool(self) -> Tool:
        return self._tool

    @property
    def color(self) -> QColor:
        return QColor(self._color)

    @property
    def windows(self) -> list[OverlayWindow]:
        return list(self._windows)

    def is_visible(self) -> bool:
        return self._visible

    def show(self) -> None:
        screens = QGuiApplication.screens()
        if not screens:
            raise RuntimeError("no screens available")
        if len(self._windows) != len(screens):
            self._destroy_windows()
            self._windows = [OverlayWindow(self, index) for index in range(len(screens))]
        for window, screen in zip(self._windows, screens, strict=True):
            self._place_window(window, screen)
            window.show()
            window.raise_()
        toolbar = self._ensure_toolbar()
        primary = QGuiApplication.primaryScreen() or screens[0]
        toolbar.setScreen(primary)
        toolbar.adjustSize()
        toolbar.move(primary.geometry().topLeft() + QPoint(TOOLBAR_MARGIN, TOOLBAR_MARGIN))
        toolbar.show()
        toolbar.raise_()
        self._visible = True
        focus_window = self._window_for_screen(primary) or self._windows[0]
        focus_window.activateWindow()
        focus_window.setFocus(Qt.FocusReason.OtherFocusReason)
        self._refresh_drawings()

    def hide(self) -> None:
        self._visible = False
        if self._toolbar is not None:
            self._toolbar.hide()
        for window in self._windows:
            window.hide()
        self._refresh_drawings()

    def close(self) -> None:
        self.hide()
        self._destroy_windows()
        if self._toolbar is not None:
            self._toolbar.deleteLater()
            self._toolbar = None

    def clear(self) -> None:
        for window in self._windows:
            window.clear_shapes()
        self._history.clear()

    def undo(self) -> None:
        while self._history:
            window = self._history.pop()
            if window.pop_shape() is not None:
                return

    def set_tool(self, tool: Tool) -> None:
        self._tool = Tool(tool)
        if self._toolbar is not None:
            self._toolbar.set_tool(self._tool)

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

    def drawings(self) -> list[dict[str, object]]:
        """JSON-ready snapshot of everything currently visible on the overlay windows.

        Safe to call from any thread: the list is rebuilt on the GUI thread whenever
        the drawing changes and never mutated afterwards. Empty while the overlay is
        hidden (nothing is on screen then) or until :meth:`set_screens` was called.
        """
        return self._drawings

    def add_shape(self, shape: Shape) -> None:
        """Add a shape programmatically to the window of ``shape.screen_index``."""
        if not self._windows:
            raise RuntimeError("overlay windows do not exist; call show() first")
        if not 0 <= shape.screen_index < len(self._windows):
            raise IndexError(f"no overlay window for screen index {shape.screen_index}")
        self._commit_shape(self._windows[shape.screen_index], shape)

    # -- internals ----------------------------------------------------------------

    def _ensure_toolbar(self) -> OverlayToolbar:
        if self._toolbar is None:
            toolbar = OverlayToolbar()
            toolbar.tool_selected.connect(self.set_tool)
            toolbar.color_selected.connect(self.set_color)
            toolbar.undo_requested.connect(self.undo)
            toolbar.clear_requested.connect(self.clear)
            toolbar.close_requested.connect(self._user_close)
            toolbar.set_tool(self._tool)
            toolbar.set_color(self._color.name().upper())
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

    def _commit_shape(self, window: OverlayWindow, shape: Shape) -> None:
        window.add_shape(shape)
        self._history.append(window)

    def _refresh_drawings(self) -> None:
        if not self._visible or not self._screens:
            self._drawings = []
            return
        qscreens = QGuiApplication.screens()
        result: list[dict[str, object]] = []
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
            for shape in window.painted_shapes:
                if shape.points:
                    result.append(shape_to_json(shape, screen, ratio))
        self._drawings = result

    def _raise_toolbar(self) -> None:
        if self._toolbar is not None and self._toolbar.isVisible():
            self._toolbar.raise_()

    def _user_close(self) -> None:
        if not self._visible:
            return
        self.hide()
        self.closed.emit()


__all__ = [
    "ARROW_HEAD_LENGTH",
    "DEFAULT_TOOL",
    "STROKE_WIDTH",
    "DrawingOverlay",
    "OverlayToolbar",
    "OverlayWindow",
    "Shape",
    "Tool",
    "paint_shape",
    "shape_to_json",
]
