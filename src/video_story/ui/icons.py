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


__all__ = [
    "arrow_icon",
    "discard_icon",
    "draw_icon",
    "pencil_icon",
    "process_icon",
    "record_icon",
    "rectangle_icon",
    "settings_icon",
    "stop_icon",
]
