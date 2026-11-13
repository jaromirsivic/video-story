# Installation

Video Story is a Python 3.12 application managed by [uv](https://docs.astral.sh/uv/).
Everything runs locally: the recorder, the FFmpeg conversion and the Whisper
transcription (whisper.cpp through `pywhispercpp`, CPU only).

## Requirements

| Component | Windows | Linux | macOS 13+ |
| --- | --- | --- | --- |
| uv | `winget install astral-sh.uv` or the installer from docs.astral.sh | distribution package or the installer from docs.astral.sh | installer from docs.astral.sh or Homebrew |
| Python 3.12 | downloaded by uv automatically (`uv python install 3.12` if downloads are set to manual) | same | same |
| FFmpeg | optional on PATH; otherwise `video-story doctor --fix` downloads a static build into `bin/ffmpeg/` of the state folder (see Configuration) | same; distribution package recommended | install it yourself (`brew install ffmpeg`); the automatic download is not available on macOS yet |
| Audio | any input device | PortAudio (`portaudio` package) and an input device (PipeWire/PulseAudio/ALSA) | any input device; macOS asks for **Microphone** permission for the terminal or host application |
| Display | Windows 10/11, any number of screens, per-monitor DPI supported | X11 session (Wayland is not supported yet) with `python-xlib` (installed by uv) | screens and cursor through CoreGraphics (no extra packages); macOS asks for **Screen Recording** permission |
| Disk | at least 1 GB free on the disk of the recording folder (Record refuses below that and a running recording stops there); expect about 3 GB per hour with two screens at the defaults | same | same |

What has been verified: recording on Windows 11 and on Fedora (X11); macOS on the GitHub
Actions runner only (not on a physical Mac, Retina untested); the editor window on
Windows only. Hosts: Claude Code on Windows end to end; Codex, Claude Desktop and ChatGPT
are documented below but not yet exercised.

## 1. Get the code and the model

```bash
git clone <this repository> video-story
cd video-story
uv sync
```

The multilingual Whisper large-v3-turbo model, 5-bit quantised
(`ggml-large-v3-turbo-q5_0.bin`, 548 MB), belongs in `models/` of the state folder
(see Configuration: `~/.video-story/models/`, or `models/` in the repository of a
development checkout). It is not part of the repository. Either copy an existing file
there or let the setup command download it from Hugging Face (SHA-256
`394221709cd5ad1f40c46e6031ca61bce88931e6e088c188294c6d5a55ffa7e2` is verified):

```bash
uv run video-story setup
```

`setup` also downloads FFmpeg when it is not on PATH. `uv run video-story doctor`
reports every check (screens, audio input, state folder, FFmpeg, model, backend,
recording folder) and where the model and FFmpeg were found, without downloading
anything. `uv run video-story setup --check` only says whether something is missing
(exit code 0: ready, 4: run `setup`); the starters use it.

## 2. Configuration

### Where Video Story keeps its files

Video Story keeps its own files - `config.json`, the model (`models/`) and a
downloaded FFmpeg (`bin/ffmpeg/<os>-<arch>/`) - in one **state folder**, separate from
the code, so an update of the plugin (a new copy in the plugin cache) keeps the
settings and does not download the model again. The state folder is, in this order:

1. the folder in the environment variable `VIDEO_STORY_HOME`, when it is set;
2. the repository itself, when `config.json` is there - a development checkout keeps
   everything next to the code as before, nothing is moved;
3. otherwise `~/.video-story` (`C:\Users\<you>\.video-story` on Windows), the same
   rule on Windows, Linux and macOS.

The model and FFmpeg are looked for in the state folder first and then next to the
code (`models/`, `bin/ffmpeg/` in the code folder: a distribution may bundle them);
`whisper_model_path`, `ffmpeg_path` and PATH keep their precedence. `setup` and
`doctor --fix` always download into the state folder; when it cannot be created or
written they fail with `STATE_NOT_WRITABLE` instead of writing elsewhere.
`uv run video-story config path` prints the `config.json` in use, the state folder and
why it was chosen; `--config <file>` overrides the file. Recordings are not part of the
state: they stay in `recording_folder`. The virtual environment (`.venv`) stays with
the code. `VIDEO_STORY_ROOT` only tells the launchers (and the program) where the code
is.

### Settings

`config.json` (in the state folder; ignored by Git in a checkout) is created with
defaults on first save. The Settings button of the control window edits the first seven
keys of the table below (Recording Folder, Capture Interval, Image Format, JPEG Quality,
Audio Input, Auto Stop Recording, Keep Recordings) and offers **Open Folder** (the
recording folder in the file manager) and **Delete All Recordings**, which asks for
confirmation and then removes every recording except the open session. A new Recording
Folder applies at once, also to the window that is open. The CLI edits every key:

```bash
uv run video-story config get
uv run video-story config set capture_interval_ms 250
uv run video-story config set archive_after_days 90
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
| `computer_audio` | `true` | also record what the computer plays (`audio/computer.wav`, WASAPI loopback of the default output; Windows only for now, ignored elsewhere) |
| `auto_stop_minutes` | 120 | recording stops by itself after this time; it is saved and opened in the editor like after Stop |
| `drawing_grid_px` | 16 | spacing of the grid that Ctrl snaps drawn shapes to (logical pixels; shown on the white Sketch canvas), 8 to 128 |
| `archive_after_days` | 30 | recordings older than this (by their start time) are archived when the editor starts (one MP4 per screen and one `.m4a` per audio track; frames and WAV audio deleted; no recording is ever deleted automatically), 0 to 3650; 0 = never archive automatically (**Never** in Settings; Archive Video in the editor still works) |
| `export_preset` | `medium` | libx264 preset for MP4 export (`ultrafast` ... `slow`) |
| `export_crf` | 18 | libx264 quality (lower is better, 0 to 51) |
| `export_audio_bitrate_k` | 192 | AAC bitrate in kb/s |
| `editor_draw_cursor` | `true` | draw the recorded cursor into the preview and the exported video |
| `whisper_model_path` | `null` | `null` means `models/ggml-large-v3-turbo-q5_0.bin` in the state folder, then in the code folder |
| `whisper_backend` | `pywhispercpp` | or `whisper-cli` with `whisper_cli_path` pointing to a whisper.cpp binary |
| `whisper_threads` | min(32, CPUs) | CPU threads for transcription (large-v3-turbo transcribes roughly 2-3x faster than real time on a desktop CPU) |
| `ffmpeg_path` | `null` | explicit FFmpeg binary; `null` means PATH, then `bin/ffmpeg/` in the state folder, then in the code folder |

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
# Windows (junction, no administrator rights needed); run it in the repository folder
$repo = (Get-Location).Path
cmd /c mklink /J "$env:USERPROFILE\.claude\skills\video-story" "$repo\skills\video-story"
cmd /c mklink /J "$env:USERPROFILE\.claude\skills\video-story-edit" "$repo\skills\video-story-edit"
```

```bash
# Linux
ln -s ~/video-story/skills/video-story ~/.claude/skills/video-story
ln -s ~/video-story/skills/video-story-edit ~/.claude/skills/video-story-edit
```

Start a new Claude Code session and say "Start Video Story" or "Edit Video Story". The repository is also a
Claude Code plugin (`.claude-plugin/plugin.json`) and a one-plugin marketplace
(`.claude-plugin/marketplace.json`); installing it that way copies the repository into
the plugin cache. Settings, the model and FFmpeg then live in `~/.video-story` and
survive an update; the virtual environment is created again next to the new copy.

### Codex (CLI and app)

Codex discovers skills in `~/.codex/skills/`. Link the same directory:

```powershell
$repo = (Get-Location).Path   # run it in the repository folder
cmd /c mklink /J "$env:USERPROFILE\.codex\skills\video-story" "$repo\skills\video-story"
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
      "args": ["run", "--project", "C:\\path\\to\\video-story", "video-story", "mcp"]
    }
  }
}
```

Tools: `video_story_start`, `video_story_wait`, `video_story_status`,
`video_story_describe`, `video_story_get_transcript`, `video_story_timeline`,
`video_story_mouse`, `video_story_list_frames`,
`video_story_get_frame`, `video_story_config`, `video_story_doctor`, `video_story_edit`,
`video_story_wait_editor`, `video_story_export`.

No tool result grows with the length of the recording. `video_story_timeline`,
`video_story_mouse` (without `at_ms`) and `video_story_get_transcript` return pages:
`first_line` (1-based, default 1) and `max_chars` (default 20000, 1000-100000) select
whole lines; the result carries the total (`timeline_lines`, `mouse_lines`,
`transcript_lines`) and `next_line` - the `first_line` of the next call, `null` when
the text is complete.

### ChatGPT

ChatGPT only connects to remote MCP servers (Developer Mode, Streamable HTTP). Run the
server over HTTP and expose it with a tunnel of your choice, then add the public URL as a
custom connector:

```bash
uv run video-story mcp --transport streamable-http --port 8765
# for example: ngrok http 8765  ->  https://<id>.ngrok.app/mcp
```

The window still opens on the machine running the server; the transcript and frames are
returned through the MCP tools, so they travel to ChatGPT. The HTTP server has no
authentication: whoever can reach the URL can read every stored recording and change the
configuration, so expose it only through a tunnel you control and close it afterwards.

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
`audio/audio.wav`, `transcript.txt`, `transcript.json`, `frames/` (images) and
`frames_info/` (one sidecar JSON per frame).

Developer checks:

```bash
uv run pytest tests/unit -q
VIDEO_STORY_INTEGRATION=1 uv run pytest tests/integration/test_recording.py tests/integration/test_transcription.py -q
```

## Troubleshooting

- `ROOT_NOT_FOUND` (launcher, exit code 21): `scripts/video-story.cmd` / `.sh` found no
  `pyproject.toml` three levels above itself, nor behind the junction or symlink the skill
  is registered with. Register the skill again (`scripts/install-hosts`) or set
  `VIDEO_STORY_ROOT` to the Video Story directory.
- `STATE_NOT_WRITABLE`: the state folder (`video-story config path` names it) cannot be
  created or written, so settings cannot be saved and `setup` cannot download. Make it
  writable or set `VIDEO_STORY_HOME` to a writable folder.
- `ALREADY_RUNNING`: a control window is open; finish or close it, or run
  `video-story sessions` to see it.
- `WINDOW_EXITED`: the window process ended; read `ui.log` (window output, stays in
  `<recording_folder>/.work/<session>/`) and `recording.log` (next to the frames). What
  was recorded is not lost: the next start of Video Story or of the editor finishes the
  recording (manifest from the frames and the audio) and lists it in the editor. It is
  never handed to the agent by itself.
- `PROCESSING_FAILED`: the recording could not be saved (a file was locked, the folder
  went away); the files stay in `<recording_folder>/.work/<session>/` and the next start
  finishes the save in the same way.
- "At least 1 GB of free space is needed": Record does not start, and a running
  recording stops, when less than 1 GB is left on the disk of the recording folder. Free
  some space or choose another Recording Folder in Settings.
- Transcription failed in the editor: press **Retry transcription** (it continues from
  the last subtitle); `video-story setup` downloads a missing model or FFmpeg.
- No audio device: the Record button reports the error; choose another input in
  Settings or check the operating system's input devices.
- Linux: `XDG_SESSION_TYPE=wayland` is refused; log in to an X11 session. Transparent
  overlay windows need a compositor.
