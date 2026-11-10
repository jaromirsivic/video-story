"""The mouse next to the transport: lit buttons and wheels at the playhead."""

from __future__ import annotations

import json
import os
import xml.etree.ElementTree as ET

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QColor  # noqa: E402

from video_story.core.input_events import ButtonRecord, MouseLog, WheelRecord  # noqa: E402
from video_story.editor.mouse_activity import (  # noqa: E402
    MIN_LIT_MS,
    PRESSED_FILL,
    WHEEL_FILL,
    LitSpan,
    MouseActivity,
    mouse_svg,
)
from video_story.editor.mouse_view import MOUSE_WIDGET_SIZE, MouseWidget  # noqa: E402
from video_story.editor.preview import PreviewPanel  # noqa: E402

NS = {"s": "http://www.w3.org/2000/svg"}


def press(t: int, button: str, kind: str = "down", **kw) -> ButtonRecord:
    return ButtonRecord(seq=t, t=t, kind=kind, button=button, **kw)


def log_of(*records) -> MouseLog:
    log = MouseLog()
    for record in records:
        log.add(record)
    return log


def fills(svg: bytes) -> dict[str, str]:
    root = ET.fromstring(svg)
    return {e.get("id"): e.get("fill") for e in root.iter() if e.get("fill") is not None}


def test_a_held_button_is_lit_from_down_to_up():
    activity = MouseActivity.from_log(log_of(press(1000, "left"), press(2500, "left", "up")))
    assert activity.lit_at(999) == frozenset()
    assert activity.lit_at(1000) == {"button-1"}
    assert activity.lit_at(2499) == {"button-1"}
    assert activity.lit_at(2500) == frozenset()


def test_a_quick_click_stays_lit_the_minimum_time():
    activity = MouseActivity.from_log(log_of(press(1000, "right"), press(1050, "right", "up")))
    assert activity.lit_at(1000 + MIN_LIT_MS - 1) == {"button-2"}
    assert activity.lit_at(1000 + MIN_LIT_MS) == frozenset()


def test_each_button_has_its_own_control():
    activity = MouseActivity.from_log(
        log_of(
            press(0, "middle"),
            press(0, "x1"),
            press(0, "x2"),
            press(500, "middle", "up"),
            press(500, "x1", "up"),
            press(500, "x2", "up"),
            press(0, "b6"),  # Windows knows five buttons; others are not drawn
        )
    )
    assert activity.lit_at(100) == {"vertical-scroll-module", "button-4", "button-5"}


def test_wheels_light_the_wheel_and_the_arrow_of_their_direction():
    log = log_of(
        WheelRecord(seq=1, t=1000, axis="v", delta=240, until=1100),
        WheelRecord(seq=2, t=3000, axis="v", delta=-120),
        WheelRecord(seq=3, t=5000, axis="h", delta=120),
        WheelRecord(seq=4, t=7000, axis="h", delta=-360),
    )
    activity = MouseActivity.from_log(log)
    assert activity.lit_at(1050) == {"button-3", "scroll-up"}
    assert activity.lit_at(1100 + MIN_LIT_MS - 1) == {"button-3", "scroll-up"}
    assert activity.lit_at(1100 + MIN_LIT_MS) == frozenset()
    assert activity.lit_at(3000) == {"button-3", "scroll-down"}
    assert activity.lit_at(5000) == {"horizontal-scroll-wheel", "scroll-right"}
    assert activity.lit_at(7000) == {"horizontal-scroll-wheel", "scroll-left"}


def test_injected_events_and_lost_ups_are_handled():
    activity = MouseActivity.from_log(
        log_of(
            press(0, "left", injected=True),
            press(10, "left", "up", injected=True),
            press(1000, "right"),  # its up was lost
            press(3000, "right"),
            press(3400, "right", "up"),
            press(9000, "left"),  # held when the recording ended
        )
    )
    assert activity.lit_at(5) == frozenset()
    assert activity.lit_at(2000) == {"button-2"}  # until the next down of the button
    assert activity.lit_at(3200) == {"button-2"}
    assert activity.lit_at(3400) == frozenset()
    assert activity.lit_at(9000) == {"button-1"}


def test_overlapping_spans_of_one_control_count():
    activity = MouseActivity(
        [LitSpan(0, 500, ("button-3", "scroll-up")), LitSpan(300, 900, ("button-3",))]
    )
    assert activity.lit_at(400) == {"button-3", "scroll-up"}
    assert activity.lit_at(600) == {"button-3"}
    assert activity.lit_at(900) == frozenset()


def test_load_reads_the_recordings_mouse_log(tmp_path):
    assert MouseActivity.load(tmp_path / "missing.jsonl") is None
    path = tmp_path / "mouse.jsonl"
    lines = [
        {"seq": 1, "t": 100, "type": "down", "button": "left", "target": "app"},
        {"seq": 2, "t": 600, "type": "up", "button": "left", "target": "app"},
        {"seq": 3, "t": 900, "type": "wheel", "axis": "v", "delta": -120, "target": "app"},
    ]
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")
    activity = MouseActivity.load(path)
    assert activity is not None
    assert activity.lit_at(300) == {"button-1"}
    assert activity.lit_at(950) == {"button-3", "scroll-down"}


def test_svg_fills_follow_the_lit_controls_and_the_theme():
    light = fills(mouse_svg({"vertical-scroll-module", "button-3", "scroll-up"}, dark=False))
    assert "background" not in light  # the widget's background shows through
    assert light["vertical-scroll-module"] == PRESSED_FILL
    assert light["button-3"] == WHEEL_FILL and light["scroll-up"] == WHEEL_FILL
    assert light["scroll-down"] == "#ffffff" and light["mouse-body"] == "#ffffff"
    dark_svg = mouse_svg({"button-1"}, dark=True, stroke_width=9)
    dark = fills(dark_svg)
    assert dark["button-1"] == PRESSED_FILL
    assert dark["mouse-body"] == "#1f1f1f"
    outline = ET.fromstring(dark_svg).find(".//s:g[@id='outline']", NS)
    assert outline.get("stroke") == "#ffffff" and float(outline.get("stroke-width")) == 9


def test_widget_draws_the_lit_button_red(qtbot):
    widget = MouseWidget()
    qtbot.addWidget(widget)
    widget.set_dark(False)
    widget.set_lit(frozenset({"button-1"}))
    assert widget.lit == frozenset()  # no mouse data: nothing lights up
    widget.set_recorded(True)
    widget.set_lit(frozenset({"button-1"}))
    image = widget.grab().toImage()
    rect = widget.target_rect()
    # A point inside the left button (x 250..400, y 150..430 of the 8,50 756x740 view).
    x = rect.left() + (320 - 8) / 756 * rect.width()
    y = rect.top() + (300 - 50) / 740 * rect.height()
    ratio = image.devicePixelRatio()
    color = QColor(image.pixel(round(x * ratio), round(y * ratio)))
    assert color.red() > 200 and color.green() < 100
    widget.set_recorded(False)
    assert widget.lit == frozenset()


def test_panel_puts_the_mouse_left_of_the_transport(qtbot):
    panel = PreviewPanel()
    qtbot.addWidget(panel)
    panel.resize(1000, 600)
    panel.show()
    qtbot.waitExposed(panel)
    mouse = panel.mouse_view.geometry()
    assert mouse.size() == MOUSE_WIDGET_SIZE
    assert mouse.left() <= 8
    assert mouse.top() <= panel.in_button.geometry().top() + 2
    assert mouse.right() < panel.subtitle_label.geometry().left()
    assert mouse.top() > panel.view.geometry().bottom()
    panel.set_dark(False)
    assert not panel.mouse_view.dark
