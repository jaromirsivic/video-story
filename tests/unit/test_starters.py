"""Real manual starters delegate once and never install implicitly."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
WINDOWS = sys.platform == "win32"


@pytest.mark.parametrize("mode", ["", "edit"])
@pytest.mark.parametrize("exit_code", [0, 4, 22, 23])
def test_starter_returns_launcher_result_once(tmp_path, mode, exit_code):
    root = tmp_path / "repo with space"
    scripts = root / "skills/video-story/scripts"
    scripts.mkdir(parents=True)
    name = "start-video-story.bat" if WINDOWS else "start-video-story.sh"
    shutil.copyfile(REPO / name, root / name)
    log = tmp_path / "calls"
    if WINDOWS:
        (scripts / "video-story.bat").write_text(
            '@echo off\necho %*>>"%VS_TEST_LOG%"\n'
            'echo {"ok":false,"code":"TEST_RESULT"}\n'
            f"exit /b {exit_code}\n",
            encoding="ascii",
        )
        command = f'"{os.environ["ComSpec"]}" /d /s /c ""{root / name}" {mode}"'
    else:
        (scripts / "video-story.sh").write_text(
            '#!/bin/sh\nprintf "%s\\n" "$*" >> "$VS_TEST_LOG"\n'
            'printf \'%s\\n\' \'{"ok":false,"code":"TEST_RESULT"}\'\n'
            f"exit {exit_code}\n",
            encoding="ascii",
        )
        command = ["sh", str(root / name)] + ([mode] if mode else [])
    result = subprocess.run(
        command,
        cwd=tmp_path,
        env={**os.environ, "VS_TEST_LOG": str(log)},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == exit_code, result.stderr
    assert log.read_text().splitlines() == [f"{mode or 'run'} --detach"]
    assert json.loads(result.stdout)["code"] == "TEST_RESULT"
