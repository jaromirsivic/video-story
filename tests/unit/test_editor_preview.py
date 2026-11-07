"""Offscreen tests for the preview zoom (spec v2, chapter 4): wheel, anchor, pan, reset."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path  # noqa: E402

import pytest  # noqa: E402
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QImage, QMouseEvent, QWheelEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from video_story.core.cursor import CursorPosition  # noqa: E402
from video_story.editor.preview import (  # noqa: E402
    MAX_ZOOM,
    ZOOM_STEP,
    PreviewPanel,
    PreviewWidget,
)
from video_story.host.runner import FrameRef  # noqa: E402

WIDGET_W, WIDGET_H = 640, 400
IMAGE_W, IMAGE_H = 320, 200  # fits at exactly 200 %
FILL = QColor(120, 20, 20)


def make_frame(
    root: Path, name: str, width: int = IMAGE_W, height: int = IMAGE_H, screen_id: str = "screen1"
) -> FrameRef:
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(FILL)
    path = root / f"{name}.png"
    assert image.save(str(path))
    sidecar = root / f"{name}.json"
    sidecar.write_text("{}", encoding="utf-8")
    return FrameRef(screen_id=screen_id, time_ms=0, image=path, sidecar=sidecar)


def show(qtbot, view: PreviewWidget, ref: FrameRef, cursor: CursorPosition | None = None) -> None:
    view.set_frame(ref, cursor, ref.screen_id)
    qtbot.waitUntil(lambda: view.shown_path == str(ref.image), timeout=10_000)


def wheel(view: PreviewWidget, pos: tuple[float, float], notches: float) -> None:
    point = QPointF(*pos)
    event = QWheelEvent(
        point,
        QPointF(view.mapToGlobal(point.toPoint())),
        QPoint(),
        QPoint(0, round(notches * 120)),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )
    QApplication.sendEvent(view, event)


def mouse(view: PreviewWidget, kind: QEvent.Type, pos: tuple[float, float], pressed: bool) -> None:
    local = QPointF(*pos)
    event = QMouseEvent(
        kind,
        local,
        QPointF(view.mapToGlobal(local.toPoint())),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton if pressed else Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )
    QApplication.sendEvent(view, event)


def drag(view: PreviewWidget, start: tuple[float, float], end: tuple[float, float]) -> None:
    mouse(view, QEvent.Type.MouseButtonPress, start, True)
    mouse(view, QEvent.Type.MouseMove, end, True)
    mouse(view, QEvent.Type.MouseButtonRelease, end, False)


@pytest.fixture
def view(qtbot):
    widget = PreviewWidget()
    qtbot.addWidget(widget)
    widget.resize(WIDGET_W, WIDGET_H)
    widget.show()
    return widget


@pytest.fixture
def shown(qtbot, view, tmp_path):
    show(qtbot, view, make_frame(tmp_path, "a"))
    return view


def test_fit_is_the_default_and_centres_the_image(shown):
    assert shown.at_fit
    assert shown.fit_zoom == pytest.approx(2.0)
    assert shown.zoom == pytest.approx(2.0)
    assert shown.image_origin() == QPointF(0, 0)
    assert shown.map_to_image(QPointF(640, 400)) == QPointF(IMAGE_W, IMAGE_H)


def test_wheel_up_zooms_in_by_one_step_and_wheel_down_back_out(shown, qtbot):
    with qtbot.waitSignal(shown.zoom_changed) as blocker:
        wheel(shown, (320, 200), 1)
    assert blocker.args == [pytest.approx(2.0 * ZOOM_STEP)]
    assert shown.zoom == pytest.approx(2.0 * ZOOM_STEP)
    assert not shown.at_fit
    wheel(shown, (320, 200), 1)
    assert shown.zoom == pytest.approx(2.0 * ZOOM_STEP**2)
    wheel(shown, (320, 200), -1)
    assert shown.zoom == pytest.approx(2.0 * ZOOM_STEP)
    wheel(shown, (320, 200), -1)
    assert shown.at_fit and shown.zoom == pytest.approx(2.0)


def test_fractional_wheel_deltas_scale_proportionally(shown):
    wheel(shown, (320, 200), 0.5)
    assert shown.zoom == pytest.approx(2.0 * ZOOM_STEP**0.5)
    wheel(shown, (320, 200), 0.5)
    assert shown.zoom == pytest.approx(2.0 * ZOOM_STEP)


def test_zoom_clamps_at_800_percent(shown):
    for _ in range(40):
        wheel(shown, (100, 100), 1)
    assert shown.zoom == pytest.approx(MAX_ZOOM)
    shown.zoom_at(QPointF(100, 100), 3.0)
    assert shown.zoom == pytest.approx(MAX_ZOOM)


def test_zooming_out_below_fit_snaps_to_fit_and_centres(shown):
    shown.zoom_at(QPointF(10, 10), 2.0)
    assert shown.zoom == pytest.approx(4.0)
    assert shown.image_origin() != QPointF(0, 0)
    wheel(shown, (600, 390), -1)  # 4.0 / 1.25 = 3.2, still above fit
    assert shown.zoom == pytest.approx(3.2)
    wheel(shown, (600, 390), -2)  # 3.2 / 1.5625 = 2.048 > 2.0, then one more notch
    assert shown.zoom == pytest.approx(2.048)
    wheel(shown, (600, 390), -1)
    assert shown.at_fit
    assert shown.zoom == pytest.approx(shown.fit_zoom)
    assert shown.image_origin() == QPointF(0, 0)


def test_zoom_is_anchored_at_the_mouse_position(shown):
    for pos in ((100, 100), (500, 50), (30, 380), (320, 200)):
        before = shown.map_to_image(QPointF(*pos))
        wheel(shown, pos, 1)
        after = shown.map_to_image(QPointF(*pos))
        assert abs(after.x() - before.x()) <= 1 and abs(after.y() - before.y()) <= 1
    # Independent check from fit: (100, 100) at 200 % is image (50, 50); one notch
    # gives 250 %, so the origin moves to 100 - 50 * 2.5 = -25 on both axes.
    shown.reset_zoom()
    wheel(shown, (100, 100), 1)
    assert shown.image_origin().x() == pytest.approx(-25)
    assert shown.image_origin().y() == pytest.approx(-25)
    assert shown.map_to_image(QPointF(100, 100)) == QPointF(50, 50)


def test_anchor_is_kept_when_zooming_out_unless_clamping_requires_a_shift(shown):
    wheel(shown, (600, 390), 4)
    before = shown.map_to_image(QPointF(600, 390))
    wheel(shown, (600, 390), -1)
    after = shown.map_to_image(QPointF(600, 390))
    assert abs(after.x() - before.x()) <= 1 and abs(after.y() - before.y()) <= 1


def test_zoomed_image_never_leaves_a_gap_beyond_its_edges(shown):
    wheel(shown, (640, 400), 1)  # anchor at the image's bottom-right corner (at fit)
    origin = shown.image_origin()
    assert origin.x() == pytest.approx(640 - IMAGE_W * shown.zoom)
    assert origin.y() == pytest.approx(400 - IMAGE_H * shown.zoom)
    shown.reset_zoom()
    wheel(shown, (0, 0), 2)  # anchor at the top-left corner: origin stays (0, 0)
    assert shown.zoom == pytest.approx(3.125) and shown.image_origin() == QPointF(0, 0)
    # Zooming out at the widget's bottom-right would put the origin at +128: clamped to 0.
    wheel(shown, (640, 400), -1)
    assert shown.zoom == pytest.approx(2.5) and shown.image_origin() == QPointF(0, 0)


def test_left_drag_pans_and_is_clamped(shown):
    shown.zoom_at(QPointF(320, 200), 2.0)  # 400 %: image 1280 x 800, origin (-320, -200)
    assert shown.image_origin() == QPointF(-320, -200)
    drag(shown, (300, 200), (200, 150))
    assert shown.image_origin() == QPointF(-420, -250)
    drag(shown, (100, 100), (500, 300))  # (-20, -50): still within the image
    assert shown.image_origin() == QPointF(-20, -50)
    drag(shown, (100, 100), (600, 400))  # would move to (480, 250): clamped at 0
    assert shown.image_origin() == QPointF(0, 0)
    drag(shown, (600, 390), (10, 10))  # 590 px: still within the 640 px of slack
    assert shown.image_origin() == QPointF(-590, -380)
    drag(shown, (600, 390), (10, 10))  # now beyond the bottom-right edge: clamped
    assert shown.image_origin() == QPointF(640 - 1280, 400 - 800)


def test_pan_centres_an_axis_where_the_image_is_smaller_than_the_widget(qtbot, view, tmp_path):
    show(qtbot, view, make_frame(tmp_path, "wide", 640, 100))  # fit = 1.0
    assert view.fit_zoom == pytest.approx(1.0)
    view.zoom_at(QPointF(320, 200), 2.0)  # 1280 x 200: wider, but only half the height
    assert view.image_origin().y() == pytest.approx((400 - 200) / 2)
    drag(view, (300, 200), (250, 50))
    assert view.image_origin().y() == pytest.approx((400 - 200) / 2)
    assert view.image_origin().x() == pytest.approx(-320 - 50)


def test_no_pan_and_arrow_cursor_at_fit(shown):
    drag(shown, (300, 200), (200, 150))
    assert shown.at_fit and shown.image_origin() == QPointF(0, 0)
    assert shown.cursor().shape() == Qt.CursorShape.ArrowCursor
    wheel(shown, (320, 200), 1)
    assert shown.cursor().shape() == Qt.CursorShape.OpenHandCursor
    mouse(shown, QEvent.Type.MouseButtonPress, (320, 200), True)
    assert shown.cursor().shape() == Qt.CursorShape.ClosedHandCursor
    mouse(shown, QEvent.Type.MouseButtonRelease, (320, 200), False)
    assert shown.cursor().shape() == Qt.CursorShape.OpenHandCursor
    shown.reset_zoom()
    assert shown.cursor().shape() == Qt.CursorShape.ArrowCursor


def test_double_click_resets_to_fit(shown, qtbot):
    wheel(shown, (100, 100), 3)
    assert not shown.at_fit
    with qtbot.waitSignal(shown.zoom_changed) as blocker:
        mouse(shown, QEvent.Type.MouseButtonDblClick, (100, 100), True)
    assert blocker.args == [pytest.approx(2.0)]
    assert shown.at_fit and shown.image_origin() == QPointF(0, 0)


def test_reset_zoom_and_wheel_without_an_image_are_harmless(view):
    wheel(view, (10, 10), 1)
    assert view.at_fit and view.zoom == 1.0 and view.fit_zoom == 1.0
    view.zoom_at(QPointF(10, 10), 2.0)
    view.reset_zoom()
    assert view.zoom == 1.0


def test_zoom_and_pan_survive_same_size_frames_and_screens(qtbot, view, tmp_path):
    show(qtbot, view, make_frame(tmp_path, "a"))
    wheel(view, (100, 100), 2)
    drag(view, (300, 200), (250, 180))
    zoom, origin = view.zoom, view.image_origin()
    show(qtbot, view, make_frame(tmp_path, "b"))  # next frame during playback
    assert view.zoom == pytest.approx(zoom) and view.image_origin() == origin
    show(qtbot, view, make_frame(tmp_path, "c", screen_id="screen2"))  # same resolution
    assert view.zoom == pytest.approx(zoom) and view.image_origin() == origin


def test_different_image_size_resets_to_fit(qtbot, view, tmp_path):
    show(qtbot, view, make_frame(tmp_path, "a"))
    wheel(view, (100, 100), 2)
    assert not view.at_fit
    with qtbot.waitSignal(view.zoom_changed, timeout=10_000):
        show(qtbot, view, make_frame(tmp_path, "small", 160, 100, screen_id="screen2"))
    assert view.at_fit and view.fit_zoom == pytest.approx(4.0)
    wheel(view, (100, 100), 2)
    assert view.zoom == pytest.approx(4.0 * ZOOM_STEP**2)
    view.clear()  # another recording
    assert view.at_fit and view.zoom == 1.0
    show(qtbot, view, make_frame(tmp_path, "d"))
    assert view.at_fit and view.zoom == pytest.approx(2.0)


def test_drop_frame_keeps_the_zoom_for_the_same_size_and_refits_otherwise(qtbot, view, tmp_path):
    """Selecting another screen track (spec 4.5): the zoom survives the same image size."""
    show(qtbot, view, make_frame(tmp_path, "a"))
    wheel(view, (100, 100), 2)
    drag(view, (300, 200), (250, 180))
    zoom, origin = view.zoom, view.image_origin()
    view.drop_frame()
    assert view.shown_path is None and view.zoom == pytest.approx(zoom)
    show(qtbot, view, make_frame(tmp_path, "b", screen_id="screen2"))  # same size
    assert not view.at_fit
    assert view.zoom == pytest.approx(zoom) and view.image_origin() == origin
    view.drop_frame()
    with qtbot.waitSignal(view.zoom_changed, timeout=10_000):
        show(qtbot, view, make_frame(tmp_path, "c", 160, 100, screen_id="screen3"))
    assert view.at_fit and view.fit_zoom == pytest.approx(4.0)


def test_resize_keeps_the_zoom_and_reclamps_or_refits(shown):
    shown.zoom_at(QPointF(640, 400), 2.0)  # 400 %, anchored bottom-right: origin (-640, -400)
    assert shown.image_origin() == QPointF(-640, -400)
    shown.resize(1000, 600)
    assert shown.zoom == pytest.approx(4.0)
    assert shown.image_origin() == QPointF(1000 - 1280, 600 - 800)  # re-clamped
    shown.resize(1400, 900)  # fit would be 4.375 > 4.0: back to fit
    assert shown.at_fit and shown.zoom == pytest.approx(shown.fit_zoom)
    shown.resize(WIDGET_W, WIDGET_H)
    assert shown.at_fit and shown.fit_zoom == pytest.approx(2.0)


def _is_white(image: QImage, x: float, y: float) -> bool:
    colour = image.pixelColor(round(x), round(y))
    return colour.red() > 200 and colour.green() > 200 and colour.blue() > 200


def test_cursor_arrow_follows_the_zoom_and_keeps_its_size(qtbot, view, tmp_path):
    cursor = CursorPosition("screen1", 100, 60)
    show(qtbot, view, make_frame(tmp_path, "a"), cursor)
    assert view.at_fit
    grabbed = view.grab().toImage()
    assert _is_white(grabbed, 100 * 2 + 3, 60 * 2 + 8)  # inside the arrow at 200 %
    assert not _is_white(grabbed, 100 * 2 + 40, 60 * 2 + 40)

    view.zoom_at(QPointF(200, 120), 2.0)  # anchored at the cursor: it stays at (200, 120)
    at = view.map_from_image(QPointF(100, 60))
    assert at == QPointF(200, 120)
    grabbed = view.grab().toImage()
    assert _is_white(grabbed, 203, 128)
    assert not _is_white(grabbed, 240, 160)  # arrow did not grow with the zoom
    assert grabbed.pixelColor(400, 300) == FILL  # the image itself is drawn around it

    drag(view, (300, 200), (200, 100))  # pan: the arrow moves with the image
    at = view.map_from_image(QPointF(100, 60))
    assert at == QPointF(100, 20)
    grabbed = view.grab().toImage()
    assert _is_white(grabbed, 103, 28)
    assert not _is_white(grabbed, 203, 128)

    view.set_draw_cursor(False)
    grabbed = view.grab().toImage()
    assert not _is_white(grabbed, 103, 28)


def test_zoomed_paint_covers_only_the_image_area(qtbot, view, tmp_path):
    show(qtbot, view, make_frame(tmp_path, "wide", 640, 100))  # 1280 x 200 at 200 %
    view.zoom_at(QPointF(320, 200), 2.0)
    grabbed = view.grab().toImage()
    assert grabbed.pixelColor(320, 200) == FILL
    assert grabbed.pixelColor(320, 20) == QColor("#101010")  # letterbox above
    assert grabbed.pixelColor(320, 380) == QColor("#101010")  # and below


def test_panel_keeps_the_zoom_view_and_transport(qtbot):
    panel = PreviewPanel()
    qtbot.addWidget(panel)
    assert isinstance(panel.view, PreviewWidget)
    assert panel.view.zoom == 1.0
    assert panel.play_button.text() == "Play"
