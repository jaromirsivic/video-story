"""Summarise a smoke recording for CI: GitHub annotations and the step summary.

Usage: python scripts/ci_report.py <recording_folder> [<label>]

Annotations (``::notice::``) and ``$GITHUB_STEP_SUMMARY`` are readable on the public run
page without signing in, unlike the raw job logs.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def summarise(folder: Path) -> dict:
    sessions = sorted(p for p in folder.iterdir() if p.is_dir() and not p.name.startswith("."))
    if not sessions:
        return {"error": f"no recording in {folder}"}
    session = sessions[-1]
    manifest = json.loads((session / "manifest.json").read_text(encoding="utf-8"))
    frames_dir = session / manifest["frames"]["dir"]
    frames = sorted(p for p in frames_dir.iterdir() if p.suffix in {".png", ".jpg"})
    sidecars = sorted(frames_dir.glob("*.json"))
    report: dict = {
        "session": session.name,
        "platform": manifest.get("platform"),
        "recorded_duration_ms": manifest.get("recorded_duration_ms"),
        "screens": [
            {
                "id": s["id"],
                "bounds": s["bounds"],
                "image_size": s.get("image_size"),
                "pixel_ratio": s.get("pixel_ratio"),
                "primary": s["primary"],
            }
            for s in manifest["screens"]
        ],
        "frames": len(frames),
        "dropped_ticks": manifest["frames"].get("dropped_ticks"),
        "first_frames": [p.name for p in frames[:4]],
    }
    try:
        from PIL import Image

        sizes = {}
        for frame in frames[:4]:
            with Image.open(frame) as im:
                sizes[frame.name] = {"size": im.size, "extrema": im.convert("RGB").getextrema()}
        report["image_check"] = sizes
    except Exception as exc:  # pragma: no cover
        report["image_check"] = f"unavailable: {exc}"
    if sidecars:
        report["sidecar"] = json.loads(sidecars[0].read_text(encoding="utf-8"))
    return report


def main() -> int:
    folder = Path(sys.argv[1])
    label = sys.argv[2] if len(sys.argv) > 2 else folder.name
    report = summarise(folder) if folder.exists() else {"error": f"{folder} missing"}
    text = json.dumps(report, ensure_ascii=False)
    print(text)
    # One notice per report; GitHub truncates very long messages, keep it compact.
    print(f"::notice title=Video Story smoke {label}::{text[:3800]}")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(f"## Smoke recording ({label})\n\n```json\n")
            fh.write(json.dumps(report, indent=2, ensure_ascii=False))
            fh.write("\n```\n")
    return 0 if "error" not in report else 1


if __name__ == "__main__":
    sys.exit(main())
