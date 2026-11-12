"""Vector icons drawn with QPainter (no image assets needed)."""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QColor,
    QGuiApplication,
    QIcon,
    QPainter,
    QPainterPath,
    QPalette,
    QPen,
    QPixmap,
    QPolygonF,
)

RECORD_RED = QColor("#E53935")
STOP_RED = QColor("#C62828")
PROCESS_GREEN = QColor("#2E7D32")
DISCARD_RED = QColor("#C62828")
INK = QColor("#333333")


def _pixmap(size: int, draw) -> QPixmap:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    draw(painter, size)
    painter.end()
    return pixmap


def record_icon(size: int = 24) -> QIcon:
    def draw(p: QPainter, s: int) -> None:
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(RECORD_RED)
        margin = s * 0.18
        p.drawEllipse(QRectF(margin, margin, s - 2 * margin, s - 2 * margin))

    return QIcon(_pixmap(size, draw))


def stop_icon(size: int = 24) -> QIcon:
    def draw(p: QPainter, s: int) -> None:
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(STOP_RED)
        margin = s * 0.24
        p.drawRoundedRect(QRectF(margin, margin, s - 2 * margin, s - 2 * margin), 2, 2)

    return QIcon(_pixmap(size, draw))


def settings_icon(size: int = 24) -> QIcon:
    def draw(p: QPainter, s: int) -> None:
        center = QPointF(s / 2, s / 2)
        outer = s * 0.42
        inner = s * 0.30
        path = QPainterPath()
        teeth = 8
        import math

        points = []
        for i in range(teeth * 2):
            angle = math.pi * i / teeth
            radius = outer if i % 2 == 0 else inner
            points.append(
                QPointF(
                    center.x() + radius * math.cos(angle), center.y() + radius * math.sin(angle)
                )
            )
        path.addPolygon(QPolygonF(points))
        path.closeSubpath()
        hole = QPainterPath()
        hole.addEllipse(center, s * 0.12, s * 0.12)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(INK)
        p.drawPath(path.subtracted(hole))

    return QIcon(_pixmap(size, draw))


def _button_text_color() -> QColor:
    """Text color of buttons in the current theme (white in dark mode, dark in light)."""
    app = QGuiApplication.instance()
    if app is None:
        return QColor("#FFFFFF")
    return QGuiApplication.palette().color(QPalette.ColorRole.ButtonText)


def draw_icon(size: int = 24, color: QColor | None = None) -> QIcon:
    """A paint brush (handle, ferrule, bristles) in the button text color."""
    ink = QColor(color) if color is not None else _button_text_color()

    def draw(p: QPainter, s: int) -> None:
        p.translate(s / 2, s / 2)
        p.rotate(45)  # handle to the top right, bristle tip to the bottom left
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(ink)
        # Handle.
        p.drawRoundedRect(QRectF(-s * 0.06, -s * 0.48, s * 0.12, s * 0.40), s * 0.06, s * 0.06)
        # Ferrule (metal band), separated from handle and bristles by a small gap.
        p.drawRect(QRectF(-s * 0.09, -s * 0.05, s * 0.18, s * 0.11))
        # Bristles: a teardrop ending in a point.
        bristles = QPainterPath(QPointF(-s * 0.11, s * 0.09))
        bristles.cubicTo(
            QPointF(-s * 0.13, s * 0.24), QPointF(-s * 0.04, s * 0.36), QPointF(0, s * 0.47)
        )
        bristles.cubicTo(
            QPointF(s * 0.04, s * 0.36), QPointF(s * 0.13, s * 0.24), QPointF(s * 0.11, s * 0.09)
        )
        bristles.closeSubpath()
        p.drawPath(bristles)

    return QIcon(_pixmap(size, draw))


def pencil_icon(size: int = 24, color: QColor | None = None) -> QIcon:
    """A pencil (eraser, body, sharpened tip) in the button text color."""
    ink = QColor(color) if color is not None else _button_text_color()

    def draw(p: QPainter, s: int) -> None:
        p.translate(s / 2, s / 2)
        p.rotate(45)  # eraser to the top right, tip to the bottom left
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(ink)
        # Eraser, separated from the body by a small gap.
        p.drawRoundedRect(QRectF(-s * 0.11, -s * 0.47, s * 0.22, s * 0.12), s * 0.04, s * 0.04)
        # Body.
        p.drawRect(QRectF(-s * 0.11, -s * 0.31, s * 0.22, s * 0.50))
        # Sharpened tip.
        p.drawPolygon(
            QPolygonF(
                [QPointF(-s * 0.11, s * 0.23), QPointF(s * 0.11, s * 0.23), QPointF(0, s * 0.47)]
            )
        )

    return QIcon(_pixmap(size, draw))


def rectangle_icon(size: int = 24, color: QColor | None = None) -> QIcon:
    """An outlined rectangle: the Rect drawing tool."""
    ink = QColor(color) if color is not None else _button_text_color()

    def draw(p: QPainter, s: int) -> None:
        pen = QPen(ink, max(1.5, s * 0.10))
        pen.setJoinStyle(Qt.PenJoinStyle.MiterJoin)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(QRectF(s * 0.14, s * 0.24, s * 0.72, s * 0.52))

    return QIcon(_pixmap(size, draw))


def arrow_icon(size: int = 24, color: QColor | None = None) -> QIcon:
    """A diagonal arrow (tail bottom left, head top right): the Arrow drawing tool."""
    ink = QColor(color) if color is not None else _button_text_color()

    def draw(p: QPainter, s: int) -> None:
        pen = QPen(ink, max(1.5, s * 0.10))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.drawLine(QPointF(s * 0.16, s * 0.84), QPointF(s * 0.68, s * 0.32))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(ink)
        p.drawPolygon(
            QPolygonF(
                [
                    QPointF(s * 0.88, s * 0.12),
                    QPointF(s * 0.44, s * 0.24),
                    QPointF(s * 0.76, s * 0.56),
                ]
            )
        )

    return QIcon(_pixmap(size, draw))


def process_icon(size: int = 24) -> QIcon:
    """A white check mark on a green disc: finish and process the recording."""

    def draw(p: QPainter, s: int) -> None:
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(PROCESS_GREEN)
        margin = s * 0.08
        p.drawEllipse(QRectF(margin, margin, s - 2 * margin, s - 2 * margin))
        pen = QPen(QColor("#FFFFFF"), s * 0.12)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPolyline(
            QPolygonF(
                [
                    QPointF(s * 0.28, s * 0.52),
                    QPointF(s * 0.44, s * 0.67),
                    QPointF(s * 0.72, s * 0.36),
                ]
            )
        )

    return QIcon(_pixmap(size, draw))


def discard_icon(size: int = 24, color: QColor | None = None) -> QIcon:
    """A trash can (lid with handle, tapered bin with ribs): delete the recording."""
    ink = QColor(color) if color is not None else DISCARD_RED

    def draw(p: QPainter, s: int) -> None:
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(ink)
        # Handle and lid.
        p.drawRoundedRect(QRectF(s * 0.39, s * 0.10, s * 0.22, s * 0.10), s * 0.03, s * 0.03)
        p.drawRoundedRect(QRectF(s * 0.16, s * 0.18, s * 0.68, s * 0.09), s * 0.03, s * 0.03)
        # Bin with three ribs cut out.
        body = QPainterPath()
        body.addPolygon(
            QPolygonF(
                [
                    QPointF(s * 0.23, s * 0.32),
                    QPointF(s * 0.77, s * 0.32),
                    QPointF(s * 0.70, s * 0.90),
                    QPointF(s * 0.30, s * 0.90),
                ]
            )
        )
        body.closeSubpath()
        ribs = QPainterPath()
        for x in (0.36, 0.47, 0.58):
            ribs.addRect(QRectF(s * x, s * 0.40, s * 0.06, s * 0.42))
        p.drawPath(body.subtracted(ribs))

    return QIcon(_pixmap(size, draw))


KEYPOINT_GREEN = QColor("#3ddc84")


def keypoint_icon(size: int = 24, color: QColor | None = None) -> QIcon:
    """A green flag on a pole: mark the current moment as a keypoint."""
    ink = QColor(color) if color is not None else KEYPOINT_GREEN

    def draw(p: QPainter, s: int) -> None:
        pen = QPen(ink, max(1.5, s * 0.10))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.drawLine(QPointF(s * 0.26, s * 0.12), QPointF(s * 0.26, s * 0.90))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(ink)
        p.drawPolygon(
            QPolygonF(
                [
                    QPointF(s * 0.30, s * 0.14),
                    QPointF(s * 0.82, s * 0.32),
                    QPointF(s * 0.30, s * 0.50),
                ]
            )
        )

    return QIcon(_pixmap(size, draw))


def instructions_icon(size: int = 24, color: QColor | None = None) -> QIcon:
    """A sheet with text lines: the master instructions for the agent."""
    ink = QColor(color) if color is not None else _button_text_color()

    def draw(p: QPainter, s: int) -> None:
        pen = QPen(ink, max(1.2, s * 0.08))
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(QRectF(s * 0.22, s * 0.10, s * 0.56, s * 0.80), s * 0.06, s * 0.06)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        for y in (0.34, 0.50, 0.66):
            p.drawLine(QPointF(s * 0.34, s * y), QPointF(s * 0.66, s * y))

    return QIcon(_pixmap(size, draw))


# Application ----------------------------------------------------------------------

APP_BLUE = QColor("#1f6fd0")  # the editor's Render blue
APP_ICON_SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)


def _draw_app_icon(p: QPainter, s: int) -> None:
    """A white video camera with a red record light on a blue rounded square."""
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(APP_BLUE)
    p.drawRoundedRect(QRectF(0, 0, s, s), s * 0.22, s * 0.22)
    p.setBrush(QColor("#FFFFFF"))
    # Camera body.
    p.drawRoundedRect(QRectF(s * 0.12, s * 0.28, s * 0.52, s * 0.44), s * 0.08, s * 0.08)
    # Lens hood: a trapezoid opening to the right.
    p.drawPolygon(
        QPolygonF(
            [
                QPointF(s * 0.68, s * 0.44),
                QPointF(s * 0.88, s * 0.30),
                QPointF(s * 0.88, s * 0.70),
                QPointF(s * 0.68, s * 0.56),
            ]
        )
    )
    # Record light.
    p.setBrush(RECORD_RED)
    p.drawEllipse(QPointF(s * 0.38, s * 0.50), s * 0.10, s * 0.10)


def app_icon() -> QIcon:
    """The application icon (a video camera), in several sizes for title bars and the
    taskbar. Drawn here, so no image file or third-party licence is involved."""
    icon = QIcon()
    for size in APP_ICON_SIZES:
        icon.addPixmap(_pixmap(size, _draw_app_icon))
    return icon


def install_app_icon(app, app_id: str) -> None:
    """Give every window and dialog of ``app`` the camera icon.

    On Windows the taskbar would otherwise group the windows under ``python.exe`` and
    show its icon; an explicit AppUserModelID makes it use the window icon. Call this
    before the first window is shown.
    """
    import sys

    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
        except (AttributeError, OSError):  # pragma: no cover - very old Windows
            pass
    app.setWindowIcon(app_icon())


# Transport ------------------------------------------------------------------------


def _transport_pen(ink: QColor, s: int) -> QPen:
    pen = QPen(ink, max(1.5, s * 0.10))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    return pen


def _triangle(p: QPainter, s: int, left: float, right: float, tip_right: bool) -> None:
    top, bottom, mid = s * 0.22, s * 0.78, s * 0.50
    if tip_right:
        points = [QPointF(left, top), QPointF(right, mid), QPointF(left, bottom)]
    else:
        points = [QPointF(right, top), QPointF(left, mid), QPointF(right, bottom)]
    p.drawPolygon(QPolygonF(points))


def play_icon(size: int = 24, color: QColor | None = None) -> QIcon:
    """A right-pointing triangle."""
    ink = QColor(color) if color is not None else _button_text_color()

    def draw(p: QPainter, s: int) -> None:
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(ink)
        _triangle(p, s, s * 0.28, s * 0.80, tip_right=True)

    return QIcon(_pixmap(size, draw))


def pause_icon(size: int = 24, color: QColor | None = None) -> QIcon:
    """Two vertical bars."""
    ink = QColor(color) if color is not None else _button_text_color()

    def draw(p: QPainter, s: int) -> None:
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(ink)
        p.drawRoundedRect(QRectF(s * 0.26, s * 0.22, s * 0.16, s * 0.56), s * 0.03, s * 0.03)
        p.drawRoundedRect(QRectF(s * 0.58, s * 0.22, s * 0.16, s * 0.56), s * 0.03, s * 0.03)

    return QIcon(_pixmap(size, draw))


def stop_square_icon(size: int = 24, color: QColor | None = None) -> QIcon:
    """A square in the button text colour (the editor's Stop; the recorder's is red)."""
    ink = QColor(color) if color is not None else _button_text_color()

    def draw(p: QPainter, s: int) -> None:
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(ink)
        p.drawRoundedRect(QRectF(s * 0.26, s * 0.26, s * 0.48, s * 0.48), s * 0.04, s * 0.04)

    return QIcon(_pixmap(size, draw))


def skip_start_icon(size: int = 24, color: QColor | None = None) -> QIcon:
    """A bar and a left-pointing triangle: go to In."""
    ink = QColor(color) if color is not None else _button_text_color()

    def draw(p: QPainter, s: int) -> None:
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(ink)
        p.drawRoundedRect(QRectF(s * 0.18, s * 0.22, s * 0.12, s * 0.56), s * 0.03, s * 0.03)
        _triangle(p, s, s * 0.36, s * 0.84, tip_right=False)

    return QIcon(_pixmap(size, draw))


def skip_end_icon(size: int = 24, color: QColor | None = None) -> QIcon:
    """A right-pointing triangle and a bar: go to Out."""
    ink = QColor(color) if color is not None else _button_text_color()

    def draw(p: QPainter, s: int) -> None:
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(ink)
        _triangle(p, s, s * 0.16, s * 0.64, tip_right=True)
        p.drawRoundedRect(QRectF(s * 0.70, s * 0.22, s * 0.12, s * 0.56), s * 0.03, s * 0.03)

    return QIcon(_pixmap(size, draw))


def step_back_icon(size: int = 24, color: QColor | None = None) -> QIcon:
    """A chevron pointing left: one frame back."""
    ink = QColor(color) if color is not None else _button_text_color()

    def draw(p: QPainter, s: int) -> None:
        p.setPen(_transport_pen(ink, s))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPolyline(
            QPolygonF([QPointF(s * 0.62, s * 0.22), QPointF(s * 0.36, s * 0.50),
                       QPointF(s * 0.62, s * 0.78)])
        )  # fmt: skip

    return QIcon(_pixmap(size, draw))


def step_forward_icon(size: int = 24, color: QColor | None = None) -> QIcon:
    """A chevron pointing right: one frame forward."""
    ink = QColor(color) if color is not None else _button_text_color()

    def draw(p: QPainter, s: int) -> None:
        p.setPen(_transport_pen(ink, s))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPolyline(
            QPolygonF([QPointF(s * 0.38, s * 0.22), QPointF(s * 0.64, s * 0.50),
                       QPointF(s * 0.38, s * 0.78)])
        )  # fmt: skip

    return QIcon(_pixmap(size, draw))


def sun_icon(size: int = 24, color: QColor | None = None) -> QIcon:
    """A sun (disc with rays): switch to the light theme."""
    ink = QColor(color) if color is not None else _button_text_color()

    def draw(p: QPainter, s: int) -> None:
        import math

        center = QPointF(s / 2, s / 2)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(ink)
        p.drawEllipse(center, s * 0.18, s * 0.18)
        p.setPen(_transport_pen(ink, s))
        for i in range(8):
            angle = math.pi * i / 4
            inner, outer = s * 0.30, s * 0.42
            p.drawLine(
                QPointF(center.x() + inner * math.cos(angle), center.y() + inner * math.sin(angle)),
                QPointF(center.x() + outer * math.cos(angle), center.y() + outer * math.sin(angle)),
            )

    return QIcon(_pixmap(size, draw))


def moon_icon(size: int = 24, color: QColor | None = None) -> QIcon:
    """A crescent moon: switch to the dark theme."""
    ink = QColor(color) if color is not None else _button_text_color()

    def draw(p: QPainter, s: int) -> None:
        disc = QPainterPath()
        disc.addEllipse(QPointF(s * 0.50, s * 0.50), s * 0.34, s * 0.34)
        bite = QPainterPath()
        bite.addEllipse(QPointF(s * 0.64, s * 0.40), s * 0.30, s * 0.30)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(ink)
        p.drawPath(disc.subtracted(bite))

    return QIcon(_pixmap(size, draw))


def hide_to_tray_icon(size: int = 24, color: QColor | None = None) -> QIcon:
    """An arrow pointing down into an open tray: hide the window to the system tray."""
    ink = QColor(color) if color is not None else _button_text_color()

    def draw(p: QPainter, s: int) -> None:
        pen = _transport_pen(ink, s)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        # The tray: a U open at the top.
        p.drawPolyline(
            QPolygonF([QPointF(s * 0.16, s * 0.56), QPointF(s * 0.16, s * 0.84),
                       QPointF(s * 0.84, s * 0.84), QPointF(s * 0.84, s * 0.56)])
        )  # fmt: skip
        # The arrow: a shaft and a head reaching into the tray.
        p.drawLine(QPointF(s * 0.50, s * 0.12), QPointF(s * 0.50, s * 0.56))
        p.drawPolyline(
            QPolygonF([QPointF(s * 0.32, s * 0.44), QPointF(s * 0.50, s * 0.64),
                       QPointF(s * 0.68, s * 0.44)])
        )  # fmt: skip

    return QIcon(_pixmap(size, draw))


def refresh_icon(size: int = 24, color: QColor | None = None) -> QIcon:
    """A circular arrow (the usual Refresh / reload sign)."""
    ink = QColor(color) if color is not None else _button_text_color()

    def draw(p: QPainter, s: int) -> None:
        import math

        center = QPointF(s / 2, s / 2)
        radius = s * 0.30
        pen = _transport_pen(ink, s)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        box = QRectF(center.x() - radius, center.y() - radius, 2 * radius, 2 * radius)
        start_deg, span_deg = 60, 285  # open at the top right, where the head sits
        p.drawArc(box, start_deg * 16, span_deg * 16)
        end = math.radians(start_deg + span_deg)
        tip = QPointF(center.x() + radius * math.cos(end), center.y() - radius * math.sin(end))
        # Arrow head pointing along the arc: Qt's arc runs counter-clockwise, which in
        # screen coordinates (y down) is the direction (-sin, -cos).
        tx, ty = -math.sin(end), -math.cos(end)
        nx, ny = -ty, tx
        head = s * 0.18
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(ink)
        p.drawPolygon(
            QPolygonF(
                [
                    QPointF(tip.x() + tx * head * 0.8, tip.y() + ty * head * 0.8),
                    QPointF(tip.x() + nx * head * 0.6, tip.y() + ny * head * 0.6),
                    QPointF(tip.x() - nx * head * 0.6, tip.y() - ny * head * 0.6),
                ]
            )
        )

    return QIcon(_pixmap(size, draw))


def search_icon(size: int = 24, color: QColor | None = None) -> QIcon:
    """A magnifying glass (lens and handle)."""
    ink = QColor(color) if color is not None else _button_text_color()

    def draw(p: QPainter, s: int) -> None:
        pen = _transport_pen(ink, s)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        lens = QPointF(s * 0.43, s * 0.43)
        radius = s * 0.22
        p.drawEllipse(lens, radius, radius)
        handle = QPen(pen)
        handle.setWidthF(pen.widthF() * 1.5)
        p.setPen(handle)
        offset = radius * 0.7071
        p.drawLine(
            QPointF(lens.x() + offset + s * 0.02, lens.y() + offset + s * 0.02),
            QPointF(s * 0.82, s * 0.82),
        )

    return QIcon(_pixmap(size, draw))


def new_folder_icon(size: int = 24, color: QColor | None = None) -> QIcon:
    """A folder with a plus sign (Create new folder)."""
    ink = QColor(color) if color is not None else _button_text_color()

    def draw(p: QPainter, s: int) -> None:
        pen = _transport_pen(ink, s)
        pen.setWidthF(max(1.2, s * 0.075))
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        left, right = s * 0.10, s * 0.82
        top, bottom = s * 0.22, s * 0.80
        tab_end, body_top = s * 0.40, s * 0.32
        outline = QPainterPath()
        outline.moveTo(left, bottom)
        outline.lineTo(left, top)
        outline.lineTo(tab_end, top)
        outline.lineTo(tab_end + s * 0.07, body_top)
        outline.lineTo(right, body_top)
        outline.lineTo(right, s * 0.50)
        outline.moveTo(s * 0.56, bottom)
        outline.lineTo(left, bottom)
        p.drawPath(outline)
        # the plus in the bottom right corner
        cx, cy, arm = s * 0.76, s * 0.74, s * 0.16
        plus = QPen(pen)
        plus.setWidthF(pen.widthF() * 1.15)
        p.setPen(plus)
        p.drawLine(QPointF(cx - arm, cy), QPointF(cx + arm, cy))
        p.drawLine(QPointF(cx, cy - arm), QPointF(cx, cy + arm))

    return QIcon(_pixmap(size, draw))


__all__ = [
    "KEYPOINT_GREEN",
    "new_folder_icon",
    "app_icon",
    "install_app_icon",
    "arrow_icon",
    "discard_icon",
    "draw_icon",
    "hide_to_tray_icon",
    "instructions_icon",
    "keypoint_icon",
    "moon_icon",
    "pause_icon",
    "pencil_icon",
    "play_icon",
    "process_icon",
    "record_icon",
    "rectangle_icon",
    "refresh_icon",
    "search_icon",
    "settings_icon",
    "skip_end_icon",
    "skip_start_icon",
    "step_back_icon",
    "step_forward_icon",
    "stop_icon",
    "stop_square_icon",
    "sun_icon",
]
