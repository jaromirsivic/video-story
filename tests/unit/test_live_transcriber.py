"""The recording window's side of the live transcription (with fake workers)."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

from video_story.core.config import Config, config_from_dict
from video_story.core.session import SessionPaths
from video_story.core.transcript import read_transcript
from video_story.host import live_transcription as lt
from video_story.host.live_transcription import LiveTranscriber, live_transcription_available


def _script(events: list[dict], *, then: str = "wait", exit_code: int = 0) -> str:
    lines = "\n".join(f"print({json.dumps(json.dumps(e))}, flush=True)" for e in events)
    tail = {
        "wait": (
            "import sys\nfor line in sys.stdin:\n"
            "    print(json.dumps({'cmd': line.strip()}), flush=True)\n"
        ),
        "exit": f"raise SystemExit({exit_code})\n",
    }[then]
    return "import json\n" + lines + "\n" + tail


def fake(events: list[dict], **kwargs) -> lt.CommandFactory:
    script = _script(events, **kwargs)

    def command(_paths: SessionPaths, _mode: str) -> list[str]:
        return [sys.executable, "-c", script]

    return command


def committed(start: int, end: int, texts: list[str], speech: bool = True) -> dict:
    step = (end - start) // max(1, len(texts))
    return {
        "event": "committed",
        "segments": [
            {"start_ms": start + i * step, "end_ms": start + (i + 1) * step, "text": t}
            for i, t in enumerate(texts)
        ],
        "transcribed_ms": end,
        "language": "cs",
        "start_ms": start,
        "end_ms": end,
        "cut": "pause",
        "speech": speech,
        "seconds": 5.0,
        "threads": 8,
    }


START = {"event": "start", "mode": "light", "threads": 8, "priority": "background", "from_ms": 0}


@pytest.fixture
def paths(tmp_path: Path) -> SessionPaths:
    p = SessionPaths(tmp_path / "rec", "20261111-120000")
    p.create_work_dirs()
    return p


def wait_for(condition, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.02)
    raise AssertionError("condition not reached")


def test_committed_windows_are_stored_as_an_incomplete_transcript(paths: SessionPaths) -> None:
    events = [START, {"event": "ready"}, committed(0, 25_000, ["Dobrý den.", "Jak se máte?"])]
    events.append(committed(25_000, 50_000, [], speech=False))
    live = LiveTranscriber(paths, Config(), command=fake(events))
    assert live.start()
    wait_for(lambda: live.transcribed_ms == 50_000)
    live.stop()
    transcript = read_transcript(paths.transcript_json_path(paths.work_dir))
    assert not transcript.complete
    assert transcript.transcribed_ms == 50_000
    assert transcript.language == "cs"
    assert [s.text for s in transcript.segments] == ["Dobrý den.", "Jak se máte?"]
    assert "Jak se máte?" in paths.transcript_txt_path(paths.work_dir).read_text("utf-8")
    info = live.info(60_000)
    assert info["windows"] == 2 and info["silent_windows"] == 1
    assert info["threads"] == 8 and info["priority"] == "background"
    assert info["transcribed_ms_at_stop"] == 50_000 and info["recorded_ms"] == 60_000
    assert info["rtf"] == 0.2 and info["error"] is None
    assert live.lag_ms(60_000) == 10_000
    assert not live.running


def test_a_crashed_worker_is_restarted_and_continues(paths: SessionPaths) -> None:
    command = fake([START, committed(0, 20_000, ["první"])], then="exit", exit_code=3)
    live = LiveTranscriber(paths, Config(), command=command, restart_delay_s=0.05)
    assert live.start()
    wait_for(lambda: live.error is not None)
    assert live.restarts == lt.MAX_RESTARTS
    assert "3 times" in live.error
    live.stop()
    transcript = read_transcript(paths.transcript_json_path(paths.work_dir))
    assert [s.text for s in transcript.segments] == ["první"]  # stored once


def test_a_worker_that_cannot_start(paths: SessionPaths) -> None:
    live = LiveTranscriber(
        paths, Config(), command=lambda _p, _m: [str(paths.work_dir / "missing.exe")]
    )
    assert not live.start()
    assert "cannot start" in (live.error or "")
    live.stop()


def test_nothing_is_written_after_stop(paths: SessionPaths) -> None:
    script = f"import json, time\nprint(json.dumps({START!r}), flush=True)\ntime.sleep(30)\n"
    live = LiveTranscriber(paths, Config(), command=lambda _p, _m: [sys.executable, "-c", script])
    assert live.start()
    wait_for(lambda: live.priority == "background")
    started = time.monotonic()
    live.stop()
    assert time.monotonic() - started < 5
    assert not paths.transcript_json_path(paths.work_dir).exists()
    live._handle(committed(0, 1000, ["pozdě"]))  # a late event is ignored
    assert not paths.transcript_json_path(paths.work_dir).exists()


def test_throttle_halves_the_threads_on_dropped_frames_and_restores_them(
    paths: SessionPaths,
) -> None:
    live = LiveTranscriber(paths, Config(), command=fake([START]))
    assert live.start()
    wait_for(lambda: live.threads == 8)
    live.observe_drops(0, now=100.0)
    live.observe_drops(1, now=101.0)
    assert live.threads == 8
    live.observe_drops(5, now=102.0)
    assert live.threads == 4 and live.throttled == 1
    live.observe_drops(5, now=150.0)
    assert live.threads == 4
    live.observe_drops(5, now=102.0 + lt.THROTTLE_RESTORE_S)
    assert live.threads == 8
    live.stop()


def test_resume_after_restart_reads_the_stored_transcript(paths: SessionPaths) -> None:
    first = LiveTranscriber(paths, Config(), command=fake([START, committed(0, 9_000, ["a"])]))
    first.start()
    wait_for(lambda: first.transcribed_ms == 9_000)
    first.stop()
    second = LiveTranscriber(
        paths, Config(), command=fake([START, committed(9_000, 20_000, ["b"])])
    )
    second.start()
    wait_for(lambda: second.transcribed_ms == 20_000)
    second.stop()
    transcript = read_transcript(paths.transcript_json_path(paths.work_dir))
    assert [s.text for s in transcript.segments] == ["a", "b"]


def test_availability(monkeypatch) -> None:
    import video_story.transcribe.pipeline as pipeline

    monkeypatch.setattr(pipeline, "missing_requirements", lambda _c: [])
    assert live_transcription_available(Config()) is None
    assert live_transcription_available(Config(live_transcription="off")) == "switched off"
    cli = Config(whisper_backend="whisper-cli")
    assert "pywhispercpp" in (live_transcription_available(cli) or "")
    monkeypatch.setattr(pipeline, "missing_requirements", lambda _c: ["FFmpeg"])
    assert live_transcription_available(Config()) == "missing FFmpeg"


def test_config_default_and_validation() -> None:
    assert Config().live_transcription == "light"
    assert config_from_dict({}).live_transcription == "light"
    assert config_from_dict({"live_transcription": "Faster"}).live_transcription == "faster"
    with pytest.raises(ValueError):
        config_from_dict({"live_transcription": "max"})


def test_worker_command_runs_the_hidden_cli_command(paths: SessionPaths, tmp_path: Path) -> None:
    command = lt.worker_command(tmp_path / "config.json")(paths, "faster")
    assert command[-4:] == ["transcribe-live", str(paths.work_dir), "--mode", "faster"]
    assert str(tmp_path / "config.json") in command
