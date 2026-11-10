"""Two-finger touchpad gestures (pinch, scroll) from the contacts a touchpad reports.

A platform's touchpad source hands over :class:`ContactFrame` objects (all fingers of
one scan, in touchpad units); :class:`GestureTracker` keeps one touch in memory and,
once the fingers lift, turns it into :class:`TouchGesture` values - only those go into
``input/mouse.jsonl`` (a touchpad sends ~125 frames per second).

Rule (measured in M0 on a Precision Touchpad, ``implementation-plan-mouse.md`` ch. 7):

* only the frames with exactly two fingers count; one finger is the pointer. A touch
  that had three or more fingers in more than :data:`MAX_EXTRA_FINGER_FRAMES` frames
  and more than :data:`MAX_EXTRA_FINGER_SHARE` of its frames is a system gesture and is
  ignored; a few such frames are a thumb or the palm brushing the pad during a pinch;
* ``spread`` = largest minus smallest distance of the two fingers, ``travel`` = length
  of the path of the point between them; both relative to the touchpad width;
* **pinch** when ``travel <= PINCH_MAX_TRAVEL * spread``; **scroll** when
  ``travel >= SCROLL_MIN_TRAVEL * spread``; anything in between, and touches where both
  stay below :data:`MIN_MOVE`, is dropped. Measured: scrolls had travel/spread >= 4.3,
  pinches <= 1.67 (fingers drift while pinching), so the gap between 2 and 3 is safe.
  The distance ratio alone does not work: fingers often spread while scrolling.

Directions are those of the fingers (touchpad Y grows downwards): ``up`` means the
fingers moved away from the user. Whether that scrolls the page up or down depends on
the user's scrolling setting, so the timeline says what the fingers did.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

MIN_MOVE = 0.05  # share of the touchpad size; below it a touch is a rest, not a gesture
MIN_FRAMES = 5
IDLE_MS = 150  # no frame for this long: the fingers are gone
PINCH_MIN_SCALE = 1.15  # a pinch must change the distance at least this much
PINCH_MAX_TRAVEL = 2.0  # pinch: the centre moved at most this many times the spread
SCROLL_MIN_TRAVEL = 3.0  # scroll: the centre moved at least this many times the spread
MAX_EXTRA_FINGER_FRAMES = 3  # frames with 3+ fingers a two-finger touch may have...
MAX_EXTRA_FINGER_SHARE = 0.2  # ...or this share of its frames, whichever is more


@dataclass(frozen=True)
class Contact:
    id: int
    x: int
    y: int


@dataclass(frozen=True)
class ContactFrame:
    """All fingers touching the pad in one scan (lifted fingers are not included)."""

    t_ms: int
    contacts: tuple[Contact, ...]
    device: int = 0  # which touchpad (a machine may have more than one)


@dataclass(frozen=True)
class TouchpadGeometry:
    """Logical size of the touchpad (``LogicalMax`` of X and Y)."""

    width: int
    height: int


@dataclass(frozen=True)
class TouchGesture:
    kind: Literal["pinch", "scroll"]
    t_ms: int
    until_ms: int
    direction: str  # pinch: in / out; scroll: up / down / left / right (fingers)
    scale: float | None = None  # pinch: end distance / start distance
    distance: float | None = None  # scroll: share of the touchpad size the fingers moved
    frames: int = 0


@dataclass(frozen=True)
class _Sample:
    t: int
    dist: float
    cx: float
    cy: float


def touch_measures(samples: list[_Sample], pad: TouchpadGeometry) -> tuple[float, float]:
    """``(spread, travel)`` of a two-finger touch, as shares of the touchpad size."""
    if not samples or pad.width <= 0 or pad.height <= 0:
        return 0.0, 0.0
    dists = [s.dist for s in samples]
    spread = (max(dists) - min(dists)) / pad.width
    travel = sum(
        math.hypot((b.cx - a.cx) / pad.width, (b.cy - a.cy) / pad.height)
        for a, b in zip(samples, samples[1:], strict=False)
    )
    return spread, travel


def decide(spread: float, travel: float) -> Literal["pinch", "scroll"] | None:
    """What a two-finger touch with this spread and travel is (``None``: nothing)."""
    if spread < MIN_MOVE and travel < MIN_MOVE:
        return None
    if travel <= PINCH_MAX_TRAVEL * spread:
        return "pinch"
    if travel >= SCROLL_MIN_TRAVEL * spread:
        return "scroll"
    return None


def classify_touch(samples: list[_Sample], pad: TouchpadGeometry) -> list[TouchGesture]:
    """Gestures of one two-finger touch (empty when it was none)."""
    if len(samples) < MIN_FRAMES or pad.width <= 0 or pad.height <= 0:
        return []
    kind = decide(*touch_measures(samples, pad))
    if kind == "pinch":
        return _pinch(samples)
    if kind == "scroll":
        return _scrolls(samples, pad)
    return []


def _pinch(samples: list[_Sample]) -> list[TouchGesture]:
    start = samples[0].dist
    if start <= 0:
        return []
    scale = samples[-1].dist / start
    if abs(math.log(scale)) < math.log(PINCH_MIN_SCALE):
        # out and back in (or the other way): report the larger of the two changes
        hi, lo = max(s.dist for s in samples) / start, min(s.dist for s in samples) / start
        scale = hi if abs(math.log(hi)) >= abs(math.log(max(lo, 1e-9))) else lo
        if abs(math.log(max(scale, 1e-9))) < math.log(PINCH_MIN_SCALE):
            return []
    return [
        TouchGesture(
            kind="pinch",
            t_ms=samples[0].t,
            until_ms=samples[-1].t,
            direction="out" if scale > 1 else "in",
            scale=round(scale, 2),
            frames=len(samples),
        )
    ]


def _scrolls(samples: list[_Sample], pad: TouchpadGeometry) -> list[TouchGesture]:
    """One gesture per direction: fingers going down and back up give two."""
    dx = sum(abs(b.cx - a.cx) for a, b in zip(samples, samples[1:], strict=False)) / pad.width
    dy = sum(abs(b.cy - a.cy) for a, b in zip(samples, samples[1:], strict=False)) / pad.height
    vertical = dy >= dx
    size = pad.height if vertical else pad.width
    names = ("down", "up") if vertical else ("right", "left")  # positive, negative
    hysteresis = MIN_MOVE * size

    def coord(s: _Sample) -> float:
        return s.cy if vertical else s.cx

    out: list[TouchGesture] = []
    start_i, extreme_i, sign = 0, 0, 0
    for i in range(1, len(samples)):
        c = coord(samples[i])
        if sign == 0:
            moved = c - coord(samples[start_i])
            if abs(moved) >= hysteresis:
                sign, extreme_i = (1 if moved > 0 else -1), i
            continue
        if (c - coord(samples[extreme_i])) * sign > 0:
            extreme_i = i
        elif abs(c - coord(samples[extreme_i])) >= hysteresis:
            out.append(_segment(samples, start_i, extreme_i, sign, names, size))
            start_i, sign = extreme_i, -sign
            extreme_i = i
    if sign != 0:
        out.append(_segment(samples, start_i, extreme_i, sign, names, size))
    return out


def _segment(
    samples: list[_Sample], lo: int, hi: int, sign: int, names: tuple[str, str], size: int
) -> TouchGesture:
    vertical = names[0] == "down"
    a, b = samples[lo], samples[hi]
    moved = abs((b.cy - a.cy) if vertical else (b.cx - a.cx)) / size
    return TouchGesture(
        kind="scroll",
        t_ms=a.t,
        until_ms=b.t,
        direction=names[0] if sign > 0 else names[1],
        distance=round(moved, 2),
        frames=hi - lo + 1,
    )


class GestureTracker:
    """Collects the frames of one touch; returns its gestures when the fingers lift."""

    def __init__(self, pad: TouchpadGeometry, idle_ms: int = IDLE_MS) -> None:
        self.pad = pad
        self.idle_ms = idle_ms
        self._samples: list[_Sample] = []
        self._ids: tuple[int, int] | None = None
        self._frames = 0  # frames of the current touch with at least one finger
        self._many = 0  # ...and with three or more
        self._touching = False
        self._last_t: int | None = None
        # What the last two-finger touch measured and became (for the log).
        self.last_touch: dict[str, object] | None = None

    def add(self, frame: ContactFrame) -> list[TouchGesture]:
        out: list[TouchGesture] = []
        if self._last_t is not None and frame.t_ms - self._last_t > self.idle_ms:
            out.extend(self._finish())  # a lift that sent no empty frame
        self._last_t = frame.t_ms
        n = len(frame.contacts)
        if n == 0:
            out.extend(self._finish())
            return out
        self._touching = True
        self._frames += 1
        if n >= 3:
            self._many += 1
            return out
        if n != 2:
            return out
        a, b = sorted(frame.contacts, key=lambda c: c.id)
        ids = (a.id, b.id)
        if self._ids is not None and ids != self._ids:
            # other fingers: the previous pair ended without lifting everything
            out.extend(self._classify())
        self._ids = ids
        self._samples.append(
            _Sample(frame.t_ms, math.hypot(a.x - b.x, a.y - b.y), (a.x + b.x) / 2, (a.y + b.y) / 2)
        )
        return out

    def due(self, now_ms: int) -> list[TouchGesture]:
        """Finish a touch whose fingers lifted without an empty frame."""
        if self._touching and self._last_t is not None and now_ms - self._last_t > self.idle_ms:
            return self._finish()
        return []

    def flush(self) -> list[TouchGesture]:
        return self._finish()

    @property
    def _system_gesture(self) -> bool:
        limit = max(MAX_EXTRA_FINGER_FRAMES, MAX_EXTRA_FINGER_SHARE * self._frames)
        return self._many > limit

    def _classify(self) -> list[TouchGesture]:
        samples, self._samples, self._ids = self._samples, [], None
        if not samples:
            return []
        spread, travel = touch_measures(samples, self.pad)
        if self._system_gesture:
            result: list[TouchGesture] = []
            verdict = "dropped: three or more fingers"
        else:
            result = classify_touch(samples, self.pad)
            verdict = ", ".join(f"{g.kind} {g.direction}" for g in result) or "dropped: no gesture"
        self.last_touch = {
            "two_finger_frames": len(samples),
            "frames": self._frames,
            "extra_finger_frames": self._many,
            "spread": round(spread, 3),
            "travel": round(travel, 3),
            "result": verdict,
        }
        return result

    def _finish(self) -> list[TouchGesture]:
        out = self._classify()
        self._frames = 0
        self._many = 0
        self._touching = False
        return out


__all__ = [
    "IDLE_MS",
    "MIN_FRAMES",
    "MAX_EXTRA_FINGER_FRAMES",
    "MAX_EXTRA_FINGER_SHARE",
    "MIN_MOVE",
    "PINCH_MAX_TRAVEL",
    "PINCH_MIN_SCALE",
    "SCROLL_MIN_TRAVEL",
    "Contact",
    "ContactFrame",
    "GestureTracker",
    "TouchGesture",
    "TouchpadGeometry",
    "classify_touch",
    "touch_measures",
]
