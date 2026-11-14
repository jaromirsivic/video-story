"""Preflight must stay read-only and independent of hardware / large model reads."""

import json
from pathlib import Path

import pytest

from video_story.core.config import Config
from video_story.host import cli, mcp_server, readiness
from video_story.host.errors import HostError


@pytest.fixture
def files(tmp_path, monkeypatch):
    monkeypatch.setenv("VIDEO_STORY_ROOT", str(tmp_path / "code"))
    monkeypatch.setattr(readiness, "MODEL_SIZE", 8)
    model = tmp_path / "model.bin"
    binary = tmp_path / "ffmpeg"
    binary.write_bytes(b"executable")
    binary.chmod(0o755)
    monkeypatch.setattr(readiness, "find_ffmpeg", lambda cfg: binary)
    monkeypatch.setattr(Config, "resolved_model_path", lambda self: model)
    return model, binary


@pytest.mark.parametrize(
    "content,reason",
    [(None, "missing"), (b"", "empty"), (b"short", "wrong_size"), (b"12345678", None)],
)
def test_model_checks_only_metadata(files, monkeypatch, content, reason):
    model, _ = files
    if content is not None:
        model.write_bytes(content)
    original_open = Path.open

    def check_open(path, *args, **kwargs):
        if path == model:
            pytest.fail("read model contents")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", check_open)
    result = readiness.check_readiness(Config())
    assert result["ok"] is (reason is None)
    assert result["problems"] == (
        [] if reason is None else [{"component": "model", "reason": reason, "path": str(model)}]
    )


def test_custom_model_and_aggregate_missing(files, monkeypatch):
    model, binary = files
    model.write_bytes(b"custom")
    cfg = Config(whisper_model_path=model)
    assert readiness.check_readiness(cfg)["ok"]
    binary.unlink()
    model.unlink()
    result = readiness.check_readiness(cfg)
    assert result["missing"] == ["model", "ffmpeg"]
    assert not list(Path(result["state_root"]).iterdir())


def test_cli_mcp_preserve_structured_errors(files, monkeypatch, capsys):
    def fail(*a, **k):
        return readiness.require_ready(Config())

    monkeypatch.setattr(cli, "cmd_run", fail)
    monkeypatch.setattr(mcp_server, "launch_control_window", fail)
    assert cli.main(["run", "--detach"]) == 4
    error = json.loads(capsys.readouterr().err)
    assert error == mcp_server.tool_start()
    assert error["code"] == "SETUP_REQUIRED" and error["missing"] == ["model"]
    with pytest.raises(HostError) as exc:
        readiness.require_ready(Config())
    assert exc.value.exit_code == 4
