# Video Story

Video Story is a plugin and skill for Claude Code, Codex and Claude Desktop (and ChatGPT through a remote MCP server). While you work, you narrate what you are doing or what you want done. Video Story records your microphone, takes periodic screenshots of every connected screen and tracks the cursor. When you stop, the audio is transcribed offline with whisper.cpp and the agent receives a timed transcript together with the frames, so it can connect what you said with what was on screen and where the cursor was.

- Windows and Linux (X11) are supported in this version. macOS support is planned.
- Everything runs locally: no audio or images leave the machine.
- The plugin UI and all messages are in English; the transcript keeps the language you spoke.

## How it works

1. Say "Start Video Story" to your agent. A small window stays on top of other windows.
2. Press **Record** and talk while you work. **Draw on Screen** opens a drawing layer (brush, rectangle, arrow, 16 colors) that is captured in the screenshots. Recording stops automatically after the configured time (30 minutes by default).
3. Press **Stop** and choose **Process Recording**, **Continue Recording** or **Discard Recording**. Recording continues until you decide.
4. The audio is transcribed on your CPU (Whisper small, automatic language detection). The window closes and the agent receives `transcript.txt`, `transcript.json`, `manifest.json` and `frames/` with one screenshot per screen and interval plus a sidecar JSON holding the cursor screen and position.

Recording layout (`<recording_folder>/<YYYYMMDD-HHMMSS>/`):

```text
manifest.json          screens, interval, format, audio, transcript summary
audio.wav              16-bit PCM mono, device sample rate
transcript.txt         [HH:MM:SS.mmm --> HH:MM:SS.mmm] text
transcript.json        {"language": "cs", "segments": [{"start_ms", "end_ms", "text"}]}
frames/0_250_screen1.png   screenshot of screen1 at 250 ms
frames/0_250_screen1.json  {"time_ms": 250, "screen": "screen1", "cursor": {"screen": "screen2", "x": 412, "y": 388}}
```

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
