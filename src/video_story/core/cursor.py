"""Cursor position mapping from the virtual desktop onto a particular screen image."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .screens import Screen


@dataclass(frozen=True)
class CursorPosition:
    """Cursor location expressed in pixels of the image of ``screen_id``."""

    screen_id: str
    x: int
    y: int

    def to_json(self) -> dict[str, Any]:
        return {"screen": self.screen_id, "x": self.x, "y": self.y}


def locate_cursor(screens: list[Screen], gx: int, gy: int) -> CursorPosition | None:
    """Map a global (virtual desktop, physical pixel) point to a screen.

    Returns ``None`` when the point is outside every screen, which can happen for a
    moment while the pointer crosses a gap between screens with different sizes.
    """
    for screen in screens:
        if screen.contains(gx, gy):
            lx, ly = screen.to_local(gx, gy)
            return CursorPosition(screen.id, lx, ly)
    return None


def cursor_from_json(data: dict[str, Any] | None) -> CursorPosition | None:
    if not data:
        return None
    return CursorPosition(str(data["screen"]), int(data["x"]), int(data["y"]))


__all__ = ["CursorPosition", "cursor_from_json", "locate_cursor"]
