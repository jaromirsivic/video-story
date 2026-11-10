"""The mouse in the box left of the transport buttons (see :mod:`.mouse_activity`); a click
opens the Mouse window (:mod:`.mouse_details_dialog`)."""

from __future__ import annotations

from PySide6.QtCore import QByteArray, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QMouseEvent, QPainter, QPaintEvent, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QSizePolicy, QWidget

from .mouse_activity import SVG_SIZE, mouse_svg

MOUSE_WIDGET_SIZE = QSize(96, 94)  # spans the transport, time and subtitle rows
OUTLINE_PX = 1.3  # outline width on screen, whatever the widget size
CACHE_SIZE = 64


class MouseWidget(QWidget):
    """Draws the mouse with the controls in use at the playhead filled; clickable."""

    clicked = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("mouseView")
        self.setFixedSize(MOUSE_WIDGET_SIZE)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._dark = True
        self._recorded = False
        self._lit: frozenset[str] = frozenset()
        self._cache: dict[tuple, QPixmap] = {}
        self._update_tooltip()

    @property
    def lit(self) -> frozenset[str]:
        return self._lit

    @property
    def recorded(self) -> bool:
        return self._recorded

    @property
    def dark(self) -> bool:
        return self._dark

    def set_dark(self, dark: bool) -> None:
        if dark != self._dark:
            self._dark = dark
            self.update()

    def set_recorded(self, recorded: bool) -> None:
        """Without mouse data the picture is drawn faint and nothing lights up."""
        if recorded != self._recorded:
            self._recorded = recorded
            if not recorded:
                self._lit = frozenset()
            self._update_tooltip()
            self.update()

    def set_lit(self, lit: frozenset[str]) -> None:
        lit = frozenset(lit) if self._recorded else frozenset()
        if lit != self._lit:
            self._lit = lit
            self.update()

    def _update_tooltip(self) -> None:
        self.setToolTip(
            "Mouse at the playhead: a pressed button is red, a turning wheel and its"
            " direction yellow. Click for everything recorded about it."
            if self._recorded
            else "Mouse actions were not recorded. Click for the clipboard."
        )

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            event.accept()
            self.clicked.emit()
            return
        super().mousePressEvent(event)

    def target_rect(self) -> QRectF:
        """The picture's place in the widget, keeping its proportions."""
        svg_w, svg_h = SVG_SIZE
        scale = min(self.width() / svg_w, self.height() / svg_h)
        w, h = svg_w * scale, svg_h * scale
        return QRectF((self.width() - w) / 2, (self.height() - h) / 2, w, h)

    def picture(self) -> QPixmap:
        rect = self.target_rect()
        ratio = self.devicePixelRatioF()
        key = (self._lit, self._dark, round(rect.width()), round(rect.height()), ratio)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        # The 4 px lines of the 756 px drawing would vanish at ~100 px: keep them visible.
        stroke = OUTLINE_PX * SVG_SIZE[0] / max(1.0, rect.width())
        renderer = QSvgRenderer(QByteArray(mouse_svg(self._lit, self._dark, stroke)))
        pixmap = QPixmap(max(1, round(rect.width() * ratio)), max(1, round(rect.height() * ratio)))
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        renderer.render(painter)
        painter.end()
        pixmap.setDevicePixelRatio(ratio)
        if len(self._cache) >= CACHE_SIZE:
            self._cache.clear()
        self._cache[key] = pixmap
        return pixmap

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        painter = QPainter(self)
        if not self._recorded:
            painter.setOpacity(0.35)
        painter.drawPixmap(self.target_rect().topLeft(), self.picture())
        painter.end()


__all__ = ["MOUSE_WIDGET_SIZE", "MouseWidget"]
