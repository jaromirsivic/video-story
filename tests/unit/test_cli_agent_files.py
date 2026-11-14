"""The commands an agent reads stay small whatever the length of the recording.

``timeline`` and ``mouse`` write their text to a file (``<recording_dir>/agent/`` or
``--out``) and repeat it in the JSON only when it is short; ``describe`` and ``sessions``
are bounded. Hosts cut long output (Claude Code at about 30 000 characters) and the
agent would act on a part without knowing.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from test_events import make_recording
from test_mouse_timeline import Mouse, search

from video_story.core import folders as fo
from video_story.core.archive_state import ArchiveState, ArchiveStatus, write_archive_state
from video_story.core.input_events import ElementInfo, HoverRecord
from video_story.core.session import SessionPaths, SessionState, Status, write_state
from video_story.core.transcript import Segment
from video_story.host import agent_output, cli

SESSION = "20260101-120000"  # the id test_events.make_recording uses
LONG_SPEECH = [Segment(i * 10, i * 10 + 9, f"sentence {i} " + "word " * 20) for i in range(300)]


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "rec"


@pytest.fixture
def run(tmp_path: Path, root: Path, capsys: pytest.CaptureFixture[str]):
    """Run a CLI command; returns its JSON and the number of characters it printed."""
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"recording_folder": str(root)}), encoding="utf-8")

    def call(*args: str) -> dict[str, Any]:
        capsys.readouterr()
        code = cli.main(["--config", str(config), "--log-level", "ERROR", *args])
        out = capsys.readouterr().out
        assert code == cli.EXIT_OK
        assert out.endswith("\n") and out.count("\n") == 1  # exactly one JSON object
        result = json.loads(out)
        result["_chars"] = len(out)
        return result

    return call


def short_recording(root: Path) -> SessionPaths:
    return make_recording(
        root,
        {2000: {"clipboard": "copied text"}},
        segments=[Segment(0, 900, "Look here."), Segment(1000, 2600, "And there.")],
    )


# timeline -----------------------------------------------------------------------


def test_a_short_timeline_is_inline_and_in_the_file(root: Path, run):
    paths = short_recording(root)
    result = run("timeline", "--session", SESSION)
    file = Path(result["timeline_file"])
    assert file == paths.final_dir / "agent" / "timeline.txt"
    raw = file.read_bytes()
    assert b"\r" not in raw and raw.decode("utf-8") == result["timeline"]
    assert result["timeline_inline"] is True and "timeline_note" not in result
    assert result["timeline_chars"] == len(result["timeline"])
    assert result["timeline_lines"] == result["timeline"].count("\n") == 4  # U+2028 is no break
    # Everything else the command returned before is still there.
    for key in ("session_id", "range", "crop", "duration_ms", "instructions", "language"):
        assert key in result
    for key in ("transcript_complete", "screens", "capture_interval_ms", "counts", "legend"):
        assert key in result
    assert result["mouse"] == {"recorded": False, "mode": "off"}


def test_a_long_timeline_is_only_in_the_file(root: Path, run):
    short_recording(root)
    everything = run("timeline", "--session", SESSION, "--inline-chars", "1000000")
    limit = everything["timeline_chars"]
    assert run("timeline", "--session", SESSION, "--inline-chars", str(limit))["timeline_inline"]

    result = run("timeline", "--session", SESSION, "--inline-chars", str(limit - 1))
    assert "timeline" not in result and result["timeline_inline"] is False
    assert "timeline_file" in result["timeline_note"]
    assert (result["timeline_lines"], result["timeline_chars"]) == (4, limit)
    text = Path(result["timeline_file"]).read_text(encoding="utf-8")
    assert text == everything["timeline"]  # the same lines as the inline string
    assert result["counts"] == everything["counts"]

    never = run("timeline", "--session", SESSION, "--inline-chars", "0")
    assert "timeline" not in never and never["timeline_inline"] is False


def test_the_output_does_not_grow_with_the_recording(root: Path, run):
    make_recording(root, {}, segments=LONG_SPEECH)
    timeline = run("timeline", "--session", SESSION)
    assert timeline["timeline_chars"] > 30_000 and "timeline" not in timeline
    assert timeline["_chars"] < 6_000
    lines = Path(timeline["timeline_file"]).read_text(encoding="utf-8").splitlines()
    assert len(lines) == timeline["timeline_lines"] == 301  # START and 300 sentences
    assert lines[1].startswith("0-9 SAY sentence 0 word")

    describe = run("describe", "--session", SESSION)
    assert describe["_chars"] < 6_000
    assert "transcript" not in describe and "transcript_in_range" not in describe
    assert describe["transcript_in_range_inline"] is False
    assert describe["transcript_in_range_lines"] == 300
    assert "transcript_txt" in describe["transcript_in_range_note"]
    assert Path(describe["transcript_txt"]).is_file()
    for key in ("recording_dir", "manifest_content", "crop", "instructions", "keypoints"):
        assert key in describe


def test_describe_keeps_a_short_transcript_inline(root: Path, run):
    short_recording(root)
    result = run("describe", "--session", SESSION)
    assert result["transcript_in_range_inline"] is True
    assert result["transcript_in_range"].count("\n") == 2 and "transcript" not in result
    assert "transcript_in_range_note" not in result
    cut = run("describe", "--session", SESSION, "--inline-chars", "20")
    assert "transcript_in_range" not in cut and "timeline" in cut["transcript_in_range_note"]


def test_timeline_out_writes_elsewhere(
    root: Path, run, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    paths = short_recording(root)
    target = tmp_path / "workspace" / "new folder" / "tl.txt"
    result = run("timeline", "--session", SESSION, "--out", str(target))
    assert Path(result["timeline_file"]) == target
    assert target.read_text(encoding="utf-8") == result["timeline"]
    assert not paths.agent_dir().exists()  # nothing written into the recording

    monkeypatch.chdir(tmp_path / "workspace")
    relative = run("timeline", "--session", SESSION, "--out", "rel.txt", "--inline-chars", "0")
    assert Path(relative["timeline_file"]) == tmp_path / "workspace" / "rel.txt"
    assert (tmp_path / "workspace" / "rel.txt").read_text(encoding="utf-8") == result["timeline"]
    folder = run("timeline", "--session", SESSION, "--out", str(tmp_path / "workspace"))
    assert Path(folder["timeline_file"]) == tmp_path / "workspace" / "timeline.txt"


def test_timeline_is_inline_when_the_file_cannot_be_written(
    root: Path, run, monkeypatch: pytest.MonkeyPatch
):
    paths = short_recording(root)

    def read_only(*_args, **_kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(agent_output, "atomic_write_text", read_only)
    result = run("timeline", "--session", SESSION, "--inline-chars", "0")
    assert result["ok"] is True and result["timeline_file"] is None
    assert result["timeline_inline"] is True and "1000-2600 SAY And there." in result["timeline"]
    assert "could not be written" in result["timeline_note"]
    assert not paths.agent_dir().exists()


# mouse --------------------------------------------------------------------------


def mouse_recording(root: Path) -> SessionPaths:
    paths = make_recording(root, {})
    m = Mouse(paths)
    for t in (1000, 2000, 3000):
        m.click(t, 20, 15, search(f"value {t}"))
    hover = m.writer.write(HoverRecord(t=1500, screen="screen1", x=70, y=70, gx=70, gy=70))
    m.writer.write_element(1510, hover, ElementInfo(role="button", name="Save"), win=m.win_notepad)
    m.close()
    return paths


def test_mouse_writes_json_lines_in_time_order(root: Path, run, tmp_path: Path):
    paths = mouse_recording(root)
    result = run("mouse", "--session", SESSION)
    file = Path(result["mouse_file"])
    assert file == paths.final_dir / "agent" / "mouse.jsonl"
    raw = file.read_bytes()
    assert b"\r" not in raw
    records = [json.loads(line) for line in raw.decode("utf-8").splitlines()]
    assert [(r["kind"], r["t"]) for r in records] == [
        ("click", 1000),
        ("rest", 1500),
        ("click", 2000),
        ("click", 3000),
    ]
    assert raw.decode("utf-8").splitlines()[0].startswith('{"kind": "click", "t": 1000,')
    assert (result["action_count"], result["rest_point_count"], result["mouse_lines"]) == (3, 1, 4)
    assert result["mouse_chars"] == len(raw.decode("utf-8"))
    # Short: the lists are inline too, as before.
    assert result["mouse_inline"] is True and "mouse_note" not in result
    assert result["actions"] == [r for r in records if r["kind"] != "rest"]
    assert result["rest_points"][0]["element"]["name"] == "Save"
    assert result["recorded"] is True and "params" in result and "range" in result

    cut = run("mouse", "--session", SESSION, "--inline-chars", "100")
    assert "actions" not in cut and "rest_points" not in cut and cut["mouse_inline"] is False
    assert "mouse_file" in cut["mouse_note"] and cut["action_count"] == 3
    assert cut["_chars"] < 1_500

    target = tmp_path / "ws" / "m.jsonl"
    out = run("mouse", "--session", SESSION, "--out", str(target), "--inline-chars", "0")
    assert Path(out["mouse_file"]) == target and target.read_bytes() == raw


def test_mouse_at_stays_inline_and_a_recording_without_mouse_writes_nothing(root: Path, run):
    paths = mouse_recording(root)
    one = run("mouse", "--session", SESSION, "--at", "2000", "--inline-chars", "0")
    assert [a["t"] for a in one["actions"]] == [2000]
    assert one["actions"][0]["element"]["content"]["text"] == "value 2000"
    assert "mouse_file" not in one and not paths.agent_dir().exists()

    paths.mouse_log_path(paths.final_dir).unlink()
    none = run("mouse", "--session", SESSION)
    assert none["recorded"] is False and none["actions"] == [] and "mouse_file" not in none
    assert not paths.agent_dir().exists()


def test_mouse_is_inline_when_the_file_cannot_be_written(
    root: Path, run, monkeypatch: pytest.MonkeyPatch
):
    mouse_recording(root)

    def read_only(*_args, **_kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(agent_output, "atomic_write_text", read_only)
    result = run("mouse", "--session", SESSION, "--inline-chars", "0")
    assert result["mouse_file"] is None and result["mouse_inline"] is True
    assert len(result["actions"]) == 3 and "could not be written" in result["mouse_note"]


# sessions -----------------------------------------------------------------------


def test_sessions_are_compact(root: Path, run):
    paths = short_recording(root)  # saved, without state.json: the manifest knows the times
    write_archive_state(paths, ArchiveState(status=ArchiveStatus.ARCHIVED), paths.final_dir)
    for i in range(24):
        sid = f"20260102-1200{i:02d}"
        failed = i == 23
        write_state(
            SessionPaths(root, sid).state_path,
            SessionState(
                session_id=sid,
                status=Status.FAILED if failed else Status.PROCESSED,
                recording_folder=str(root),
                pid=0,
                recording_started_at="2026-01-02T12:00:00.000+01:00",
                recorded_duration_ms=1_584_040,
                recording_dir=str(root / "default" / sid),
                manifest=str(root / "default" / sid / "manifest.json"),
                instructions="A long instruction text. " * 40,
                message="The control window process ended unexpectedly. " * (20 if failed else 1),
                error_code="WINDOW_EXITED" if failed else None,
            ),
        )

    result = run("sessions")
    assert result["_chars"] < 10_000
    assert (result["total"], result["truncated"], len(result["sessions"])) == (25, True, 20)
    assert "status --session" in result["note"]
    newest = result["sessions"][0]
    assert newest["session_id"] == "20260102-120023" and newest["status"] == "failed"
    assert newest["error_code"] == "WINDOW_EXITED" and len(newest["message"]) == 200
    entry = result["sessions"][1]
    assert set(entry) == {
        "session_id",
        "status",
        "folder",
        "recording_dir",
        "recording_started_at",
        "recorded_duration_ms",
        "updated_at",
        "archived",
    }
    assert entry["status"] == "processed" and entry["recorded_duration_ms"] == 1_584_040
    assert entry["archived"] is False and entry["folder"] is None  # only its work directory

    everything = run("sessions", "--limit", "100")
    assert (everything["truncated"], len(everything["sessions"])) == (False, 25)
    saved = everything["sessions"][-1]
    assert saved["session_id"] == SESSION and saved["folder"] == "default"
    assert saved["recording_dir"] == str(paths.final_dir) and saved["archived"] is True
    assert (saved["recording_started_at"], saved["updated_at"]) == ("x", "y")
    assert saved["recorded_duration_ms"] == 6000 and saved["status"] == "processed"
    # The full state is one command away.
    assert "state_file" in run("status", "--session", "20260102-120000")


# the agent/ folder is harmless ---------------------------------------------------


def test_the_agent_folder_moves_with_the_recording(root: Path, run):
    paths = short_recording(root)
    run("timeline", "--session", SESSION, "--inline-chars", "0")
    (root / "A").mkdir()
    moved = fo.move_recording(paths, "A")
    assert (moved.final_dir / "agent" / "timeline.txt").is_file()
    again = run("timeline", "--session", SESSION, "--inline-chars", "0")
    assert Path(again["timeline_file"]) == root / "A" / SESSION / "agent" / "timeline.txt"


def test_finalize_drops_agent_files_of_the_work_directory(tmp_path: Path):
    paths = SessionPaths(tmp_path, SESSION)
    paths.create_work_dirs()
    (paths.work_dir / "manifest.json").write_text("{}", encoding="utf-8")
    agent = paths.agent_dir(paths.work_dir)
    agent.mkdir()
    (agent / "timeline.txt").write_text("0 START\n", encoding="utf-8")
    final = paths.finalize()
    assert (final / "manifest.json").is_file()
    assert not (final / "agent").exists() and not agent.exists()
