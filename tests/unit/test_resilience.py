"""Recordings that survive trouble: a screen that cannot be grabbed, a disk that runs
full, an audio file that cannot be written, a window that dies before saving, and the
rule that only Process hands a recording to the agent."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import json  # noqa: E402
import queue  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
import wave  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402
from PIL import Image  # noqa: E402
from test_editor import NEWEST, OLDER, _wait_loaded, make_recording  # noqa: E402
from test_editor_process import _editing_session  # noqa: E402

from video_story.capture import recorder as recorder_mod  # noqa: E402
from video_story.capture.audio_recorder import AudioRecorder  # noqa: E402
from video_story.capture.recorder import (  # noqa: E402
    MIN_FREE_BYTES,
    LowDiskSpaceError,
    Recorder,
    StopReason,
)
from video_story.capture.screen_grabber import RawFrame  # noqa: E402
from video_story.core.config import Config  # noqa: E402
from video_story.core.manifest import read_manifest  # noqa: E402
from video_story.core.screens import RawScreen  # noqa: E402
from video_story.core.session import (  # noqa: E402
    SessionPaths,
    SessionState,
    Status,
    read_state,
    write_state,
)
from video_story.core.transcript import Segment, Transcript, write_transcript  # noqa: E402
from video_story.editor.window import EditorWindow  # noqa: E402
from video_story.host import editor_session as es  # noqa: E402
from video_story.host import recovery  # noqa: E402
from video_story.host.editor_launcher import processed_recordings  # noqa: E402
from video_story.host.runner import session_status  # noqa: E402
from video_story.platform.base import Platform, SupportStatus  # noqa: E402

DEAD_PID = 2**22 + 12345
SESSION = "20260101-120000"


class TwoScreens(Platform):
    name = "fake"

    def prepare_process(self) -> None:
        pass

    def support_status(self) -> SupportStatus:
        return SupportStatus(True)

    def list_screens(self):
        return [RawScreen(0, 0, 8, 6, primary=True), RawScreen(8, 0, 8, 6)]

    def cursor_position(self):
        return (3, 2)


class FlakyGrabber:
    """``failing`` holds the screen ids that cannot be grabbed right now."""

    failing: set[str] = set()
    closed = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def close(self) -> None:
        FlakyGrabber.closed += 1

    def grab(self, screen) -> RawFrame:
        if screen.id in FlakyGrabber.failing:
            raise OSError("BitBlt failed (secure desktop)")
        return RawFrame(screen.id, screen.width, screen.height, bytes(screen.width * 6 * 4))


@pytest.fixture
def recorder(tmp_path: Path, monkeypatch) -> Recorder:
    monkeypatch.setattr(recorder_mod, "ScreenGrabber", FlakyGrabber)
    FlakyGrabber.failing = set()
    FlakyGrabber.closed = 0
    cfg = Config(recording_folder=tmp_path, capture_interval_ms=10, image_format="png")
    return Recorder(cfg, TwoScreens(), SessionPaths(tmp_path, SESSION), record_audio=False)


def _wait(condition, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "condition not met in time"
        time.sleep(0.01)


def _frames(recorder: Recorder, screen_id: str) -> int:
    frames_dir = recorder.paths.frames_dir(recorder.paths.work_dir)
    return len(list(frames_dir.glob(f"*_{screen_id}.png")))


# Capture ----------------------------------------------------------------------


def test_a_screen_that_cannot_be_grabbed_does_not_end_the_recording(recorder: Recorder):
    recorder.start()
    _wait(lambda: recorder.frame_count >= 4)
    FlakyGrabber.failing = {"screen2"}  # e.g. one monitor went away
    before = _frames(recorder, "screen2")
    _wait(lambda: recorder.failed_grabs >= 5)
    _wait(lambda: _frames(recorder, "screen1") >= 8)  # the other screen carries on
    assert recorder.running
    FlakyGrabber.failing = {"screen1", "screen2"}  # lock screen: nothing can be grabbed
    stalled = recorder.frame_count
    _wait(lambda: FlakyGrabber.closed >= 1)  # the grabber is reopened after a while
    assert recorder.running and recorder.error is None
    assert recorder.frame_count <= stalled + 4  # only frames already on their way
    FlakyGrabber.failing = set()
    _wait(lambda: _frames(recorder, "screen2") > before + 2)  # capture resumes by itself
    summary = recorder.stop()
    assert summary.failed_grabs >= 5 and summary.stop_reason is None and summary.error is None
    manifest = summary.to_manifest(recorder.config, "fake")
    assert manifest.frames.failed_grabs == summary.failed_grabs
    assert manifest.to_dict()["frames"]["failed_grabs"] == summary.failed_grabs


def test_copied_text_waits_for_a_tick_that_is_stored(recorder: Recorder):
    """A tick without any frame stores nothing, so it must not consume the clipboard."""
    texts = ["copied while the screen was locked"]
    recorder.clipboard_provider = lambda: texts.pop(0) if texts else None
    FlakyGrabber.failing = {"screen1", "screen2"}
    recorder.start()
    _wait(lambda: recorder.failed_grabs >= 6)
    assert texts  # still waiting
    FlakyGrabber.failing = set()
    _wait(lambda: not texts)
    _wait(lambda: recorder.frame_count >= 2)
    recorder.stop()
    frames_dir = recorder.paths.frames_dir(recorder.paths.work_dir)
    stored = [
        json.loads(p.read_text(encoding="utf-8"))["clipboard"]
        for p in frames_dir.parent.joinpath("frames_info").glob("*.json")
    ]
    assert "copied while the screen was locked" in stored


# Disk space --------------------------------------------------------------------


def test_recording_does_not_start_below_one_gigabyte(recorder: Recorder, monkeypatch):
    assert MIN_FREE_BYTES == 1024**3
    monkeypatch.setattr(recorder_mod, "free_disk_bytes", lambda _path: MIN_FREE_BYTES - 1)
    with pytest.raises(LowDiskSpaceError, match="At least 1 GB of free space"):
        recorder.start()
    assert not recorder.running


def test_recording_stops_by_itself_when_the_disk_runs_low(recorder: Recorder, monkeypatch):
    free = {"bytes": 50 * MIN_FREE_BYTES}
    monkeypatch.setattr(recorder_mod, "free_disk_bytes", lambda _path: free["bytes"])
    monkeypatch.setattr(recorder_mod, "DISK_CHECK_INTERVAL_NS", 20_000_000)
    events: list[tuple[str, str]] = []
    recorder._on_event = lambda event, message: events.append((str(event), message))
    recorder.start()
    _wait(lambda: recorder.frame_count >= 4)
    assert recorder.running and not recorder.low_disk
    free["bytes"] = MIN_FREE_BYTES - 1
    _wait(lambda: not recorder.running)
    assert recorder.low_disk and recorder.stop_reason is StopReason.LOW_DISK
    assert not recorder.auto_stopped
    assert any(e == "low_disk" and "At least 1 GB" in m for e, m in events)
    summary = recorder.stop()
    assert summary.stop_reason == "low_disk" and summary.frame_count >= 4
    assert summary.to_manifest(recorder.config, "fake").to_dict()["stop_reason"] == "low_disk"


# Audio --------------------------------------------------------------------------


def test_audio_writer_that_cannot_write_reports_it_and_stop_returns(tmp_path: Path, monkeypatch):
    """Disk full while recording: the writer keeps emptying its queue, the error is
    visible, and stop() does not block on a queue nobody reads."""
    import numpy as np

    from video_story.capture import audio_recorder as audio_mod

    device = audio_mod.AudioDevice(
        index=0,
        name="fake mic",
        host_api="fake",
        default_samplerate=16000,
        max_input_channels=1,
        is_default=True,
    )
    monkeypatch.setattr(audio_mod, "resolve_input_device", lambda _name: device)
    audio = AudioRecorder(tmp_path / "audio.wav")
    audio._queue = queue.Queue(maxsize=4)

    class FullDisk:
        def writeframes(self, _data: bytes) -> None:
            raise OSError(28, "No space left on device")

        def close(self) -> None:
            pass

    audio._wave = FullDisk()  # type: ignore[assignment]
    audio._writer = threading.Thread(target=audio._write_loop, daemon=True)
    audio._writer.start()
    block = np.zeros((1024, 1), dtype=np.int16)
    for _ in range(40):  # ten times the queue size: a dead writer would fill it
        audio._callback(block, 1024, None, None)
        time.sleep(0.002)
    _wait(lambda: audio.error is not None)
    done = threading.Event()
    result: list = []
    threading.Thread(target=lambda: (result.append(audio.stop()), done.set()), daemon=True).start()
    assert done.wait(5), "stop() blocked"
    assert "No space left" in (result[0].error or "")


# Manifest from the first moment ----------------------------------------------------


def test_manifest_exists_while_recording_and_keeps_keypoints(recorder: Recorder, monkeypatch):
    recorder.start()
    path = recorder.paths.manifest_path(recorder.paths.work_dir)
    manifest = read_manifest(path)
    assert manifest.in_progress and manifest.finished_at is None
    assert [s.id for s in manifest.screens] == ["screen1", "screen2"]
    assert manifest.capture_interval_ms == 10 and manifest.keypoints == []
    _wait(lambda: recorder.frame_count >= 2)
    first = recorder.add_keypoint()
    assert first is not None and read_manifest(path).keypoints == [first]
    recorder.stop()


# Recovery ------------------------------------------------------------------------


def _left_behind(
    root: Path, status: Status = Status.RECORDING, pid: int = DEAD_PID
) -> SessionPaths:
    """What a window that died while recording leaves in ``.work``: frames, audio, the
    provisional manifest and a non-final ``state.json``."""
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(recorder_mod, "ScreenGrabber", FlakyGrabber)
    FlakyGrabber.failing = set()
    try:
        cfg = Config(recording_folder=root, capture_interval_ms=10, image_format="png")
        paths = SessionPaths(root, SESSION)
        rec = Recorder(cfg, TwoScreens(), paths, record_audio=False)
        rec.start()
        _wait(lambda: rec.frame_count >= 6)
        rec.add_keypoint()
        # The process "dies": threads end, nothing is saved.
        rec._stop_event.set()
        rec._capture_thread.join(5)
        rec._pool.shutdown(wait=True)
    finally:
        monkeypatch.undo()
    frames_dir = paths.frames_dir(paths.work_dir)
    last = sorted(frames_dir.glob("*_screen1.png"))[-1]
    for extra in ("1_500_screen1", "1_500_screen2"):  # make it longer than a second
        (frames_dir / f"{extra}.png").write_bytes(last.read_bytes())
        (paths.frames_info_dir(paths.work_dir) / f"{extra}.json").write_text(
            '{"time_ms": 1500}', encoding="utf-8"
        )
    with wave.open(str(paths.audio_path(paths.work_dir)), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(bytes(2 * 16000 * 2))  # two seconds
    write_state(
        paths.state_path,
        SessionState(session_id=SESSION, status=status, recording_folder=str(root), pid=pid),
    )
    return paths


def test_recording_of_a_dead_window_is_finished_and_listed(tmp_path: Path):
    paths = _left_behind(tmp_path)
    cfg = Config(recording_folder=tmp_path)
    assert processed_recordings(cfg) == []
    assert recovery.recover_orphaned_recordings(tmp_path) == [SESSION]
    final = paths.final_dir
    manifest = read_manifest(paths.manifest_path(final))
    assert not manifest.in_progress and manifest.stop_reason == "recovered"
    assert manifest.recorded_duration_ms == 2000  # the audio is longer than the last frame
    assert manifest.audio.duration_ms == 2000 and manifest.audio.sample_rate == 16000
    assert manifest.frames.count == len(list(paths.frames_dir(final).glob("*.png")))
    assert len(manifest.keypoints) == 1  # stored when it was inserted
    assert [s.id for s in manifest.screens] == ["screen1", "screen2"]
    assert any("Recovered" in note for note in manifest.notes)
    assert processed_recordings(cfg) == [SESSION]  # the editor lists it now
    # The agent is told what happened and never gets it as processed.
    data = session_status(paths)
    assert data["status"] == "failed" and data["final"] and data["crop"] is None
    assert "recovered" in data["message"] and data["recording_dir"] == str(final)
    assert recovery.recover_orphaned_recordings(tmp_path) == []  # nothing left to do


def test_recording_without_any_manifest_is_recovered_from_its_frames(tmp_path: Path):
    """Left by a version that wrote the manifest only at the end."""
    paths = _left_behind(tmp_path)
    paths.manifest_path(paths.work_dir).unlink()
    assert recovery.recover_orphaned_recordings(tmp_path) == [SESSION]
    manifest = read_manifest(paths.manifest_path(paths.final_dir))
    assert [(s.id, s.width, s.height) for s in manifest.screens] == [
        ("screen1", 8, 6),
        ("screen2", 8, 6),
    ]
    assert manifest.image_format == "png" and manifest.recorded_duration_ms == 2000
    assert manifest.capture_interval_ms >= 1
    with Image.open(next(paths.frames_dir(paths.final_dir).glob("*.png"))) as image:
        assert image.size == (8, 6)


def test_recovery_leaves_a_running_window_and_foreign_folders_alone(tmp_path: Path):
    paths = _left_behind(tmp_path, pid=os.getpid())  # its window is alive
    assert recovery.recover_orphaned_recordings(tmp_path) == []
    assert not paths.final_dir.exists() and paths.frames_dir(paths.work_dir).is_dir()
    # No state.json: not a window's session (`record --keep-work` leaves such folders).
    paths.state_path.unlink()
    assert recovery.recover_orphaned_recordings(tmp_path) == []
    assert not paths.final_dir.exists()


def test_recovery_ignores_leftovers_shorter_than_a_second(tmp_path: Path):
    paths = _left_behind(tmp_path)
    paths.audio_path(paths.work_dir).unlink()
    for folder in (paths.frames_dir(paths.work_dir), paths.frames_info_dir(paths.work_dir)):
        for path in folder.glob("1_500_*"):
            path.unlink()
    assert recovery.recover_orphaned_recordings(tmp_path) == []
    assert not paths.final_dir.exists()


def test_a_save_that_stopped_half_way_is_resumed(tmp_path: Path):
    """``finalize`` moved the audio, then a locked file stopped it: the next start
    continues the move instead of refusing the existing folder."""
    paths = _left_behind(tmp_path, status=Status.FAILED)
    with pytest.raises(FileExistsError):
        paths.final_dir.mkdir(parents=True)
        os.replace(paths.audio_dir(paths.work_dir), paths.audio_dir(paths.final_dir))
        paths.finalize()
    assert recovery.recover_orphaned_recordings(tmp_path) == [SESSION]
    final = paths.final_dir
    assert paths.audio_path(final).is_file() and (final / "manifest.json").is_file()
    assert paths.frames_dir(final).is_dir() and not paths.frames_dir(paths.work_dir).exists()
    assert paths.state_path.is_file()  # the session files stay in the work folder


def test_manifest_moves_last(tmp_path: Path):
    paths = _left_behind(tmp_path)
    moved: list[str] = []
    real_replace = os.replace

    def spy(src, dst):
        moved.append(Path(src).name)
        real_replace(src, dst)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(os, "replace", spy)
        paths.finalize()
    assert moved[-1] == "manifest.json" and "frames" in moved


# Only Process hands a recording to the agent ---------------------------------------


def _pending(folder: Path, session_id: str) -> SessionPaths:
    """A recording whose transcription has not finished."""
    paths = make_recording(folder, session_id, with_transcript=False)
    write_transcript(
        paths.transcript_txt_path(paths.final_dir),
        paths.transcript_json_path(paths.final_dir),
        Transcript("en", [Segment(0, 500, "so far")], complete=False, transcribed_ms=500),
    )
    return paths


def test_process_waits_for_the_transcript_of_the_awaited_recording(qtbot, tmp_path: Path):
    """The agent waits for the recording from the Stop dialog; pressing Process while a
    complete older one is shown must not finish that session with half a transcript."""
    make_recording(tmp_path, OLDER, first_text="older one")
    awaited = _pending(tmp_path, NEWEST)
    _editing_session(tmp_path, NEWEST)
    cfg = Config(recording_folder=tmp_path)
    editor_session = es.new_editor_session(cfg)
    window = EditorWindow(
        cfg, session_id=OLDER, editor_session=editor_session, recording_session=NEWEST
    )
    qtbot.addWidget(window)
    window.transcription.start = lambda _recording: False  # no worker in this test
    window.show()
    _wait_loaded(qtbot, window, OLDER)
    assert window.recording.transcript_complete and window.process_button.isEnabled()
    assert window.process() is None
    assert NEWEST in window.statusBar().currentMessage()
    assert read_state(awaited.state_path).status is Status.EDITING
    assert es.read_editor_event(editor_session)["event"] == "open"
    # Once the awaited transcript is complete, Process works as before.
    write_transcript(
        awaited.transcript_txt_path(awaited.final_dir),
        awaited.transcript_json_path(awaited.final_dir),
        Transcript("en", [Segment(0, 500, "so far"), Segment(600, 900, "done")]),
    )
    assert window.process() is not None
    assert read_state(awaited.state_path).status is Status.PROCESSED


def test_failed_transcription_can_be_retried_without_closing(qtbot, tmp_path: Path):
    _pending(tmp_path, NEWEST)
    session = _editing_session(tmp_path, NEWEST)
    window = EditorWindow(Config(recording_folder=tmp_path), recording_session=NEWEST)
    qtbot.addWidget(window)
    started: list[str] = []
    window.transcription.start = lambda recording: bool(started.append(recording.session_id))
    window.show()
    _wait_loaded(qtbot, window, NEWEST)
    assert started == [NEWEST] and window.retry_transcription_button.isHidden()
    window._on_transcription_failed(NEWEST, "whisper model not found")
    assert not window.retry_transcription_button.isHidden()
    assert "whisper model not found" in window.retry_transcription_button.toolTip()
    window.retry_transcription_button.click()
    assert started == [NEWEST, NEWEST]  # started again (it resumes where it stopped)
    assert window.retry_transcription_button.isHidden()
    assert read_state(session.state_path).status is Status.EDITING  # the agent still waits
