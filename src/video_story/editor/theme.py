"""Light / dark theme of the editor window (the button above **Edit** toggles it).

Qt follows the system theme by default; once the user picks a theme in the editor it is
applied to the whole application with the Fusion style and a palette built here, and
remembered in the editor's settings (``view/dark``).
"""

from __future__ import annotations

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

DARK = "dark"
LIGHT = "light"


def system_prefers_dark(app: QApplication | None = None) -> bool:
    """True when the palette Qt started with is a dark one."""
    app = app or QApplication.instance()  # type: ignore[assignment]
    if app is None:
        return True
    return app.palette().color(QPalette.ColorRole.Window).lightness() < 128


def dark_palette() -> QPalette:
    p = QPalette()
    window = QColor("#2b2b2b")
    base = QColor("#1f1f1f")
    text = QColor("#e6e6e6")
    disabled = QColor("#7a7a7a")
    highlight = QColor("#3b7dd8")
    for group in (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive):
        p.setColor(group, QPalette.ColorRole.Window, window)
        p.setColor(group, QPalette.ColorRole.WindowText, text)
        p.setColor(group, QPalette.ColorRole.Base, base)
        p.setColor(group, QPalette.ColorRole.AlternateBase, QColor("#262626"))
        p.setColor(group, QPalette.ColorRole.ToolTipBase, QColor("#3a3a3a"))
        p.setColor(group, QPalette.ColorRole.ToolTipText, text)
        p.setColor(group, QPalette.ColorRole.Text, text)
        p.setColor(group, QPalette.ColorRole.PlaceholderText, disabled)
        p.setColor(group, QPalette.ColorRole.Button, QColor("#3a3a3a"))
        p.setColor(group, QPalette.ColorRole.ButtonText, text)
        p.setColor(group, QPalette.ColorRole.BrightText, QColor("#ff6060"))
        p.setColor(group, QPalette.ColorRole.Highlight, highlight)
        p.setColor(group, QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
        p.setColor(group, QPalette.ColorRole.Link, QColor("#6ea8ff"))
        p.setColor(group, QPalette.ColorRole.Light, QColor("#4a4a4a"))
        p.setColor(group, QPalette.ColorRole.Midlight, QColor("#404040"))
        p.setColor(group, QPalette.ColorRole.Mid, QColor("#353535"))
        p.setColor(group, QPalette.ColorRole.Dark, QColor("#1a1a1a"))
        p.setColor(group, QPalette.ColorRole.Shadow, QColor("#101010"))
    dis = QPalette.ColorGroup.Disabled
    p.setColor(dis, QPalette.ColorRole.WindowText, disabled)
    p.setColor(dis, QPalette.ColorRole.Text, disabled)
    p.setColor(dis, QPalette.ColorRole.ButtonText, disabled)
    p.setColor(dis, QPalette.ColorRole.Highlight, QColor("#505050"))
    p.setColor(dis, QPalette.ColorRole.HighlightedText, disabled)
    return p


def light_palette() -> QPalette:
    p = QPalette()
    window = QColor("#f3f3f3")
    base = QColor("#ffffff")
    text = QColor("#1e1e1e")
    disabled = QColor("#9a9a9a")
    highlight = QColor("#2f6fd0")
    for group in (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive):
        p.setColor(group, QPalette.ColorRole.Window, window)
        p.setColor(group, QPalette.ColorRole.WindowText, text)
        p.setColor(group, QPalette.ColorRole.Base, base)
        p.setColor(group, QPalette.ColorRole.AlternateBase, QColor("#f7f7f7"))
        p.setColor(group, QPalette.ColorRole.ToolTipBase, QColor("#ffffdc"))
        p.setColor(group, QPalette.ColorRole.ToolTipText, text)
        p.setColor(group, QPalette.ColorRole.Text, text)
        p.setColor(group, QPalette.ColorRole.PlaceholderText, disabled)
        p.setColor(group, QPalette.ColorRole.Button, QColor("#e9e9e9"))
        p.setColor(group, QPalette.ColorRole.ButtonText, text)
        p.setColor(group, QPalette.ColorRole.BrightText, QColor("#d02020"))
        p.setColor(group, QPalette.ColorRole.Highlight, highlight)
        p.setColor(group, QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
        p.setColor(group, QPalette.ColorRole.Link, QColor("#1f5fc0"))
        p.setColor(group, QPalette.ColorRole.Light, QColor("#ffffff"))
        p.setColor(group, QPalette.ColorRole.Midlight, QColor("#f0f0f0"))
        p.setColor(group, QPalette.ColorRole.Mid, QColor("#c8c8c8"))
        p.setColor(group, QPalette.ColorRole.Dark, QColor("#a0a0a0"))
        p.setColor(group, QPalette.ColorRole.Shadow, QColor("#707070"))
    dis = QPalette.ColorGroup.Disabled
    p.setColor(dis, QPalette.ColorRole.WindowText, disabled)
    p.setColor(dis, QPalette.ColorRole.Text, disabled)
    p.setColor(dis, QPalette.ColorRole.ButtonText, disabled)
    p.setColor(dis, QPalette.ColorRole.Highlight, QColor("#c8c8c8"))
    p.setColor(dis, QPalette.ColorRole.HighlightedText, disabled)
    return p


def apply_theme(dark: bool, app: QApplication | None = None) -> None:
    """Switch the whole application to the dark or the light palette."""
    app = app or QApplication.instance()  # type: ignore[assignment]
    if app is None:
        return
    if app.style().objectName().lower() != "fusion":
        app.setStyle("Fusion")  # the only style that honours a custom palette everywhere
    app.setPalette(dark_palette() if dark else light_palette())


__all__ = ["DARK", "LIGHT", "apply_theme", "dark_palette", "light_palette", "system_prefers_dark"]
