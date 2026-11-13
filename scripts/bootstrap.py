"""No-install entry point, runnable even when the application package is missing."""

import importlib.util
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEPENDENCIES = {
    "video_story",
    "platformdirs",
    "PySide6",
    "numpy",
    "PIL",
    "sounddevice",
    "mss",
    "pywhispercpp",
    "mcp",
    "anyio",
    "soundcard",
    "comtypes",
    "Xlib",
}


def fail(message, **details):
    print(
        json.dumps({"ok": False, "code": "ENVIRONMENT_INVALID", "message": message, **details}),
        file=sys.stderr,
    )
    return 23


def main():
    # Mark entry for the shell: an interpreter that never ran this file is a different failure.
    import os

    marker = os.environ.pop("VS_BOOT_MARKER", None)
    if marker:
        Path(marker).touch()
    if sys.version_info < (3, 11):  # noqa: UP036 - diagnoses an unsupported interpreter
        return fail("Python is too old to read project metadata. Prepare Python with uv.")
    import tomllib

    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    requirement = project["project"]["requires-python"]
    current = tuple(sys.version_info[:3])
    for term in requirement.split(","):
        match = re.fullmatch(r"\s*(>=|<=|==|>|<)\s*(\d+)\.(\d+)(?:\.(\d+))?\s*", term)
        if match is None:
            return fail("Unsupported requires-python constraint.", requirement=requirement)
        op, major, minor, patch = match.groups()
        target = (int(major), int(minor), int(patch or 0))
        if not {
            ">=": current >= target,
            "<=": current <= target,
            "==": current == target,
            ">": current > target,
            "<": current < target,
        }[op]:
            return fail(
                "Python does not satisfy the project requirement.",
                requirement=requirement,
                python=sys.version.split()[0],
            )
    if importlib.util.find_spec("video_story") is None:
        return fail("Video Story is not installed. Run uv sync --locked for this project.")
    try:
        from video_story.host.cli import main as cli_main

        return cli_main()
    except ModuleNotFoundError as exc:
        if (exc.name or "").split(".")[0] not in DEPENDENCIES:
            raise
        return fail(
            "A required library is missing. Run uv sync --locked for this project.",
            missing=[exc.name],
        )


if __name__ == "__main__":
    raise SystemExit(main())
