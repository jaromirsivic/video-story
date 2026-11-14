"""Manual check of the drawing overlay: ``uv run python tests/manual/overlay_demo.py``.

Opens the overlay on every screen with the floating toolbar on the primary screen.
Try the tools (rectangle, ellipse, line, brush, arrow, eraser), Shift with them (square,
circle, closed brush shape, connected lines), lines and arrows across monitors, the
palette, Sketch (S), Snap to grid (Ctrl+G), Clear (Ctrl+Alt+C) and leave with Esc or the
toolbar's cross; the script then exits. Take a screenshot while drawing (for example
with ``video-story record``) to confirm the drawing is captured.
"""

from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

from video_story.platform import get_platform
from video_story.ui.overlay import DrawingOverlay


def main() -> int:
    get_platform().prepare_process()
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    overlay = DrawingOverlay()
    overlay.closed.connect(lambda: print("overlay closed by the user"))
    overlay.closed.connect(app.quit)
    overlay.show()
    print(f"overlay shown on {len(overlay.windows)} screen(s); press Esc to finish")
    code = app.exec()
    print(f"{len(overlay.shapes())} shape(s) were drawn")
    overlay.close()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
