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
from PySide6.QtCore import QPointF, Qt
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


def test_sketch_canvas_is_white_in_captured_frames(overlay: DrawingOverlay, qtbot) -> None:
    """Sketch: every screen is captured as a white canvas with the shapes on top."""
    overlay.show()
    qtbot.wait(200)
    red = QColor("#FF0000")
    for index in range(len(overlay.windows)):
        overlay.add_shape(
            Shape(
                Tool.BRUSH, red, [QPointF(STROKE_X0, STROKE_Y), QPointF(STROKE_X1, STROKE_Y)], index
            )
        )
    overlay.set_sketch(True)
    assert overlay.background() == "sketch"
    qtbot.wait(300)

    platform = get_platform()
    screens = assign_screen_ids(platform.list_screens())
    checked = 0
    with ScreenGrabber() as grabber:
        for screen in screens:
            qscreen = _match_qscreen(screen)
            if qscreen is None:
                continue
            dpr = qscreen.devicePixelRatio()
            image = frame_to_image(grabber.grab(screen)).convert("RGB")

            def px(lx: float, ly: float, image=image, dpr=dpr) -> tuple[int, int, int]:
                return image.getpixel((round(lx * dpr), round(ly * dpr)))

            # Away from the stroke, the toolbar (top left) and the windows kept above.
            for x in _samples_x(STROKE_X0, STROKE_X1):
                assert px(x, STROKE_Y + 120) == (255, 255, 255), f"{screen.id} at x={x}"
                assert _is_red(px(x, STROKE_Y)), f"{screen.id}: stroke at x={x}"
            checked += 1
    assert checked >= 1, "no screen could be matched between Qt and the platform layer"
    overlay.set_sketch(False)
    assert len(overlay.shapes()) == len(overlay.windows)  # switching removed nothing


def test_line_being_drawn_is_captured_on_every_screen_it_crosses(
    overlay: DrawingOverlay, qtbot
) -> None:
    """Connected lines wait without a button: the line is on screen (and in the frames)
    on every screen it crosses, and is split at the edges once finished."""
    overlay.show()
    qtbot.wait(200)
    windows = overlay.windows
    first = windows[0]
    start = QPointF(first.mapToGlobal(QPointF(STROKE_X0, STROKE_Y)))
    if len(windows) > 1:
        end = QPointF(windows[1].geometry().center())  # into the second screen
    else:
        end = QPointF(first.mapToGlobal(QPointF(STROKE_X1, STROKE_Y)))
    overlay._begin_span(first, Tool.LINE, Qt.MouseButton.LeftButton, start)
    overlay._move_span(end)
    overlay._span.button = Qt.MouseButton.NoButton  # waiting, as after a Shift release
    qtbot.wait(300)

    def red_samples() -> dict[int, list[bool]]:
        """Per screen index: is the line red at points along it in the captured image."""
        seen: dict[int, list[bool]] = {}
        screens = assign_screen_ids(get_platform().list_screens())
        with ScreenGrabber() as grabber:
            for screen in screens:
                qscreen = _match_qscreen(screen)
                if qscreen is None:
                    continue
                pairs = zip(windows, QGuiApplication.screens(), strict=False)
                window = next((w for w, s in pairs if s is qscreen), None)
                if window is None:
                    continue
                image = frame_to_image(grabber.grab(screen)).convert("RGB")
                dpr = qscreen.devicePixelRatio()
                # Qt maps global points through the screen they are on (native pixels), so
                # the line is straight in each window's own coordinates: sample it there.
                a = QPointF(window.mapFromGlobal(start))
                b = QPointF(window.mapFromGlobal(end))
                for step in range(1, 80):
                    local = a + (b - a) * (step / 80)
                    if (
                        4 <= local.x() <= window.width() - 4
                        and 4 <= local.y() <= window.height() - 4
                    ):
                        pixel = image.getpixel((round(local.x() * dpr), round(local.y() * dpr)))
                        seen.setdefault(window.screen_index, []).append(_is_red(pixel))
        return seen

    def mostly_red(seen: dict[int, list[bool]]) -> bool:
        # Allow a few samples hidden by windows kept above the overlay.
        return all(sum(hits) >= 0.8 * len(hits) for hits in seen.values())

    seen = red_samples()
    for _attempt in range(5):  # a screen may repaint a little later than the other
        if mostly_red(seen):
            break
        qtbot.wait(200)
        seen = red_samples()
    assert seen, "no screen could be matched between Qt and the platform layer"
    assert mostly_red(seen), {index: f"{sum(h)}/{len(h)} red" for index, h in seen.items()}

    overlay._finish_span()
    pieces = overlay.shapes()
    assert {s.screen_index for s in pieces} == set(seen)
    if len(pieces) > 1:
        assert len({s.group for s in pieces}) == 1 and pieces[0].group is not None
