"""Where the plugin keeps its state (config, model, FFmpeg) and the code it runs from.

The state root is ``VIDEO_STORY_HOME``, else the code root when it holds ``config.json``
(a development checkout), else ``~/.video-story``. The model and FFmpeg are looked for
in the state root first and then in the code root; downloads go to the state root.
Every test runs with its own ``VIDEO_STORY_HOME`` (``tests/conftest.py``); the tests of
the other two rules remove it and point the home folder into ``tmp_path``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from video_story.core import config as cfg
from video_story.host import cli, mcp_server

MODEL = cfg.MODEL_FILE_NAME


@pytest.fixture
def code(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A code root without ``config.json`` (as a plugin installed from a cache)."""
    root = tmp_path / "code"
    root.mkdir()
    (root / "pyproject.toml").write_text("# stand-in\n", encoding="utf-8")
    monkeypatch.setenv(cfg.CODE_ROOT_ENV, str(root))
    return root


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fake home folder and no ``VIDEO_STORY_HOME``."""
    folder = tmp_path / "home"
    folder.mkdir()
    monkeypatch.setenv("USERPROFILE", str(folder))  # Path.home() on Windows
    monkeypatch.setenv("HOME", str(folder))  # Path.home() elsewhere
    monkeypatch.delenv(cfg.STATE_HOME_ENV, raising=False)
    assert Path.home() == folder
    return folder


def _write_model(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"model")
    return path


# Resolution order -----------------------------------------------------------------


def test_environment_variable_wins(code: Path, home: Path, tmp_path: Path, monkeypatch):
    (code / "config.json").write_text("{}", encoding="utf-8")  # development would apply
    monkeypatch.setenv(cfg.STATE_HOME_ENV, str(tmp_path / "state"))
    root = cfg.resolve_state_root()
    assert root.path == tmp_path / "state" and root.mode == "environment"
    assert root.reason == "VIDEO_STORY_HOME is set"
    assert cfg.config_path() == tmp_path / "state" / "config.json"
    assert cfg.default_model_path() == tmp_path / "state" / "models" / MODEL


def test_development_checkout_keeps_its_state(code: Path, home: Path):
    (code / "config.json").write_text("{}", encoding="utf-8")
    root = cfg.resolve_state_root()
    assert root.path == code and root.mode == "development"
    assert root.reason.startswith("development")
    assert cfg.config_path() == code / "config.json"
    assert cfg.default_model_path() == code / "models" / MODEL
    assert cfg.model_search_paths() == [code / "models" / MODEL], "one root: one place"
    assert not (home / ".video-story").exists()


def test_user_folder_without_a_development_config(code: Path, home: Path):
    root = cfg.resolve_state_root()
    assert root.path == home / ".video-story" and root.mode == "user"
    assert root.reason == "user folder"
    assert cfg.config_path() == home / ".video-story" / "config.json"
    # Reading never creates anything.
    assert cfg.load_config() == cfg.Config()
    assert not (home / ".video-story").exists()
    # The first save creates the folder; nothing is written next to the code.
    cfg.update_config(capture_interval_ms=250)
    assert cfg.load_config(home / ".video-story" / "config.json").capture_interval_ms == 250
    assert sorted(p.name for p in code.iterdir()) == ["pyproject.toml"]


def test_video_story_root_is_only_the_code_root(code: Path, tmp_path: Path, monkeypatch):
    monkeypatch.setenv(cfg.STATE_HOME_ENV, str(tmp_path / "state"))
    assert cfg.code_root() == code
    assert cfg.state_root() == tmp_path / "state"
    assert cfg.config_path().parent != code


def test_installed_package_uses_the_user_folder(home: Path, tmp_path: Path, monkeypatch):
    """A wheel (not a checkout): the code root is inside site-packages, without config."""
    monkeypatch.delenv(cfg.CODE_ROOT_ENV, raising=False)
    site = tmp_path / "venv" / "Lib" / "site-packages"
    monkeypatch.setattr(cfg, "__file__", str(site / "video_story" / "core" / "config.py"))
    assert cfg.code_root() == tmp_path / "venv" / "Lib"
    assert cfg.resolve_state_root().mode == "user"
    assert cfg.config_path() == home / ".video-story" / "config.json"


# Model lookup -----------------------------------------------------------------------


def test_model_in_the_state_root_then_the_code_root(code: Path, tmp_path: Path, monkeypatch):
    state = tmp_path / "state"
    monkeypatch.setenv(cfg.STATE_HOME_ENV, str(state))
    assert cfg.model_search_paths() == [state / "models" / MODEL, code / "models" / MODEL]
    # Nothing anywhere: the download target in the state root.
    assert cfg.Config().resolved_model_path() == state / "models" / MODEL
    bundled = _write_model(code / "models" / MODEL)
    assert cfg.Config().resolved_model_path() == bundled
    downloaded = _write_model(state / "models" / MODEL)
    assert cfg.Config().resolved_model_path() == downloaded
    explicit = _write_model(tmp_path / "elsewhere" / "model.bin")
    assert cfg.Config(whisper_model_path=explicit).resolved_model_path() == explicit


# Unwritable state folder ----------------------------------------------------------


@pytest.fixture
def blocked_state(tmp_path: Path, monkeypatch) -> Path:
    """``VIDEO_STORY_HOME`` below a plain file: it can never be created."""
    blocker = tmp_path / "a-file"
    blocker.write_text("x", encoding="utf-8")
    state = blocker / "state"
    monkeypatch.setenv(cfg.STATE_HOME_ENV, str(state))
    return state


def test_unwritable_state_folder_fails_clearly(blocked_state: Path, capsys):
    with pytest.raises(cfg.StateFolderError, match="VIDEO_STORY_HOME"):
        cfg.ensure_state_root()
    with pytest.raises(cfg.StateFolderError, match="cannot write the configuration"):
        cfg.save_config(cfg.Config())
    assert cli.main(["config", "set", "capture_interval_ms", "250"]) == cli.EXIT_ERROR
    captured = capsys.readouterr()
    assert captured.out == ""
    error = json.loads(captured.err.strip().splitlines()[-1])
    assert error["ok"] is False and error["code"] == "STATE_NOT_WRITABLE"
    assert str(blocked_state / "config.json") in error["message"]
    # The MCP server reports it the same way.
    result = mcp_server.tool_config("set", "capture_interval_ms", "250")
    assert result["ok"] is False and result["code"] == "STATE_NOT_WRITABLE"


# What `config path` and `setup --check` print -------------------------------------


def test_config_path_tells_where_and_why(tmp_path: Path, capsys):
    assert cli.main(["config", "path"]) == 0
    out = json.loads(capsys.readouterr().out)
    state = cfg.state_root()
    assert out == {
        "ok": True,
        "path": str(state / "config.json"),
        "exists": False,
        "source": "state folder",
        "state_root": str(state),
        "state_reason": "VIDEO_STORY_HOME is set",
    }
    explicit = tmp_path / "other.json"
    assert cli.main(["--config", str(explicit), "config", "path"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["path"] == str(explicit) and out["source"] == "--config"
    assert out["state_root"] == str(state)


def test_config_set_and_get_round_trip_in_the_state_folder(capsys):
    assert cli.main(["config", "set", "capture_interval_ms", "250"]) == 0
    written = cfg.state_root() / "config.json"
    assert json.loads(capsys.readouterr().out)["path"] == str(written)
    assert cli.main(["config", "get", "capture_interval_ms"]) == 0
    assert json.loads(capsys.readouterr().out)["value"] == 250
    assert json.loads(written.read_text(encoding="utf-8"))["capture_interval_ms"] == 250


def test_setup_check(code: Path, tmp_path: Path, monkeypatch, capsys):
    from video_story.transcribe import ffmpeg as ff

    state = cfg.state_root()
    monkeypatch.setattr(ff.shutil, "which", lambda name: None)
    assert cli.main(["setup", "--check"]) == cli.EXIT_SETUP_NEEDED
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False and out["missing"] == ["model", "ffmpeg"]
    assert out["state_root"] == str(state) and "_exit_code" not in out
    assert not state.exists() or not any(state.iterdir()), "the check creates nothing"

    # A model and FFmpeg bundled with the code are enough.
    _write_model(code / "models" / MODEL)
    bundled = ff.bundled_ffmpeg_path(code)
    bundled.parent.mkdir(parents=True)
    bundled.write_bytes(b"")
    assert cli.main(["setup", "--check"]) == cli.EXIT_OK
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True and out["missing"] == []
    assert out["model"] == str(code / "models" / MODEL) and out["ffmpeg"] == str(bundled)
