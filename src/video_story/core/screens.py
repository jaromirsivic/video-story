"""Screen model and deterministic screen numbering.

Screens are numbered ``screen1``, ``screen2``, ... with the primary screen first and
the remaining screens ordered by their top-left corner in the virtual desktop
(x first, then y). All geometry is in physical pixels of the virtual desktop, which
is also the pixel size of the captured image of each screen.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .naming import screen_id


@dataclass(frozen=True)
class RawScreen:
    """A screen as reported by the platform layer (before numbering)."""

    x: int
    y: int
    width: int
    height: int
    primary: bool = False
    scale: float = 1.0
    name: str = ""


@dataclass(frozen=True)
class Screen:
    id: str
    index: int
    x: int
    y: int
    width: int
    height: int
    primary: bool
    scale: float
    name: str = ""

    @property
    def right(self) -> int:
        return self.x + self.width

    @property
    def bottom(self) -> int:
        return self.y + self.height

    def contains(self, gx: int, gy: int) -> bool:
        """True when the global point lies inside this screen (half-open bounds)."""
        return self.x <= gx < self.right and self.y <= gy < self.bottom

    def to_local(self, gx: int, gy: int) -> tuple[int, int]:
        return gx - self.x, gy - self.y

    def to_manifest(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "primary": self.primary,
            "bounds": {"x": self.x, "y": self.y, "width": self.width, "height": self.height},
            "scale": self.scale,
            "name": self.name,
        }

    @classmethod
    def from_manifest(cls, data: dict[str, Any]) -> Screen:
        bounds = data["bounds"]
        return cls(
            id=data["id"],
            index=int(data["id"].removeprefix("screen")),
            x=int(bounds["x"]),
            y=int(bounds["y"]),
            width=int(bounds["width"]),
            height=int(bounds["height"]),
            primary=bool(data.get("primary", False)),
            scale=float(data.get("scale", 1.0)),
            name=str(data.get("name", "")),
        )


def assign_screen_ids(raw_screens: list[RawScreen]) -> list[Screen]:
    """Number screens deterministically: primary first, then by (x, y)."""
    if not raw_screens:
        raise ValueError("no screens reported")
    primaries = [s for s in raw_screens if s.primary]
    if not primaries:
        # Fall back to the screen whose top-left corner is closest to the origin.
        primaries = [min(raw_screens, key=lambda s: (abs(s.x) + abs(s.y), s.x, s.y))]
    primary = primaries[0]
    others = sorted((s for s in raw_screens if s is not primary), key=lambda s: (s.x, s.y))
    ordered = [primary, *others]
    return [
        Screen(
            id=screen_id(index),
            index=index,
            x=s.x,
            y=s.y,
            width=s.width,
            height=s.height,
            primary=s is primary,
            scale=s.scale,
            name=s.name,
        )
        for index, s in enumerate(ordered, start=1)
    ]


def find_screen(screens: list[Screen], screen_id_: str) -> Screen | None:
    for screen in screens:
        if screen.id == screen_id_:
            return screen
    return None


__all__ = ["RawScreen", "Screen", "assign_screen_ids", "find_screen"]
