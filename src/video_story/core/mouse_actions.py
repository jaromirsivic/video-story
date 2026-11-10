"""What the user did with the mouse, derived from the raw events of ``input/mouse.jsonl``.

Pure and deterministic: the same file and the same system parameters (stored in the
manifest) always give the same actions, on every operating system.

Rules (see ``implementation-plan-mouse.md``, chapter 5):

* **click** - ``down`` and ``up`` of one button, the pointer stayed within ``drag_px``;
  held longer than :data:`HOLD_MS` it carries ``held_ms``.
* **double_click** / **multi_click** - the next press of the same button within
  ``double_click_ms`` of the previous press and within the double-click rectangle
  (``double_click_px``, centred on the previous press), as Windows decides it. A press
  of another button in between breaks the series.
* **drag** - the pointer left ``drag_px`` while the button was down; the path is
  simplified (Douglas-Peucker, :data:`PATH_TOLERANCE_PX`).
* **scroll** - wheel lines of one axis and direction with gaps up to
  :data:`SCROLL_GAP_MS`; with Ctrl on the vertical axis it is a **zoom**.
* **gesture** - a two-finger touchpad gesture; a scroll or zoom from the wheel that
  overlaps it (the system turned the gesture into wheel events) merges into it.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from typing import Literal

from .input_events import (
    ButtonRecord,
    GestureRecord,
    MouseLog,
    MouseSystemParams,
    MoveRecord,
    WheelRecord,
)

HOLD_MS = 800
SCROLL_GAP_MS = 400
PATH_TOLERANCE_PX = 3
NOTCH = 120
GESTURE_OVERLAP_MS = 150  # a wheel action this close to a gesture belongs to it

ActionKind = Literal["click", "double_click", "multi_click", "drag", "scroll", "zoom", "gesture"]


@dataclass(frozen=True)
class Point:
    screen: str | None
    x: int | None
    y: int | None
    gx: int
    gy: int


@dataclass(frozen=True)
class MouseAction:
    kind: ActionKind
    t: int
    until: int
    at: Point  # where it started
    button: str | None = None
    clicks: int = 1
    held_ms: int | None = None  # the (last) press lasted longer than HOLD_MS
    end: Point | None = None  # drag: where the button went up
    path: tuple[tuple[int, int], ...] = ()  # drag: simplified path, virtual desktop px
    length_px: int = 0  # drag: length of the path
    axis: str | None = None  # scroll / zoom: "v" or "h"
    direction: str | None = None  # up/down/left/right, zoom/pinch: in/out
    notches: float = 0.0  # scroll / zoom: sum of |delta| / 120
    gesture: str | None = None  # gesture: "pinch" or "scroll"
    scale: float | None = None  # pinch
    touchpad: bool = False
    mods: tuple[str, ...] = ()
    target: str = "app"
    win: int | None = None
    injected: bool = False
    unfinished: bool = False  # the recording ended (or the source failed) first
    ref: int | None = None  # seq of the event the element under the mouse belongs to
    end_ref: int | None = None  # seq of the last event (drop target, selection)

    @property
    def duration_ms(self) -> int:
        return max(0, self.until - self.t)


def _point(rec: ButtonRecord | MoveRecord | WheelRecord | GestureRecord) -> Point:
    return Point(rec.screen, rec.x, rec.y, rec.gx, rec.gy)


# Path simplification --------------------------------------------------------------


def simplify_path(
    points: list[tuple[int, int]], tolerance: float = PATH_TOLERANCE_PX
) -> list[tuple[int, int]]:
    """Douglas-Peucker: keeps the first and last point and every point farther than
    ``tolerance`` from the simplified line."""
    if len(points) <= 2:
        return list(points)
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        lo, hi = stack.pop()
        if hi - lo < 2:
            continue
        (ax, ay), (bx, by) = points[lo], points[hi]
        dx, dy = bx - ax, by - ay
        norm = math.hypot(dx, dy)
        best, best_i = -1.0, -1
        for i in range(lo + 1, hi):
            px, py = points[i]
            if norm == 0:
                dist = math.hypot(px - ax, py - ay)
            else:
                dist = abs(dy * (px - ax) - dx * (py - ay)) / norm
            if dist > best:
                best, best_i = dist, i
        if best > tolerance:
            keep[best_i] = True
            stack.append((lo, best_i))
            stack.append((best_i, hi))
    return [p for p, k in zip(points, keep, strict=True) if k]


def path_length(points: Iterable[tuple[int, int]]) -> float:
    total = 0.0
    prev = None
    for p in points:
        if prev is not None:
            total += math.hypot(p[0] - prev[0], p[1] - prev[1])
        prev = p
    return total


# Buttons -----------------------------------------------------------------------


@dataclass
class _Press:
    down: ButtonRecord
    path: list[tuple[int, int]] = field(default_factory=list)
    dragging: bool = False
    last: MoveRecord | None = None


@dataclass
class _Series:
    """Clicks of one button that form a double / multi click."""

    first: ButtonRecord
    last_down: ButtonRecord
    last_up: ButtonRecord
    clicks: int
    held_ms: int | None
    injected: bool


def _beyond(a: tuple[int, int], b: tuple[int, int], limit: tuple[int, int]) -> bool:
    return abs(a[0] - b[0]) > limit[0] or abs(a[1] - b[1]) > limit[1]


def _in_double_click_rect(prev: ButtonRecord, down: ButtonRecord, px: tuple[int, int]) -> bool:
    return abs(down.gx - prev.gx) <= px[0] / 2 and abs(down.gy - prev.gy) <= px[1] / 2


def _click_action(series: _Series) -> MouseAction:
    kind: ActionKind = "click"
    if series.clicks == 2:
        kind = "double_click"
    elif series.clicks > 2:
        kind = "multi_click"
    first = series.first
    return MouseAction(
        kind=kind,
        t=first.t,
        until=series.last_up.t,
        at=_point(first),
        button=first.button,
        clicks=series.clicks,
        held_ms=series.held_ms,
        mods=first.mods,
        target=first.target,
        win=first.win,
        injected=series.injected,
        ref=first.seq,
        end_ref=series.last_up.seq,
    )


def _drag_action(press: _Press, up: ButtonRecord | None, end: Point, until: int) -> MouseAction:
    down = press.down
    simplified = simplify_path(press.path)
    return MouseAction(
        kind="drag",
        t=down.t,
        until=until,
        at=_point(down),
        button=down.button,
        end=end,
        path=tuple(simplified),
        length_px=round(path_length(press.path)),
        mods=down.mods,
        target=down.target,
        win=down.win,
        injected=down.injected or bool(up and up.injected),
        unfinished=up is None,
        ref=down.seq,
        end_ref=up.seq if up is not None else None,
    )


def _button_actions(
    buttons: list[ButtonRecord],
    moves: list[MoveRecord],
    params: MouseSystemParams,
    end_ms: int | None,
) -> list[MouseAction]:
    events: list[ButtonRecord | MoveRecord] = sorted([*buttons, *moves], key=lambda r: r.seq)
    pressed: dict[str, _Press] = {}
    series: dict[str, _Series] = {}
    out: list[MouseAction] = []

    def close_series(button: str) -> None:
        s = series.pop(button, None)
        if s is not None:
            out.append(_click_action(s))

    for ev in events:
        if isinstance(ev, MoveRecord):
            for press in pressed.values():
                press.path.append((ev.gx, ev.gy))
                press.last = ev
                if not press.dragging and _beyond(
                    (ev.gx, ev.gy), (press.down.gx, press.down.gy), params.drag_px
                ):
                    press.dragging = True
            continue
        if ev.kind == "down":
            for other in [b for b in series if b != ev.button]:
                close_series(other)  # another button breaks a double click
            if ev.button in pressed:  # a lost "up": treat the old press as finished
                old = pressed.pop(ev.button)
                if old.dragging and old.last is not None:
                    out.append(_drag_action(old, None, _point(old.last), old.last.t))
            pressed[ev.button] = _Press(down=ev, path=[(ev.gx, ev.gy)])
            continue
        press = pressed.pop(ev.button, None)
        if press is None:
            continue  # pressed before Record (or before the source started)
        press.path.append((ev.gx, ev.gy))
        down = press.down
        if press.dragging or _beyond((ev.gx, ev.gy), (down.gx, down.gy), params.drag_px):
            close_series(ev.button)
            out.append(_drag_action(press, ev, _point(ev), ev.t))
            continue
        held = ev.t - down.t
        held_ms = held if held > HOLD_MS else None
        s = series.get(ev.button)
        if (
            s is not None
            and s.held_ms is None
            and down.t - s.last_down.t <= params.double_click_ms
            and _in_double_click_rect(s.last_down, down, params.double_click_px)
        ):
            s.clicks += 1
            s.last_down, s.last_up = down, ev
            s.held_ms = held_ms
            s.injected = s.injected or down.injected or ev.injected
        else:
            close_series(ev.button)
            series[ev.button] = _Series(
                first=down,
                last_down=down,
                last_up=ev,
                clicks=1,
                held_ms=held_ms,
                injected=down.injected or ev.injected,
            )
    for button in list(series):
        close_series(button)
    for press in pressed.values():  # still down when the recording ended
        down = press.down
        last_t = press.last.t if press.last is not None else down.t
        until = max(last_t, end_ms if end_ms is not None else last_t)
        if press.dragging and press.last is not None:
            out.append(_drag_action(press, None, _point(press.last), until))
        else:
            held = until - down.t
            out.append(
                MouseAction(
                    kind="click",
                    t=down.t,
                    until=until,
                    at=_point(down),
                    button=down.button,
                    held_ms=held if held > HOLD_MS else None,
                    mods=down.mods,
                    target=down.target,
                    win=down.win,
                    injected=down.injected,
                    unfinished=True,
                    ref=down.seq,
                )
            )
    return out


# Wheel -------------------------------------------------------------------------


@dataclass
class _Scroll:
    first: WheelRecord
    last: WheelRecord
    delta: int
    abs_delta: int
    injected: bool


def _wheel_key(rec: WheelRecord) -> tuple[str, bool]:
    return (rec.axis, rec.axis == "v" and "ctrl" in rec.mods)


def _scroll_action(s: _Scroll) -> MouseAction:
    axis, zoom = _wheel_key(s.first)
    if zoom:
        direction = "in" if s.delta > 0 else "out"
    elif axis == "v":
        direction = "up" if s.delta > 0 else "down"
    else:
        direction = "right" if s.delta > 0 else "left"
    return MouseAction(
        kind="zoom" if zoom else "scroll",
        t=s.first.t,
        until=s.last.end,
        at=_point(s.first),
        axis=axis,
        direction=direction,
        notches=round(s.abs_delta / NOTCH, 1),
        mods=s.first.mods,
        target=s.first.target,
        win=s.first.win,
        injected=s.injected,
        ref=s.first.seq,
        end_ref=s.last.seq,
    )


def _wheel_actions(wheels: list[WheelRecord]) -> list[MouseAction]:
    open_: dict[tuple[str, bool], _Scroll] = {}
    out: list[MouseAction] = []
    for rec in sorted(wheels, key=lambda r: (r.t, r.seq)):
        if rec.delta == 0:
            continue
        key = _wheel_key(rec)
        cur = open_.get(key)
        if (
            cur is not None
            and (cur.delta > 0) == (rec.delta > 0)
            and rec.t - cur.last.end <= SCROLL_GAP_MS
            and cur.first.target == rec.target
        ):
            cur.last = rec
            cur.delta += rec.delta
            cur.abs_delta += abs(rec.delta)
            cur.injected = cur.injected or rec.injected
            continue
        if cur is not None:
            out.append(_scroll_action(cur))
        open_[key] = _Scroll(rec, rec, rec.delta, abs(rec.delta), rec.injected)
    out.extend(_scroll_action(s) for s in open_.values())
    return out


# Gestures ----------------------------------------------------------------------


def _gesture_action(g: GestureRecord) -> MouseAction:
    return MouseAction(
        kind="gesture",
        t=g.t,
        until=g.end,
        at=_point(g),
        gesture=g.kind,
        direction=g.direction,
        scale=g.scale,
        touchpad=True,
        ref=g.seq,
        end_ref=g.seq,
    )


def _merge_gestures(
    wheel_actions: list[MouseAction], gestures: list[GestureRecord]
) -> tuple[list[MouseAction], list[MouseAction]]:
    """Gesture actions, and the wheel actions no gesture absorbed."""
    remaining = list(wheel_actions)
    result: list[MouseAction] = []
    for g in sorted(gestures, key=lambda r: (r.t, r.seq)):
        action = _gesture_action(g)
        wanted = "zoom" if g.kind == "pinch" else "scroll"
        lo, hi = g.t - GESTURE_OVERLAP_MS, g.end + GESTURE_OVERLAP_MS
        absorbed = [a for a in remaining if a.kind == wanted and a.t <= hi and a.until >= lo]
        if absorbed:
            taken = {id(a) for a in absorbed}
            remaining = [a for a in remaining if id(a) not in taken]
            action = replace(
                action,
                notches=round(sum(a.notches for a in absorbed), 1),
                axis=absorbed[0].axis,
                target=absorbed[0].target,
                win=absorbed[0].win,
            )
        result.append(action)
    return result, remaining


# Entry point --------------------------------------------------------------------


def derive_actions(
    log: MouseLog, params: MouseSystemParams | None = None, end_ms: int | None = None
) -> list[MouseAction]:
    """All actions of the recording, ordered by start time.

    ``end_ms`` (the recorded duration) closes presses that were never released."""
    params = params or MouseSystemParams()
    buttons = _button_actions(log.buttons, log.moves, params, end_ms)
    gestures, wheels = _merge_gestures(_wheel_actions(log.wheels), log.gestures)
    actions = [*buttons, *wheels, *gestures]
    actions.sort(key=lambda a: (a.t, a.ref or 0))
    return actions


__all__ = [
    "GESTURE_OVERLAP_MS",
    "HOLD_MS",
    "PATH_TOLERANCE_PX",
    "SCROLL_GAP_MS",
    "MouseAction",
    "Point",
    "derive_actions",
    "path_length",
    "simplify_path",
]
