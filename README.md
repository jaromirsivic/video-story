# Video Story

[![CI](https://github.com/jaromirsivic/video-story/actions/workflows/ci.yml/badge.svg)](https://github.com/jaromirsivic/video-story/actions/workflows/ci.yml)

Video Story is a plugin and skill for Claude Code, Codex and Claude Desktop (and ChatGPT through a remote MCP server). While you work, you narrate what you are doing or what you want done. Video Story records your microphone, takes periodic screenshots of every connected screen and tracks the cursor. When you stop, the audio is transcribed offline with whisper.cpp and the agent receives a timed transcript together with the frames, so it can connect what you said with what was on screen and where the cursor was.

- Windows, Linux (X11) and macOS are supported. Windows and Linux were verified on real machines; macOS is verified on the GitHub Actions macOS runner (screens, cursor, recording, drawing capture) and still needs a hands-on check on a Mac with a microphone and a Retina display.
- Everything runs locally: no audio or images leave the machine. The transcription model is Whisper large-v3-turbo q5_0 (whisper.cpp, 548 MB, downloaded by `video-story setup`).
- The plugin UI and all messages are in English; the transcript keeps the language you spoke.

## How it works

1. Say "Start Video Story" to your agent. A small window stays on top of other windows.
2. Press **Record** and talk while you work. **Draw on Screen** opens a drawing layer (rectangle by default, brush, arrow, 8 colors; the right mouse button always draws an arrow; `Esc` leaves, `Ctrl+Z` undoes, `Ctrl+Shift+C` clears) that is captured in the screenshots and described in each frame's sidecar JSON. Recording stops automatically after the configured time (2 hours by default).
3. Press **Stop** and choose **Video Editor** (the default: process, then crop the recording in the editor and press its green **Process** button so the agent acts only on that range), **Continue Recording** or **Discard Recording**. A stopped recording always goes through the editor. Recording continues until you decide.
4. The audio is transcribed on your CPU (Whisper large-v3-turbo, 5-bit quantised, automatic language detection). The window closes and the agent receives `transcript.txt`, `transcript.json`, `manifest.json` and `frames/` with one screenshot per screen and interval plus a sidecar JSON holding the cursor screen and position and the drawings visible at that moment.
5. Recordings older than the configured retention (30 days by default) are deleted when Video Story starts; **Settings** also offers **Open Folder** and **Delete All Recordings**.

Recording layout (`<recording_folder>/<YYYYMMDD-HHMMSS>/`):

```text
manifest.json          screens, interval, format, audio, transcript summary
audio.wav              16-bit PCM mono, device sample rate
crop.json              {"start_ms", "end_ms"} chosen in the editor (absent = whole recording)
transcript.txt         [HH:MM:SS.mmm --> HH:MM:SS.mmm] text
transcript.json        {"language": "cs", "segments": [{"start_ms", "end_ms", "text"}]}
frames/0_500_screen1.jpg   screenshot of screen1 at 500 ms (JPEG 70 % by default, PNG optional)
frames/0_500_screen1.json  {"time_ms": 500, "screen": "screen1", "cursor": {"screen": "screen2", "x": 412, "y": 388},
                            "drawings": [{"tool": "rectangle", "color": "#FF0000", "screen": "screen1",
                                          "from": {"screen": "screen1", "x": 300, "y": 450},
                                          "to": {"screen": "screen1", "x": 750, "y": 675}}]}
```

## Editing and exporting

Say "Edit Video Story" and the **Video Story Editor** opens: a list of the stored recordings on the left (newest selected), a preview with Play/Stop and subtitles under it, and a timeline with one video track per screen plus the audio track. Zoom the timeline, drag the In/Out handles (or press `I`/`O`) to crop the start and the end (the crop is remembered per recording in `crop.json`), click a screen track to choose what is exported and press **Render**: the selected screen becomes an MP4 (H.264 + AAC, constant 24 fps, screen resolution, cursor drawn in) with an SRT subtitle file next to it. The green **Process** button instead hands the cropped range to the agent that opened the editor (all screens), which then acts only on that part of the recording. The mouse wheel zooms the preview up to 800 % around the cursor. `video-story export --session <id> --screen screen2 --start-ms 10000 --end-ms 60000` does the same without the window. The editor is verified on Windows; Linux and macOS follow.

## Quick start

```bash
uv sync
uv run video-story setup      # verifies FFmpeg and the whisper model, downloads them when missing
uv run video-story doctor     # environment report
uv run video-story run        # opens the control window and waits for the result
```

Agents use `run --detach`, then `wait --session <id>` until the session is final, then `frames --session <id> --at <ms>` to fetch the screenshot that matches a sentence. See [skills/video-story/SKILL.md](skills/video-story/SKILL.md) for the agent workflow and [docs/installation.md](docs/installation.md) for setup and host registration (Claude Code, Codex, Claude Desktop, ChatGPT).

## Development

```bash
uv run pytest tests/unit -q                                   # fast, offscreen
VIDEO_STORY_INTEGRATION=1 uv run pytest tests/integration -q  # real screens, microphone and model
uv run ruff check src tests && uv run ruff format src tests
```

The code is split into `core` (pure logic), `capture` (recording threads), `platform` (the only OS specific code), `transcribe`, `ui` (PySide6) and `host` (CLI, MCP server, session controller). The design document is [implementation-plan.md](implementation-plan.md) (in Czech).

## License

MIT. The Whisper model and whisper.cpp are MIT licensed; FFmpeg builds carry their own licenses (see `bin/ffmpeg/*/SOURCE.json` when downloaded).
