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
    QPixmap,
    QPolygonF,
)

RECORD_RED = QColor("#E53935")
STOP_RED = QColor("#C62828")
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


__all__ = ["draw_icon", "record_icon", "settings_icon", "stop_icon"]
