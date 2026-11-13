import hashlib
from pathlib import Path

import pytest

from video_story.core.config import Config
from video_story.host import setup
from video_story.host.editor_lock import EditorLock
from video_story.host.errors import HostError


def test_setup_repairs_only_missing_and_reports_progress(tmp_path, monkeypatch):
    model = tmp_path / "state/models/m.bin"
    ffmpeg = tmp_path / "ffmpeg"
    ffmpeg.write_bytes(b"ok")
    monkeypatch.setattr(setup, "ensure_state_root", lambda: tmp_path / "state")
    monkeypatch.setattr(Config, "resolved_model_path", lambda self: model)
    monkeypatch.setattr(setup, "default_model_path", lambda: model)
    monkeypatch.setattr(setup, "MODEL_SHA256", hashlib.sha256(b"model").hexdigest())
    monkeypatch.setattr(setup, "find_ffmpeg", lambda cfg: ffmpeg)
    monkeypatch.setattr(setup, "ffmpeg_usable", lambda p: True)
    calls = []

    def download(url, path, **kwargs):
        calls.append(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"model")
        kwargs["progress"](5, 5)

    monkeypatch.setattr(setup, "download_file", download)
    monkeypatch.setattr(setup, "download_ffmpeg", lambda *a, **k: pytest.fail("unneeded download"))
    assert setup.install_components(Config())["ok"]
    assert setup.install_components(Config())["ok"]
    assert calls == [model]


def test_custom_model_preserved_and_setup_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(setup, "ensure_state_root", lambda: tmp_path)
    model = tmp_path / "custom.bin"
    model.write_bytes(b"not the default model")
    monkeypatch.setattr(setup, "find_ffmpeg", lambda cfg: Path("ffmpeg"))
    monkeypatch.setattr(setup, "ffmpeg_usable", lambda p: True)
    monkeypatch.setattr(setup, "download_file", lambda *a, **k: pytest.fail("custom overwrite"))
    result = setup.install_components(Config(whisper_model_path=model))
    assert result["ok"] and model.read_bytes() == b"not the default model"
    with EditorLock(tmp_path / "setup.lock"):
        with pytest.raises(HostError) as exc:
            setup.install_components(Config())
        assert exc.value.code == "SETUP_IN_PROGRESS"


def test_bad_path_ffmpeg_is_replaced_in_config_not_system(tmp_path, monkeypatch):
    monkeypatch.setattr(setup, "ensure_state_root", lambda: tmp_path)
    model = tmp_path / "custom.bin"
    model.write_bytes(b"custom")
    original = tmp_path / "system-ffmpeg"
    original.write_bytes(b"bad")
    new = tmp_path / "local-ffmpeg"
    new.write_bytes(b"good")
    monkeypatch.setattr(setup, "find_ffmpeg", lambda cfg: cfg.ffmpeg_path or original)
    monkeypatch.setattr(setup, "ffmpeg_usable", lambda p: p == new)
    monkeypatch.setattr(setup, "download_ffmpeg", lambda *a, **k: new)
    config_file = tmp_path / "config.json"
    assert setup.install_components(Config(whisper_model_path=model), config_file)["ok"]
    assert original.read_bytes() == b"bad"
    assert "local-ffmpeg" in config_file.read_text()
