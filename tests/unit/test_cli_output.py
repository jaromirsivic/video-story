"""The CLI writes UTF-8 whatever the stream encoding (a piped stdout on Windows is cp125x)."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys

from video_story.host import cli

# U+FFFC (object replacement character, found in window texts) and U+1F600 are not in
# cp1250; "ř" is, but must still come out as UTF-8.
TEXT = "ř \ufffc \U0001f600"


def test_emit_after_use_utf8_streams_writes_utf8(monkeypatch):
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp1250")
    monkeypatch.setattr(sys, "stdout", stream)
    cli.use_utf8_streams()
    cli.emit({"text": TEXT})
    assert json.loads(raw.getvalue().decode("utf-8"))["text"] == TEXT


def test_use_utf8_streams_tolerates_streams_without_reconfigure(monkeypatch):
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    cli.use_utf8_streams()
    cli.emit({"text": TEXT})
    assert json.loads(sys.stdout.getvalue())["text"] == TEXT


def _run_cli(*args: str) -> subprocess.CompletedProcess[bytes]:
    env = dict(os.environ, PYTHONIOENCODING="cp1250")
    return subprocess.run(
        [sys.executable, "-m", "video_story.host.cli", *args],
        capture_output=True,
        env=env,
        timeout=120,
        check=False,
    )


def test_piped_cp1250_stdout_gets_utf8_json(tmp_path):
    config_file = tmp_path / f"config {TEXT}.json"
    done = _run_cli("--config", str(config_file), "config", "path")
    assert done.returncode == 0, done.stderr.decode("utf-8", "replace")
    assert json.loads(done.stdout.decode("utf-8"))["path"] == str(config_file)


def test_piped_cp1250_stderr_gets_utf8_error(tmp_path):
    done = _run_cli("--config", str(tmp_path / "config.json"), "config", "get", TEXT)
    assert done.returncode == cli.EXIT_ERROR
    last = done.stderr.decode("utf-8").strip().splitlines()[-1]
    error = json.loads(last)
    assert error["code"] == "UNKNOWN_KEY"
    assert TEXT in error["message"]
