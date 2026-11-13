"""The skill launchers find the repository root from any working directory.

Hosts hand the agent the path of the junction (Windows) or symlink (Linux, macOS) the
skill is registered with, so "three levels above the script" is not the repository. The
launcher of the current platform runs here from a copy of the layout in ``tmp_path``
with a stub ``uv`` first on PATH; nothing touches the real repository or the real uv.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SKILLS_DIR = Path(__file__).resolve().parents[2] / "skills"
SKILLS = ("video-story", "video-story-edit")
WINDOWS = sys.platform == "win32"
EXT = ".cmd" if WINDOWS else ".sh"
MARKER = "launcher-test-root-marker"
STUB_EXIT = 7
ROOT_NOT_FOUND_EXIT = 21

# The stub answers "is --project the directory with the marker" itself, so the test never
# has to decode a path printed by cmd.exe in the console code page.
STUB_CMD = f"""@echo off
if exist "%~4\\{MARKER}" echo ROOT_OK
echo ARGS=%5 %6 %7
exit /b {STUB_EXIT}
"""
STUB_SH = f"""#!/bin/sh
[ -f "$4/{MARKER}" ] && echo ROOT_OK
echo "ARGS=$5 $6 [$7]"
exit {STUB_EXIT}
"""
EXPECTED_ARGS = 'ARGS=video-story version "a b"' if WINDOWS else "ARGS=video-story version [a b]"


def _root(base: Path, skill: str, *, marker: bool) -> Path:
    """A stand-in repository: pyproject.toml and a copy of the launcher three levels below."""
    scripts = base / "skills" / skill / "scripts"
    scripts.mkdir(parents=True)
    (base / "pyproject.toml").write_text("# stand-in\n", encoding="utf-8")
    if marker:
        (base / MARKER).write_text("", encoding="utf-8")
    shutil.copyfile(SKILLS_DIR / skill / "scripts" / f"{skill}{EXT}", scripts / f"{skill}{EXT}")
    return base


def _link(link: Path, target: Path) -> None:
    """Junction on Windows (what install-hosts.ps1 creates), symlink elsewhere."""
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        if WINDOWS:
            subprocess.run(
                [os.environ.get("ComSpec", "cmd.exe"), "/d", "/c", "mklink", "/J", link, target],
                check=True,
                capture_output=True,
            )
        else:
            os.symlink(target, link, target_is_directory=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        pytest.skip(f"cannot create a directory link here: {exc}")


@pytest.fixture
def run(tmp_path: Path):
    """Run a launcher with ``version "a b"`` from a directory outside every root."""
    stub = tmp_path / "stub"
    stub.mkdir()
    if WINDOWS:
        (stub / "uv.cmd").write_text(STUB_CMD, encoding="ascii", newline="\r\n")
    else:
        (stub / "uv").write_text(STUB_SH, encoding="ascii", newline="\n")
        (stub / "uv").chmod(0o755)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    def _run(launcher: Path, *, cwd: Path = elsewhere, root: Path | None = None):
        env = dict(os.environ)
        env.pop("VIDEO_STORY_ROOT", None)
        if root is not None:
            env["VIDEO_STORY_ROOT"] = str(root)
        env["PATH"] = f"{stub}{os.pathsep}{env.get('PATH', '')}"
        if WINDOWS:
            # /s with one pair of outer quotes: the only form cmd.exe parses reliably.
            comspec = os.environ.get("ComSpec", "cmd.exe")
            command: str | list[str] = f'"{comspec}" /d /s /c ""{launcher}" version "a b""'
        else:
            command = ["sh", str(launcher), "version", "a b"]
        return subprocess.run(
            command,
            cwd=cwd,
            env=env,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )

    return _run


def _assert_ran_uv_in_root(proc: subprocess.CompletedProcess) -> None:
    assert proc.returncode == STUB_EXIT, proc.stderr  # uv's exit code is passed on
    lines = proc.stdout.splitlines()
    assert "ROOT_OK" in lines, (proc.stdout, proc.stderr)
    assert EXPECTED_ARGS in lines


@pytest.mark.parametrize("skill", SKILLS)
def test_real_path_from_a_foreign_cwd(tmp_path: Path, run, skill: str):
    root = _root(tmp_path / "repo", skill, marker=True)
    _assert_ran_uv_in_root(run(root / "skills" / skill / "scripts" / f"{skill}{EXT}"))


@pytest.mark.parametrize("skill", SKILLS)
def test_link_path_from_a_foreign_cwd(tmp_path: Path, run, skill: str):
    """The link is named differently from the skill and has look-alike neighbours that
    lead to another root; only the launcher's own link may be followed."""
    root = _root(tmp_path / "repo", skill, marker=True)
    decoy = _root(tmp_path / "decoy", skill, marker=False)
    host = tmp_path / "host dir" / "skills"
    _link(host / "linked", root / "skills" / skill)
    for neighbour in ("linked-edit", "my linked", "link"):
        _link(host / neighbour, decoy / "skills" / skill)
    try:
        _assert_ran_uv_in_root(run(host / "linked" / "scripts" / f"{skill}{EXT}"))
    finally:
        for name in ("linked", "linked-edit", "my linked", "link"):
            # rmdir/unlink removes the link itself, never the target's content
            (host / name).rmdir() if WINDOWS else (host / name).unlink()


@pytest.mark.parametrize("skill", SKILLS)
def test_root_not_found_is_one_json_object(tmp_path: Path, run, skill: str):
    """No root above the launcher: a JSON error, and uv is not tried even though the
    working directory is a project (that is what made the old launcher work by luck)."""
    project = _root(tmp_path / "repo", skill, marker=True)
    bare = tmp_path / "bare" / "a" / "b" / "scripts"
    bare.mkdir(parents=True)
    launcher = bare / f"{skill}{EXT}"
    shutil.copyfile(SKILLS_DIR / skill / "scripts" / launcher.name, launcher)

    proc = run(launcher, cwd=project)

    assert proc.returncode == ROOT_NOT_FOUND_EXIT
    assert proc.stdout == ""
    (line,) = proc.stderr.strip().splitlines()
    error = json.loads(line)
    assert error["ok"] is False
    assert error["code"] == "ROOT_NOT_FOUND"
    assert "VIDEO_STORY_ROOT" in error["message"]

    # VIDEO_STORY_ROOT makes the same copy work.
    _assert_ran_uv_in_root(run(launcher, root=project))


@pytest.mark.parametrize("skill", SKILLS)
def test_video_story_root_without_a_project_is_not_used(tmp_path: Path, run, skill: str):
    root = _root(tmp_path / "repo", skill, marker=True)
    launcher = root / "skills" / skill / "scripts" / f"{skill}{EXT}"
    _assert_ran_uv_in_root(run(launcher, root=tmp_path / "elsewhere"))


@pytest.mark.parametrize("ext", [".cmd", ".sh"])
def test_launchers_are_lf_and_differ_only_in_the_title_comment(ext: str):
    """LF endings (a CRLF .sh does not run; the .cmd files avoid labels so LF is safe),
    and one text for both skills."""
    first, second = (
        (SKILLS_DIR / skill / "scripts" / f"{skill}{ext}").read_bytes() for skill in SKILLS
    )
    assert b"\r" not in first and b"\r" not in second
    differing = [
        (a, b) for a, b in zip(first.splitlines(), second.splitlines(), strict=True) if a != b
    ]
    assert len(differing) == 1
    assert b"launcher" in differing[0][0] and b"Editor launcher" in differing[0][1]
