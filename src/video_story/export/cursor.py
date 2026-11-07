"""The arrow pointer drawn over exported video (generated, no binary asset in the repo).

A classic 24x32 px arrow: white fill with a black outline, drawn with Pillow. The tip
of the arrow is at :data:`CURSOR_HOTSPOT` (the top-left corner), so the overlay is
placed at ``cursor - hotspot`` to put the tip on the recorded cursor position.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

CURSOR_SIZE = (24, 32)
CURSOR_HOTSPOT = (0, 0)

# Arrow outline, clockwise from the tip (pixels, inside CURSOR_SIZE).
_ARROW = [(0, 0), (0, 25), (6, 19), (11, 30), (16, 28), (11, 17), (19, 17)]


def cursor_image() -> Image.Image:
    """The pointer as an RGBA image (transparent background)."""
    image = Image.new("RGBA", CURSOR_SIZE, (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.polygon(_ARROW, fill=(255, 255, 255, 255), outline=(0, 0, 0, 255))
    draw.line([*_ARROW, _ARROW[0]], fill=(0, 0, 0, 255), width=2, joint="curve")
    return image


def write_cursor_png(path: Path) -> Path:
    """Write the pointer PNG to ``path`` and return it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    cursor_image().save(path, format="PNG")
    return path


__all__ = ["CURSOR_HOTSPOT", "CURSOR_SIZE", "cursor_image", "write_cursor_png"]
