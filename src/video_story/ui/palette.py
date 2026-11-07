"""Drawing palette: 16 high-contrast colors for the on-screen overlay.

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
    PaletteColor("Yellow", "#FFFF00"),
    PaletteColor("Lime", "#00FF00"),
    PaletteColor("Cyan", "#00FFFF"),
    PaletteColor("Magenta", "#FF00FF"),
    PaletteColor("Orange", "#FF8000"),
    PaletteColor("White", "#FFFFFF"),
    PaletteColor("Black", "#000000"),
    PaletteColor("Blue", "#0040FF"),
    PaletteColor("Deep pink", "#FF1493"),
    PaletteColor("Spring green", "#00FF80"),
    PaletteColor("Violet", "#8000FF"),
    PaletteColor("Gold", "#FFC000"),
    PaletteColor("Dark red", "#8B0000"),
    PaletteColor("Navy", "#000080"),
    PaletteColor("Teal", "#008080"),
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
