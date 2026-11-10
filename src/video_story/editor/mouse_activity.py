"""Which mouse buttons and wheels were in use at any moment of a recording (no Qt).

The editor draws ``assets/mouse.svg`` (top view, every control has its own id) next to
the transport (:mod:`video_story.editor.mouse_view`). As the playhead moves, a pressed
button is filled red and a turned wheel yellow together with the arrow of the direction
it turned; the outline stays black (white in the dark theme) and everything else keeps
the body colour (white, dark in the dark theme):

=================  ===========================================================
``left``           ``button-1`` red
``right``          ``button-2`` red
``middle``         ``vertical-scroll-module`` red; the wheel and its arrows on it
                   stay in the body colour
``x2`` (forward)   ``button-4`` red (the front side button)
``x1`` (back)      ``button-5`` red (the rear side button)
wheel up / down    ``button-3`` (the wheel) and ``scroll-up`` / ``scroll-down`` yellow
wheel right / left ``horizontal-scroll-wheel`` and ``scroll-right`` / ``scroll-left``
                   yellow
=================  ===========================================================

A button counts as pressed from its ``down`` to its ``up`` in ``input/mouse.jsonl``, a
wheel as turning from the first to the last event of a merged wheel record; both stay
lit at least :data:`MIN_LIT_MS`, so that a quick click or a single notch is still seen
while playing or stepping. Events a program injected are left out (no finger pressed
anything); clicks into Video Story and onto the drawing layer are shown like any other,
since the mouse was really pressed.
"""

from __future__ import annotations

import bisect
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from video_story.core.input_events import MouseLog, read_mouse_log

SVG_PATH = Path(__file__).with_name("assets") / "mouse.svg"
SVG_NS = "http://www.w3.org/2000/svg"
SVG_SIZE = (756.0, 740.0)  # the viewBox of the picture

MIN_LIT_MS = 200  # a click or one notch stays visible at least this long

PRESSED_FILL = "#e53935"  # red: a pressed button
WHEEL_FILL = "#ffe600"  # bright yellow: a turning wheel and its arrow
LIGHT = {"stroke": "#000000", "fill": "#ffffff"}
DARK = {"stroke": "#ffffff", "fill": "#1f1f1f"}

BUTTON_IDS = {
    "left": "button-1",
    "right": "button-2",
    "middle": "vertical-scroll-module",
    "x2": "button-4",  # forward: the side button nearer the front of the mouse
    "x1": "button-5",  # back
}
WHEEL_IDS = {
    ("v", True): ("button-3", "scroll-up"),
    ("v", False): ("button-3", "scroll-down"),
    ("h", True): ("horizontal-scroll-wheel", "scroll-right"),
    ("h", False): ("horizontal-scroll-wheel", "scroll-left"),
}
RED_IDS = frozenset(BUTTON_IDS.values())
YELLOW_IDS = frozenset(i for ids in WHEEL_IDS.values() for i in ids)


@dataclass(frozen=True)
class LitSpan:
    """Controls (SVG ids) lit from ``start_ms`` up to, not including, ``end_ms``."""

    start_ms: int
    end_ms: int
    ids: tuple[str, ...]


class MouseActivity:
    """The lit controls of the mouse over the whole recording, looked up by time."""

    def __init__(self, spans: Iterable[LitSpan]) -> None:
        self.spans = sorted(spans, key=lambda s: (s.start_ms, s.end_ms))
        events: list[tuple[int, int, str]] = []
        for span in self.spans:
            if span.end_ms <= span.start_ms:
                continue
            for control in span.ids:
                events.append((span.start_ms, 1, control))
                events.append((span.end_ms, -1, control))
        events.sort(key=lambda e: (e[0], e[1]))
        self._times: list[int] = []
        self._states: list[frozenset[str]] = []
        counts: dict[str, int] = {}
        for i, (time_ms, delta, control) in enumerate(events):
            counts[control] = counts.get(control, 0) + delta
            if i + 1 < len(events) and events[i + 1][0] == time_ms:
                continue  # one state per moment, after all of its changes
            state = frozenset(c for c, n in counts.items() if n > 0)
            if self._states and self._states[-1] == state:
                continue
            self._times.append(time_ms)
            self._states.append(state)

    @classmethod
    def from_log(cls, log: MouseLog, min_lit_ms: int = MIN_LIT_MS) -> MouseActivity:
        spans: list[LitSpan] = []
        pressed: dict[str, int] = {}  # button -> time of its down

        def release(button: str, until: int) -> None:
            start = pressed.pop(button)
            spans.append(LitSpan(start, max(until, start + min_lit_ms), (BUTTON_IDS[button],)))

        for rec in sorted(log.buttons, key=lambda r: (r.t, r.seq)):
            if rec.button not in BUTTON_IDS or rec.injected:
                continue
            if rec.kind == "down":
                if rec.button in pressed:  # its up was lost: the earlier press ends here
                    release(rec.button, rec.t)
                pressed[rec.button] = rec.t
            elif rec.button in pressed:
                release(rec.button, rec.t)
        for button in list(pressed):  # still held when the recording ended
            release(button, pressed[button])
        for wheel in log.wheels:
            if wheel.injected or wheel.delta == 0:
                continue
            ids = WHEEL_IDS.get((wheel.axis, wheel.delta > 0))
            if ids is not None:
                spans.append(LitSpan(wheel.t, max(wheel.end, wheel.t) + min_lit_ms, ids))
        return cls(spans)

    @classmethod
    def load(cls, path: Path) -> MouseActivity | None:
        """``input/mouse.jsonl`` of a recording; ``None`` when the mouse was not recorded."""
        if not path.is_file():
            return None
        return cls.from_log(read_mouse_log(path))

    def lit_at(self, time_ms: int) -> frozenset[str]:
        """SVG ids of the controls in use at ``time_ms``."""
        i = bisect.bisect_right(self._times, time_ms) - 1
        return self._states[i] if i >= 0 else frozenset()


@lru_cache(maxsize=1)
def _template() -> bytes:
    return SVG_PATH.read_bytes()


def mouse_svg(lit: Iterable[str], dark: bool, stroke_width: float = 4.0) -> bytes:
    """The mouse picture with the ``lit`` controls filled, in the colours of the theme."""
    lit = frozenset(lit)
    colors = DARK if dark else LIGHT
    ET.register_namespace("", SVG_NS)
    root = ET.fromstring(_template())
    for child in list(root):
        if child.get("id") == "background":
            root.remove(child)  # the widget's own background shows through
    for element in root.iter():
        element_id = element.get("id")
        if element_id == "outline":
            element.set("stroke", colors["stroke"])
            element.set("stroke-width", f"{stroke_width:.2f}")
        elif element.get("fill") is not None:
            if element_id in lit and element_id in RED_IDS:
                element.set("fill", PRESSED_FILL)
            elif element_id in lit and element_id in YELLOW_IDS:
                element.set("fill", WHEEL_FILL)
            else:
                element.set("fill", colors["fill"])
    return ET.tostring(root, encoding="utf-8")


__all__ = [
    "BUTTON_IDS",
    "MIN_LIT_MS",
    "PRESSED_FILL",
    "SVG_SIZE",
    "WHEEL_FILL",
    "WHEEL_IDS",
    "LitSpan",
    "MouseActivity",
    "mouse_svg",
]
