"""Editing subtitles with the mouse on the two narration tracks - pure logic, no Qt.

Once the user unlocks the narration in the editor, every subtitle box has zones that
depend on its width in pixels at the current zoom (they are not drawn; the cursor
shows them):

- **yellow** - :data:`EDGE_PX` at each edge: dragging moves that edge (the start or
  the end of the subtitle);
- **green** - the rest in the middle: dragging moves the whole subtitle in time and,
  vertically, to the other track;
- **orange** - where two subtitles of one track touch (the end of one is the start of
  the next): :data:`ROLL_PX` taken from each yellow zone at that edge. Dragging moves
  the shared edge: one subtitle gets shorter and the other longer (a roll edit). A
  subtitle without a yellow zone there (a narrow one) gets no orange part of its own,
  but the orange part of its wider neighbour still rolls it.

A box of :data:`FULL_ZONES_PX` or more has all zones; a narrower one wider than
:data:`MIN_INTERACTIVE_PX` is green as a whole (it can only be moved); a box of
:data:`MIN_INTERACTIVE_PX` or less has no zone (zoom in to edit it).

Rules: a subtitle lasts at least :data:`MIN_DURATION_MS` (one already shorter may be
moved but not shortened), times move in :data:`STEP_MS` steps, subtitles of one track
never overlap (touching is fine; the two tracks may overlap), and edges are pulled
onto magnets (the edges of the other subtitles of the target track, keypoints, pauses
and the playhead) within :data:`SNAP_PX`.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum

from video_story.core.transcript import LOWER_TRACK, UPPER_TRACK, Segment, overlaps

EDGE_PX = 16  # yellow zone at each edge
ROLL_PX = 6  # orange zone: taken from a yellow zone where two subtitles touch
MIN_INTERACTIVE_PX = 16  # boxes this narrow or narrower have no zone
FULL_ZONES_PX = 48  # boxes this wide or wider have yellow, green (and orange) zones
MIN_DURATION_MS = 500  # shortest subtitle the user can make
STEP_MS = 10  # subtitles move in hundredths of a second
SNAP_PX = 8  # the magnet (as for the crop handles)
NEW_SUBTITLE_MS = 1000  # Add New Subtitle: the length of the new subtitle

TRACKS = (UPPER_TRACK, LOWER_TRACK)


class Zone(Enum):
    NONE = "none"
    MOVE = "move"  # green
    RESIZE_START = "resize_start"  # yellow at the start
    RESIZE_END = "resize_end"  # yellow at the end
    ROLL = "roll"  # orange: the shared edge of two touching subtitles


@dataclass(frozen=True)
class Hit:
    """What a press at a point of the narration would edit."""

    zone: Zone
    segment: Segment | None = None  # the subtitle under the pointer
    partner: Segment | None = None  # ROLL: the other subtitle at the shared edge

    @property
    def editable(self) -> bool:
        return self.zone is not Zone.NONE

    @property
    def roll_pair(self) -> tuple[Segment, Segment] | None:
        """ROLL: (the subtitle before the edge, the one after it)."""
        if self.zone is not Zone.ROLL or self.segment is None or self.partner is None:
            return None
        if self.segment.end_ms == self.partner.start_ms:
            return self.segment, self.partner
        return self.partner, self.segment


@dataclass(frozen=True)
class Proposal:
    """The result of a drag so far: ``old`` becomes ``new`` when it is ``valid``."""

    old: tuple[Segment, ...]
    new: tuple[Segment, ...]
    valid: bool
    reason: str = ""

    @property
    def changed(self) -> bool:
        return self.old != self.new


def has_text(segment: Segment) -> bool:
    return bool(segment.text.strip())


def track_segments(segments: list[Segment], track: int) -> list[Segment]:
    """The subtitles with words of one track, in time order."""
    return sorted(
        (s for s in segments if s.track == track and has_text(s)),
        key=lambda s: (s.start_ms, s.end_ms),
    )


def own_zone(width_px: float, offset_px: float) -> Zone:
    """The zone of a box ``width_px`` wide at ``offset_px`` from its left edge (no
    neighbours considered)."""
    if width_px <= MIN_INTERACTIVE_PX or offset_px < 0 or offset_px > width_px:
        return Zone.NONE
    if width_px < FULL_ZONES_PX:
        return Zone.MOVE
    if offset_px < EDGE_PX:
        return Zone.RESIZE_START
    if offset_px > width_px - EDGE_PX:
        return Zone.RESIZE_END
    return Zone.MOVE


def hit_test(segments: list[Segment], track: int, time_ms: float, px_per_ms: float) -> Hit:
    """The zone under the pointer at ``time_ms`` on ``track`` (``px_per_ms`` = zoom)."""
    if px_per_ms <= 0:
        return Hit(Zone.NONE)
    lane = track_segments(segments, track)
    for index, segment in enumerate(lane):
        if not segment.start_ms <= time_ms <= segment.end_ms:
            continue
        nxt = lane[index + 1] if index + 1 < len(lane) else None
        if nxt is not None and nxt.start_ms == segment.end_ms and time_ms >= segment.end_ms:
            continue  # exactly on a shared edge: the subtitle after it
        width = (segment.end_ms - segment.start_ms) * px_per_ms
        offset = (time_ms - segment.start_ms) * px_per_ms
        zone = own_zone(width, offset)
        prev = lane[index - 1] if index > 0 else None
        if zone is Zone.RESIZE_START and offset < ROLL_PX:
            if prev is not None and prev.end_ms == segment.start_ms:
                return Hit(Zone.ROLL, segment, prev)
        if zone is Zone.RESIZE_END and width - offset < ROLL_PX:
            if nxt is not None and nxt.start_ms == segment.end_ms:
                return Hit(Zone.ROLL, segment, nxt)
        return Hit(zone, segment if zone is not Zone.NONE else None)
    return Hit(Zone.NONE)


def quantize(time_ms: float) -> int:
    return int(round(time_ms / STEP_MS)) * STEP_MS


def _min_duration(segment: Segment) -> int:
    """A subtitle already shorter than the minimum keeps its length (it is not made
    shorter, but it is not stretched either)."""
    return min(MIN_DURATION_MS, segment.end_ms - segment.start_ms)


@dataclass(frozen=True)
class Magnets:
    """Moments edges are pulled onto: keypoints, pauses and the playhead (rounded to
    :data:`STEP_MS`). The edges of the other subtitles are added per drag (exact)."""

    markers: tuple[int, ...] = ()

    @classmethod
    def of(cls, keypoints=(), pauses=(), playhead: int | None = None) -> Magnets:
        points = {quantize(k) for k in keypoints} | {quantize(p) for p in pauses}
        if playhead is not None:
            points.add(quantize(playhead))
        return cls(tuple(sorted(points)))


def _snap(candidates: list[int], targets: list[int], tolerance_ms: float) -> int | None:
    """The shift (ms) that puts the candidate edge closest to a target onto it, or
    ``None`` when none is within ``tolerance_ms``."""
    best: int | None = None
    for edge in candidates:
        for target in targets:
            shift = target - edge
            if abs(shift) <= tolerance_ms and (best is None or abs(shift) < abs(best)):
                best = shift
    return best


class Drag:
    """One drag of a subtitle zone, from the press to the release.

    ``segments`` are all subtitles of the recording when the drag started;
    ``duration_ms`` the length of the recording. :meth:`propose` gives the result for
    the pointer moved by ``delta_ms`` (any value; it is rounded) over ``track``.
    """

    def __init__(
        self,
        hit: Hit,
        segments: list[Segment],
        duration_ms: int,
        magnets: Magnets | None = None,
    ) -> None:
        if not hit.editable or hit.segment is None:
            raise ValueError("nothing to drag here")
        self.hit = hit
        self.segments = list(segments)
        self.duration_ms = max(0, int(duration_ms))
        self.magnets = magnets or Magnets()

    @property
    def zone(self) -> Zone:
        return self.hit.zone

    @property
    def segment(self) -> Segment:
        assert self.hit.segment is not None
        return self.hit.segment

    def _others(self, track: int, exclude: tuple[Segment, ...]) -> list[Segment]:
        return [s for s in track_segments(self.segments, track) if s not in exclude]

    def _targets(self, track: int, exclude: tuple[Segment, ...]) -> list[int]:
        edges = {t for s in self._others(track, exclude) for t in (s.start_ms, s.end_ms)}
        return sorted(edges | set(self.magnets.markers))

    def propose(self, delta_ms: float, track: int, snap_ms: float = 0.0) -> Proposal:
        """The subtitles after moving the pointer by ``delta_ms`` while over ``track``
        (only a move changes the track); ``snap_ms`` is the magnet's reach."""
        if self.zone is Zone.ROLL:
            return self._roll(delta_ms, snap_ms)
        if self.zone is Zone.MOVE:
            return self._move(delta_ms, track, snap_ms)
        return self._resize(delta_ms, snap_ms)

    def _check(self, old: tuple[Segment, ...], new: tuple[Segment, ...]) -> Proposal:
        for segment in new:
            for other in self._others(segment.track, old):
                if overlaps(segment, other):
                    return Proposal(old, new, False, "overlaps another subtitle")
        return Proposal(old, new, True)

    def _move(self, delta_ms: float, track: int, snap_ms: float) -> Proposal:
        segment = self.segment
        track = track if track in TRACKS else segment.track
        length = segment.end_ms - segment.start_ms
        delta = quantize(delta_ms)
        start = segment.start_ms + delta
        if snap_ms > 0:
            targets = self._targets(track, (segment,))
            start += _snap([start, start + length], targets, snap_ms) or 0
        start = max(0, min(start, self.duration_ms - length))
        new = replace(segment, start_ms=start, end_ms=start + length, track=track)
        return self._check((segment,), (new,))

    def _resize(self, delta_ms: float, snap_ms: float) -> Proposal:
        segment = self.segment
        shortest = _min_duration(segment)
        targets = self._targets(segment.track, (segment,))
        if self.zone is Zone.RESIZE_START:
            start = segment.start_ms + quantize(delta_ms)
            shift = _snap([start], targets, snap_ms) if snap_ms > 0 else None
            start = start + (shift or 0)
            start = max(0, min(start, segment.end_ms - shortest))
            new = replace(segment, start_ms=start)
        else:
            end = segment.end_ms + quantize(delta_ms)
            shift = _snap([end], targets, snap_ms) if snap_ms > 0 else None
            end = end + (shift or 0)
            end = min(self.duration_ms, max(end, segment.start_ms + shortest))
            new = replace(segment, end_ms=end)
        return self._check((segment,), (new,))

    def _roll(self, delta_ms: float, snap_ms: float) -> Proposal:
        pair = self.hit.roll_pair
        assert pair is not None
        before, after = pair
        edge = before.end_ms + quantize(delta_ms)
        if snap_ms > 0:
            shift = _snap([edge], list(self.magnets.markers), snap_ms)
            edge += shift or 0
        # Both limits keep the shared edge where it was at the worst (a side already
        # shorter than the minimum cannot give way).
        edge = max(before.start_ms + _min_duration(before), edge)
        edge = min(after.end_ms - _min_duration(after), edge)
        old = (before, after)
        new = (replace(before, end_ms=edge), replace(after, start_ms=edge))
        return self._check(old, new)


def apply(segments: list[Segment], old: tuple[Segment, ...], new: tuple[Segment, ...]):
    """``segments`` with ``old`` replaced by ``new``.

    Of equal length, ``old[i]`` becomes ``new[i]`` in its place (a drag, an Edit).
    Otherwise every subtitle of ``old`` is removed and every one of ``new`` added at
    the end: ``old`` empty adds a subtitle, ``new`` empty deletes one (the caller sorts).
    """
    missing = [s for s in old if s not in segments]
    if missing:
        raise ValueError("a subtitle to change is not part of the transcript")
    if len(old) == len(new):
        mapping = dict(zip(old, new, strict=False))
        return [mapping.get(s, s) for s in segments]
    remaining = list(segments)
    for segment in old:
        remaining.remove(segment)  # one occurrence per subtitle of ``old``
    return remaining + list(new)


def free_slot(
    segments: list[Segment],
    track: int,
    at_ms: float,
    duration_ms: int,
    length_ms: int = NEW_SUBTITLE_MS,
) -> tuple[int, int] | None:
    """Where Add New Subtitle puts a subtitle of ``length_ms`` for a click at ``at_ms``
    on ``track``: starting at the click (rounded to :data:`STEP_MS`), moved left as far
    as needed to end inside the free gap around the click (and the recording).
    ``None`` when the click lies on a subtitle or the gap is shorter than
    ``length_ms``."""
    duration_ms = max(0, int(duration_ms))
    if at_ms < 0 or at_ms > duration_ms or length_ms <= 0:
        return None
    gap_start, gap_end = 0, duration_ms
    for segment in track_segments(segments, track):
        if segment.start_ms < at_ms < segment.end_ms:
            return None  # inside a subtitle
        if segment.end_ms <= at_ms:
            gap_start = max(gap_start, segment.end_ms)
        elif segment.start_ms >= at_ms:
            gap_end = min(gap_end, segment.start_ms)
    if gap_end - gap_start < length_ms:
        return None
    start = max(gap_start, quantize(at_ms))
    start = min(start, gap_end - length_ms)
    return start, start + length_ms


def validate(segments: list[Segment]) -> str | None:
    """Why ``segments`` break the rules of the tracks (``None`` when they do not):
    two subtitles of one track overlapping."""
    for track in TRACKS:
        lane = track_segments(segments, track)
        for a, b in zip(lane, lane[1:], strict=False):
            if overlaps(a, b):
                return "two subtitles of one track overlap"
    return None


__all__ = [
    "EDGE_PX",
    "FULL_ZONES_PX",
    "MIN_DURATION_MS",
    "MIN_INTERACTIVE_PX",
    "NEW_SUBTITLE_MS",
    "ROLL_PX",
    "SNAP_PX",
    "STEP_MS",
    "Drag",
    "Hit",
    "Magnets",
    "Proposal",
    "Zone",
    "apply",
    "free_slot",
    "has_text",
    "hit_test",
    "own_zone",
    "quantize",
    "track_segments",
    "validate",
]
