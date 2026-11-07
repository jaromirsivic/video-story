"""Vector icons drawn with QPainter (no image assets needed)."""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap, QPolygonF

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


def draw_icon(size: int = 24) -> QIcon:
    def draw(p: QPainter, s: int) -> None:
        pen = QPen(
            INK, s * 0.11, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin
        )
        p.setPen(pen)
        p.drawLine(QPointF(s * 0.22, s * 0.78), QPointF(s * 0.72, s * 0.28))
        p.setPen(QPen(RECORD_RED, s * 0.11, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawLine(QPointF(s * 0.72, s * 0.28), QPointF(s * 0.80, s * 0.20))
        p.setPen(QPen(INK, s * 0.07, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawLine(QPointF(s * 0.18, s * 0.86), QPointF(s * 0.40, s * 0.86))

    return QIcon(_pixmap(size, draw))


__all__ = ["draw_icon", "record_icon", "settings_icon", "stop_icon"]
