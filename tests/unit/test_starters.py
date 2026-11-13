"""The starters (``start-video-story.bat`` / ``.sh``) run ``setup`` only when needed.

They do not look for the model or FFmpeg themselves: ``video-story setup --check``
answers (exit code 0: ready, 4: setup needed), so the state folder rules live in one
place. The starter of the current platform runs here from a copy in ``tmp_path`` with
a stub ``uv`` first on PATH that logs its arguments; no window is opened and nothing
is downloaded.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
WINDOWS = sys.platform == "win32"
STARTER = "start-video-story.bat" if WINDOWS else "start-video-story.sh"

# Arguments as uv sees them: run --project . video-story [--log-level WARNING] <command>...
STUB_CMD = """@echo off
echo %*>>"%VS_STUB_LOG%"
if "%8"=="--check" goto :check
if "%5"=="setup" if defined VS_STUB_SETUP_WORKS echo done>"%VS_STUB_DONE%"
exit /b 0
:check
if exist "%VS_STUB_DONE%" exit /b 0
exit /b %VS_STUB_CHECK%
"""
STUB_SH = """#!/bin/sh
echo "$*" >> "$VS_STUB_LOG"
if [ "$8" = "--check" ]; then
  [ -f "$VS_STUB_DONE" ] && exit 0
  exit "$VS_STUB_CHECK"
fi
if [ "$5" = "setup" ] && [ -n "$VS_STUB_SETUP_WORKS" ]; then echo done > "$VS_STUB_DONE"; fi
exit 0
"""
CHECK = "run --project . video-story --log-level WARNING setup --check"
SETUP = "run --project . video-story setup"
START = "run --project . video-story --log-level WARNING run --detach"


@pytest.fixture
def start(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    stub = tmp_path / "stub"
    stub.mkdir()
    if WINDOWS:
        # The real uv is an .exe; a batch file runs the .cmd stub only through `call`
        # (without it control would never come back to the starter).
        text = (REPO / STARTER).read_text(encoding="ascii")
        assert text.count("\nuv run ") == 4
        (root / STARTER).write_text(text.replace("\nuv run ", "\ncall uv run "), "ascii")
        (stub / "uv.cmd").write_text(STUB_CMD, encoding="ascii", newline="\r\n")
    else:
        shutil.copyfile(REPO / STARTER, root / STARTER)
        (stub / "uv").write_text(STUB_SH, encoding="ascii", newline="\n")
        (stub / "uv").chmod(0o755)
        for folder in ("/opt/homebrew/bin", "/usr/local/bin"):  # put before PATH
            if (Path(folder) / "uv").exists():
                pytest.skip(f"a real uv in {folder} would shadow the stub")
    log = tmp_path / "uv.log"

    def _start(check_exit: int, setup_works: bool = True) -> tuple[int, list[str]]:
        env = dict(os.environ)
        env["PATH"] = f"{stub}{os.pathsep}{env.get('PATH', '')}"
        env["HOME"] = str(tmp_path)  # the .sh adds ~/.local/bin (a real uv) before PATH
        env["VS_STUB_LOG"] = str(log)
        env["VS_STUB_DONE"] = str(tmp_path / "setup-done")
        env["VS_STUB_CHECK"] = str(check_exit)
        env.pop("VS_STUB_SETUP_WORKS", None)
        if setup_works:
            env["VS_STUB_SETUP_WORKS"] = "1"
        if WINDOWS:
            comspec = os.environ.get("ComSpec", "cmd.exe")
            command: str | list[str] = f'"{comspec}" /d /s /c ""{root / STARTER}""'
        else:
            command = ["sh", str(root / STARTER)]
        proc = subprocess.run(
            command,
            cwd=tmp_path,
            env=env,
            stdin=subprocess.DEVNULL,  # the failure path waits for a key
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
        lines = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
        return proc.returncode, [line.strip() for line in lines]

    return _start


def test_ready_starts_at_once(start):
    assert start(check_exit=0) == (0, [CHECK, START])


def test_first_start_runs_setup_then_checks_again(start):
    assert start(check_exit=4) == (0, [CHECK, SETUP, CHECK, START])


def test_setup_that_leaves_something_missing_does_not_start(start):
    code, calls = start(check_exit=4, setup_works=False)
    assert code == 1 and calls == [CHECK, SETUP, CHECK]
