"""Drawing palette: 9 high-contrast colors for the on-screen overlay (keys 1-9).

The palette is plain Python (no Qt) so that it can be validated and listed without a
GUI. The first entry is the default drawing color (red).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PaletteColor:
    """One palette entry: a display name and an ``#RRGGBB`` hex value."""

    name: str
    hex: str

    @property
    def rgb(self) -> tuple[int, int, int]:
        value = self.hex.lstrip("#")
        return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


PALETTE: list[PaletteColor] = [
    PaletteColor("Red", "#FF0000"),
    PaletteColor("Orange", "#FF8C00"),
    PaletteColor("Yellow", "#FFFF00"),
    PaletteColor("Green", "#00C000"),
    PaletteColor("Cyan", "#00E5FF"),
    PaletteColor("Blue", "#0055FF"),
    PaletteColor("Magenta", "#FF00FF"),
    PaletteColor("White", "#FFFFFF"),
    PaletteColor("Black", "#000000"),
]

DEFAULT_COLOR: PaletteColor = PALETTE[0]


def find_color(hex_value: str) -> PaletteColor | None:
    """Return the palette entry with the given hex value (case-insensitive), if any."""
    wanted = hex_value.strip().upper()
    for entry in PALETTE:
        if entry.hex.upper() == wanted:
            return entry
    return None


__all__ = ["DEFAULT_COLOR", "PALETTE", "PaletteColor", "find_color"]
