"""Real-desktop test: drawings on the overlay are visible to screen capture (mss).

Must run on a visible desktop (not ``QT_QPA_PLATFORM=offscreen``). The overlay is
shown on every screen, a red brush stroke and a red rectangle are added in a known
area, the screens are grabbed with :class:`ScreenGrabber` and pixels along the drawn
lines are checked. On Windows it also checks that the overlay window, not the
application below it, receives mouse input at the drawn location (not click-through).
"""

from __future__ import annotations

import ctypes
import sys
from collections.abc import Iterator

import pytest
from PySide6.QtCore import QPointF
from PySide6.QtGui import QColor, QGuiApplication, QScreen
from PySide6.QtWidgets import QApplication

from video_story.capture.screen_grabber import ScreenGrabber, frame_to_image
from video_story.core.screens import Screen, assign_screen_ids
from video_story.platform import get_platform
from video_story.ui.overlay import DrawingOverlay, Shape, Tool

pytestmark = pytest.mark.integration

# Logical coordinates inside each overlay window, well away from the toolbar corner.
STROKE_Y = 260
STROKE_X0, STROKE_X1 = 120, 420
RECT_LEFT, RECT_TOP, RECT_RIGHT, RECT_BOTTOM = 140, 320, 440, 480


def _is_red(rgb: tuple[int, int, int]) -> bool:
    r, g, b = rgb
    return r >= 200 and g <= 80 and b <= 80


def _match_qscreen(screen: Screen) -> QScreen | None:
    """Find the QScreen that corresponds to a platform screen (physical pixels)."""
    if screen.primary:
        return QGuiApplication.primaryScreen()
    for candidate in QGuiApplication.screens():
        if candidate is QGuiApplication.primaryScreen():
            continue
        dpr = candidate.devicePixelRatio()
        size = candidate.geometry().size()
        if (
            round(size.width() * dpr) == screen.width
            and round(size.height() * dpr) == screen.height
        ):
            return candidate
    return None


def _samples_x(x0: int, x1: int, step: int = 20) -> Iterator[int]:
    yield from range(x0 + 10, x1 - 9, step)


@pytest.fixture
def overlay(qapp: QApplication, qtbot) -> Iterator[DrawingOverlay]:
    assert qapp.platformName() != "offscreen", "needs a real desktop"
    overlay = DrawingOverlay()
    try:
        yield overlay
    finally:
        overlay.close()
        qtbot.wait(150)


def test_drawing_is_visible_in_captured_frames(overlay: DrawingOverlay, qtbot) -> None:
    overlay.show()
    qtbot.wait(200)
    red = QColor("#FF0000")
    for index in range(len(overlay.windows)):
        overlay.add_shape(
            Shape(
                Tool.BRUSH, red, [QPointF(STROKE_X0, STROKE_Y), QPointF(STROKE_X1, STROKE_Y)], index
            )
        )
        overlay.add_shape(
            Shape(
                Tool.RECTANGLE,
                red,
                [QPointF(RECT_LEFT, RECT_TOP), QPointF(RECT_RIGHT, RECT_BOTTOM)],
                index,
            )
        )
    qtbot.wait(300)

    platform = get_platform()
    screens = assign_screen_ids(platform.list_screens())
    checked = 0
    with ScreenGrabber() as grabber:
        for screen in screens:
            qscreen = _match_qscreen(screen)
            if qscreen is None:
                continue
            pairs = zip(overlay.windows, QGuiApplication.screens(), strict=False)
            window = next((w for w, s in pairs if s is qscreen), None)
            assert window is not None, f"no overlay window for {screen.id}"
            dpr = qscreen.devicePixelRatio()
            image = frame_to_image(grabber.grab(screen)).convert("RGB")
            assert image.size == (screen.width, screen.height)
            _check_drawing(screen, image, dpr)
            if sys.platform.startswith("win"):
                _assert_window_receives_input(window, screen, dpr)
            checked += 1
    assert checked >= 1, "no screen could be matched between Qt and the platform layer"


def _check_drawing(screen: Screen, image, dpr: float) -> None:
    """Assert the stroke and the rectangle outline are red in the captured image."""

    def px(lx: float, ly: float) -> tuple[int, int, int]:
        return image.getpixel((round(lx * dpr), round(ly * dpr)))

    # Brush stroke: red along the centre line, not red a bit above it.
    for x in _samples_x(STROKE_X0, STROKE_X1):
        assert _is_red(px(x, STROKE_Y)), f"{screen.id}: stroke at x={x} is {px(x, STROKE_Y)}"
    above = [px(x, STROKE_Y - 12) for x in _samples_x(STROKE_X0, STROKE_X1)]
    assert not all(_is_red(p) for p in above), f"{screen.id}: desktop above stroke is red"

    # Rectangle outline: red along all four edges, not inside.
    for x in _samples_x(RECT_LEFT, RECT_RIGHT):
        assert _is_red(px(x, RECT_TOP)), f"{screen.id}: top edge x={x}"
        assert _is_red(px(x, RECT_BOTTOM)), f"{screen.id}: bottom edge x={x}"
    for y in range(RECT_TOP + 10, RECT_BOTTOM - 9, 20):
        assert _is_red(px(RECT_LEFT, y)), f"{screen.id}: left edge y={y}"
        assert _is_red(px(RECT_RIGHT, y)), f"{screen.id}: right edge y={y}"
    inside = [px(x, (RECT_TOP + RECT_BOTTOM) / 2) for x in _samples_x(RECT_LEFT, RECT_RIGHT)]
    assert not all(_is_red(p) for p in inside), f"{screen.id}: rectangle is filled"


def _assert_window_receives_input(window, screen: Screen, dpr: float) -> None:
    """Windows: the overlay window owns the point under the stroke (not click-through)."""

    class POINT(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

    user32 = ctypes.windll.user32
    user32.WindowFromPoint.restype = ctypes.c_void_p
    user32.WindowFromPoint.argtypes = [POINT]
    user32.GetAncestor.restype = ctypes.c_void_p
    user32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    GA_ROOT = 2
    for lx, ly in ((STROKE_X0 + 50, STROKE_Y), (RECT_LEFT + 80, RECT_TOP + 40)):
        point = POINT(screen.x + round(lx * dpr), screen.y + round(ly * dpr))
        hwnd = user32.WindowFromPoint(point)
        root = user32.GetAncestor(hwnd, GA_ROOT) if hwnd else None
        assert root == int(window.winId()), (
            f"{screen.id}: window under ({point.x}, {point.y}) is {root!r}, "
            f"expected overlay {int(window.winId())}"
        )
