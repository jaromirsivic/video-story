"""Unit tests for ``run_doctor`` with every OS dependent piece monkeypatched."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from video_story.capture.audio_recorder import AudioDevice, AudioError
from video_story.core.config import Config
from video_story.core.screens import RawScreen
from video_story.host import doctor
from video_story.host.downloads import DownloadError
from video_story.platform.base import SupportStatus
from video_story.transcribe.ffmpeg import FfmpegNotFound


class FakePlatform:
    name = "fake-os"

    def __init__(self, supported: bool = True, screens: list[RawScreen] | None = None) -> None:
        self._status = SupportStatus(supported, "" if supported else "Wayland session")
        self._screens = screens if screens is not None else [RawScreen(0, 0, 1920, 1080, True)]
        self.prepared = False

    def prepare_process(self) -> None:
        self.prepared = True

    def support_status(self) -> SupportStatus:
        return self._status

    def list_screens(self) -> list[RawScreen]:
        return self._screens


DEVICES = [
    AudioDevice(3, "USB Mic", "WASAPI", 48000, 2, True),
    AudioDevice(5, "Webcam", "WASAPI", 44100, 1, False),
]


def _model_file(tmp_path: Path, content: bytes = b"model-bytes") -> Path:
    path = tmp_path / "models" / "ggml-small.bin"
    path.parent.mkdir(parents=True)
    path.write_bytes(content)
    return path


@pytest.fixture
def healthy(tmp_path: Path, monkeypatch) -> tuple[Config, FakePlatform]:
    platform = FakePlatform()
    model = _model_file(tmp_path)
    monkeypatch.setattr(doctor, "MODEL_SHA1", hashlib.sha1(b"model-bytes").hexdigest())
    monkeypatch.setattr(doctor, "get_platform", lambda: platform)
    monkeypatch.setattr(doctor, "list_input_devices", lambda: DEVICES)
    monkeypatch.setattr(doctor, "find_ffmpeg", lambda config: tmp_path / "ffmpeg")
    monkeypatch.setattr(doctor, "ffmpeg_version", lambda path: "7.1")
    monkeypatch.setattr(doctor, "config_path", lambda: tmp_path / "config.json")
    cfg = Config(recording_folder=tmp_path / "rec", whisper_model_path=model)
    return cfg, platform


def _by_name(report: dict) -> dict[str, dict]:
    return {c["name"]: c for c in report["checks"]}


def test_doctor_all_green(healthy, tmp_path: Path):
    cfg, platform = healthy
    report = doctor.run_doctor(cfg)
    checks = _by_name(report)
    assert report["ok"] is True and report["platform"] == "fake-os"
    assert set(checks) == {
        "python",
        "platform",
        "screens",
        "audio",
        "ffmpeg",
        "model",
        "backend",
        "recording_folder",
        "config",
    }
    assert all(c["ok"] for c in checks.values())
    assert set(checks["python"]) == {"name", "ok", "detail", "hint"}
    assert "screen1 1920x1080 (primary)" in checks["screens"]["detail"] and platform.prepared
    assert "USB Mic" in checks["audio"]["detail"]
    assert checks["ffmpeg"]["detail"].startswith("ffmpeg 7.1")
    assert "SHA-1 verified" in checks["model"]["detail"]
    assert report["ffmpeg"] == str(tmp_path / "ffmpeg")
    assert report["model"] == str(tmp_path / "models" / "ggml-small.bin")
    assert report["config_path"] == str(tmp_path / "config.json")
    assert (tmp_path / "rec").is_dir() and not (tmp_path / "rec" / ".write-test").exists()


def test_doctor_reports_missing_pieces(healthy, tmp_path: Path, monkeypatch):
    cfg, _ = healthy
    monkeypatch.setattr(doctor, "get_platform", lambda: FakePlatform(supported=False))
    monkeypatch.setattr(doctor, "list_input_devices", lambda: [])
    monkeypatch.setattr(doctor, "find_ffmpeg", lambda config: None)
    (tmp_path / "models" / "ggml-small.bin").write_bytes(b"corrupt")
    report = doctor.run_doctor(cfg)
    checks = _by_name(report)
    assert report["ok"] is False
    assert checks["platform"]["ok"] is False and "Wayland" in checks["platform"]["detail"]
    assert checks["screens"]["ok"] is True, "screens are still reported when possible"
    assert checks["audio"]["ok"] is False
    assert checks["ffmpeg"]["ok"] is False and "setup" in checks["ffmpeg"]["hint"]
    assert checks["model"]["ok"] is False and "SHA-1" in checks["model"]["detail"]
    assert "setup" in checks["model"]["hint"]
    assert report["ffmpeg"] is None and report["model"] is None


def test_doctor_audio_error_and_configured_device(healthy, monkeypatch):
    cfg, _ = healthy

    def broken():
        raise AudioError("PortAudio not initialised")

    monkeypatch.setattr(doctor, "list_input_devices", broken)
    assert _by_name(doctor.run_doctor(cfg))["audio"]["ok"] is False

    monkeypatch.setattr(doctor, "list_input_devices", lambda: DEVICES)
    check = _by_name(doctor.run_doctor(cfg.with_values(audio_input="Webcam")))["audio"]
    assert check["ok"] and "configured: Webcam" in check["detail"]
    check = _by_name(doctor.run_doctor(cfg.with_values(audio_input="Gone")))["audio"]
    assert check["ok"] and "not found" in check["detail"] and check["hint"]


def test_doctor_fix_downloads_ffmpeg_only_when_asked(healthy, tmp_path: Path, monkeypatch):
    cfg, _ = healthy
    monkeypatch.setattr(doctor, "find_ffmpeg", lambda config: None)
    calls: list[bool] = []

    def fake_ensure(config, download=False, progress=None):
        calls.append(download)
        return tmp_path / "bin" / "ffmpeg"

    monkeypatch.setattr(doctor, "ensure_ffmpeg", fake_ensure)
    assert _by_name(doctor.run_doctor(cfg))["ffmpeg"]["ok"] is False
    assert calls == []
    report = doctor.run_doctor(cfg, fix=True)
    assert calls == [True] and _by_name(report)["ffmpeg"]["ok"] is True
    assert report["ffmpeg"] == str(tmp_path / "bin" / "ffmpeg")

    def failing(config, download=False, progress=None):
        raise FfmpegNotFound("no build for this platform")

    monkeypatch.setattr(doctor, "ensure_ffmpeg", failing)
    check = _by_name(doctor.run_doctor(cfg, fix=True))["ffmpeg"]
    assert check["ok"] is False and "no build" in check["detail"]


def test_doctor_downloads_model_only_when_asked(healthy, tmp_path: Path, monkeypatch):
    cfg, _ = healthy
    model = tmp_path / "models" / "ggml-small.bin"
    model.unlink()
    downloads: list[tuple[str, Path, str | None]] = []

    def fake_download(url, dest, *, expected_sha1=None, **kwargs):
        downloads.append((url, dest, expected_sha1))
        dest.write_bytes(b"model-bytes")

    monkeypatch.setattr(doctor, "download_file", fake_download)
    assert _by_name(doctor.run_doctor(cfg, fix=True))["model"]["ok"] is False
    assert downloads == [], "fix alone never downloads the model"
    report = doctor.run_doctor(cfg, download_model=True)
    assert downloads == [(doctor.MODEL_URL, model, doctor.MODEL_SHA1)]
    assert _by_name(report)["model"]["ok"] is True and report["model"] == str(model)
    # A valid model is never re-downloaded.
    doctor.run_doctor(cfg, download_model=True)
    assert len(downloads) == 1

    model.unlink()

    def failing(url, dest, **kwargs):
        raise DownloadError("offline")

    monkeypatch.setattr(doctor, "download_file", failing)
    check = _by_name(doctor.run_doctor(cfg, download_model=True))["model"]
    assert check["ok"] is False and "offline" in check["detail"]


def test_doctor_backend_checks(healthy, tmp_path: Path, monkeypatch):
    cfg, _ = healthy
    assert "pywhispercpp" in _by_name(doctor.run_doctor(cfg))["backend"]["detail"]
    monkeypatch.setattr(doctor.importlib.util, "find_spec", lambda name: None)
    assert _by_name(doctor.run_doctor(cfg))["backend"]["ok"] is False

    cli_cfg = cfg.with_values(whisper_backend="whisper-cli")
    assert _by_name(doctor.run_doctor(cli_cfg))["backend"]["ok"] is False
    cli = tmp_path / "whisper-cli.exe"
    cli_cfg = cli_cfg.with_values(whisper_cli_path=str(cli))
    assert _by_name(doctor.run_doctor(cli_cfg))["backend"]["ok"] is False
    cli.write_bytes(b"")
    assert _by_name(doctor.run_doctor(cli_cfg))["backend"]["ok"] is True


def test_doctor_unwritable_recording_folder(healthy, tmp_path: Path):
    cfg, _ = healthy
    blocker = tmp_path / "file-not-dir"
    blocker.write_text("x", encoding="utf-8")
    check = _by_name(doctor.run_doctor(cfg.with_values(recording_folder=str(blocker))))
    assert check["recording_folder"]["ok"] is False and check["recording_folder"]["hint"]
