# Installation

Video Story is a Python 3.12 application managed by [uv](https://docs.astral.sh/uv/).
Everything runs locally: the recorder, the FFmpeg conversion and the Whisper
transcription (whisper.cpp through `pywhispercpp`, CPU only).

## Requirements

| Component | Windows | Linux |
| --- | --- | --- |
| uv | `winget install astral-sh.uv` or the installer from docs.astral.sh | distribution package or the installer from docs.astral.sh |
| Python 3.12 | downloaded by uv automatically (`uv python install 3.12` if downloads are set to manual) | same |
| FFmpeg | optional on PATH; otherwise `video-story doctor --fix` downloads a static build into `bin/ffmpeg/` | same; distribution package recommended |
| Audio | any input device | PortAudio (`portaudio` package) and an input device (PipeWire/PulseAudio/ALSA) |
| Display | Windows 10/11, any number of screens, per-monitor DPI supported | X11 session (Wayland is not supported yet) with `python-xlib` (installed by uv) |

| macOS 13+ | | screens and cursor through CoreGraphics (no extra packages); on first use macOS asks for **Screen Recording** and **Microphone** permission for the terminal or host application; FFmpeg from Homebrew (`brew install ffmpeg`) or `video-story doctor --fix` is not available on macOS yet, so install it manually |

macOS is verified on the GitHub Actions macOS runner (CI), not yet on a physical Mac.

## 1. Get the code and the model

```bash
git clone <this repository> video-story
cd video-story
uv sync
```

The multilingual Whisper large-v3-turbo model, 5-bit quantised
(`ggml-large-v3-turbo-q5_0.bin`, 548 MB), belongs in `models/`. It is not part of the
repository. Either copy an existing file there or let the setup command download it
from Hugging Face (SHA-256
`394221709cd5ad1f40c46e6031ca61bce88931e6e088c188294c6d5a55ffa7e2` is verified):

```bash
uv run video-story setup
```

`setup` also downloads FFmpeg when it is not on PATH. `uv run video-story doctor`
reports every check (screens, audio input, FFmpeg, model, backend, recording folder)
without downloading anything.

## 2. Configuration

`config.json` lives in the repository root (it is ignored by Git). It is created with
defaults on first save. The Settings button of the control window edits it; the CLI can
too:

```bash
uv run video-story config get
uv run video-story config set capture_interval_ms 250
uv run video-story config set retention_days 90
uv run video-story config set image_format jpeg
uv run video-story config set audio_input "Microphone (Realtek(R) Audio)"
```

| Key | Default | Meaning |
| --- | --- | --- |
| `recording_folder` | `~/Videos/VideoStory` | where recordings are stored (`.work/` inside it holds recordings in progress) |
| `capture_interval_ms` | 500 | screenshot interval, 1 to 1000 ms |
| `image_format` | `jpeg` | `jpeg` or `png` |
| `jpeg_quality` | 70 | 20 to 100, used for JPEG only |
| `audio_input` | `null` | device name; `null` means the system default input |
| `auto_stop_minutes` | 120 | recording stops and is processed automatically after this time |
| `retention_days` | 30 | recordings older than this (by their start time) are deleted when the control window starts, 1 to 3650 |
| `export_preset` | `medium` | libx264 preset for MP4 export (`ultrafast` ... `slow`) |
| `export_crf` | 18 | libx264 quality (lower is better, 0 to 51) |
| `export_audio_bitrate_k` | 192 | AAC bitrate in kb/s |
| `editor_draw_cursor` | `true` | draw the recorded cursor into the preview and the exported video |
| `whisper_model_path` | `null` | `null` means `models/ggml-large-v3-turbo-q5_0.bin` |
| `whisper_backend` | `pywhispercpp` | or `whisper-cli` with `whisper_cli_path` pointing to a whisper.cpp binary |
| `whisper_threads` | min(32, CPUs) | CPU threads for transcription (large-v3-turbo transcribes roughly 2-3x faster than real time on a desktop CPU) |
| `ffmpeg_path` | `null` | explicit FFmpeg binary; `null` means PATH, then `bin/ffmpeg/` |

## 3. Register the skill with your agent

The skill is the directory `skills/video-story` (a `SKILL.md`, `agents/openai.yaml`
and the launcher scripts). Registering it means making that directory visible to the
host. `scripts/install-hosts.ps1` (Windows) and `scripts/install-hosts.sh` (Linux)
create the links described below.

There are two skills: `video-story` (recording) and `video-story-edit` (the editor and
MP4 export). Register both the same way; the install scripts do.

### Claude Code

Personal skills live in `~/.claude/skills/<name>/`. Link the skill directories there:

```powershell
# Windows (junction, no administrator rights needed)
cmd /c mklink /J "$env:USERPROFILE\.claude\skills\video-story" "D:\git\video-story\skills\video-story"
cmd /c mklink /J "$env:USERPROFILE\.claude\skills\video-story-edit" "D:\git\video-story\skills\video-story-edit"
```

```bash
# Linux
ln -s ~/video-story/skills/video-story ~/.claude/skills/video-story
ln -s ~/video-story/skills/video-story-edit ~/.claude/skills/video-story-edit
```

Start a new Claude Code session and say "Start Video Story" or "Edit Video Story". The repository is also a
Claude Code plugin (`.claude-plugin/plugin.json`) and a one-plugin marketplace
(`.claude-plugin/marketplace.json`); installing it that way copies the repository into
the plugin cache, so prefer the link while the model and the virtual environment live
next to the code.

### Codex (CLI and app)

Codex discovers skills in `~/.codex/skills/`. Link the same directory:

```powershell
cmd /c mklink /J "$env:USERPROFILE\.codex\skills\video-story" "D:\git\video-story\skills\video-story"
```

```bash
ln -s ~/video-story/skills/video-story ~/.codex/skills/video-story
```

Open a new chat (or restart Codex) and ask for "$video-story" or "Start Video Story".

### Claude Desktop (MCP)

Claude Desktop has no shell, so it uses the MCP server. Add to
`claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "video-story": {
      "command": "uv",
      "args": ["run", "--project", "D:\\git\\video-story", "video-story", "mcp"]
    }
  }
}
```

Tools: `video_story_start`, `video_story_wait`, `video_story_status`,
`video_story_describe`, `video_story_get_transcript`, `video_story_timeline`, `video_story_list_frames`,
`video_story_get_frame`, `video_story_config`, `video_story_doctor`, `video_story_edit`,
`video_story_export`.

### ChatGPT

ChatGPT only connects to remote MCP servers (Developer Mode, Streamable HTTP). Run the
server over HTTP and expose it with a tunnel of your choice, then add the public URL as a
custom connector:

```bash
uv run video-story mcp --transport streamable-http --port 8765
# for example: ngrok http 8765  ->  https://<id>.ngrok.app/mcp
```

The window still opens on the machine running the server; the transcript and frames are
returned through the MCP tools.

The Settings button edits the first six keys (Recording Folder, Capture Interval, Image
Format, JPEG Quality, Audio Input, Auto Stop Recording, Keep Recordings) and offers
**Open Folder** (the recording folder in the file manager) and **Delete All Recordings**,
which asks for confirmation and then removes every recording except the open session.

## 4. Verify

Editor and export:

```bash
uv run video-story edit                                   # opens the Video Story Editor (newest recording selected)
uv run video-story export --session <id> --screen screen1 # MP4 + SRT without the window
uv run video-story edit --detach                          # agents: returns editor_session, then
uv run video-story wait --editor <editor_session>         # ... waits for the green Process button
```

```bash
uv run video-story doctor
uv run video-story run            # opens the window and waits; press Record, talk, Stop, Video Editor,
                                  # wait for the green transcription arrow to reach the end, Process
```

Recordings land in the recording folder as `<YYYYMMDD-HHMMSS>/` with `manifest.json`,
`audio.wav`, `transcript.txt`, `transcript.json` and `frames/`.

Developer checks:

```bash
uv run pytest tests/unit -q
VIDEO_STORY_INTEGRATION=1 uv run pytest tests/integration/test_recording.py tests/integration/test_transcription.py -q
```

## Troubleshooting

- `ALREADY_RUNNING`: a control window is open; finish or close it, or run
  `video-story sessions` to see it.
- `WINDOW_EXITED`: the window process ended; read `ui.log` (window output, stays in
  `<recording_folder>/.work/<session>/`) and `recording.log` (next to the frames).
- `PROCESSING_FAILED`: transcription or finalisation failed; the recorded files stay in
  `<recording_folder>/.work/<session>/` and `video-story transcribe <that folder>` can
  retry the transcription.
- No audio device: the Record button reports the error; choose another input in
  Settings or check the operating system's input devices.
- Linux: `XDG_SESSION_TYPE=wayland` is refused; log in to an X11 session. Transparent
  overlay windows need a compositor.
