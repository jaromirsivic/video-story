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
Windows only. Hosts: Claude Code on Windows end to end (skills linked into
`~/.claude/skills`). Codex and Cursor are described below from their documentation and
have not been exercised yet ([tests/manual/hosts.md](../tests/manual/hosts.md) is the
checklist for that); Claude Desktop and ChatGPT are documented but not exercised either.

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

## 3. Register the skills with your agent

There are two skills, always registered together: `video-story` (recording) and
`video-story-edit` (the editor and MP4 export). Each is a directory under `skills/`
with a `SKILL.md`, `agents/openai.yaml` (Codex metadata) and the launcher scripts.
A host can get them in one of three ways:

1. **Skill links** (recommended for a checkout): link the two directories into the
   folder the host reads user skills from. The code stays where it is, a `git pull`
   updates every host at once. `scripts/install-hosts.ps1` (Windows, junctions) and
   `scripts/install-hosts.sh` (Linux, macOS, symbolic links) do it.
2. **Plugin**: the host copies the repository into its plugin cache.
3. **MCP server**: the alternative when a host cannot keep the window open when it is
   started by a command.

Use one way per host: a skill registered both as a link and as a plugin is listed
twice.

Status: Claude Code on Windows is verified end to end with skill links. Codex and Cursor
are written from their documentation (linked below) and not yet exercised; run
[tests/manual/hosts.md](../tests/manual/hosts.md) to check them.

### What the window needs from the host

The launcher (`run --detach`, `edit --detach`) starts the window as its own GUI process
that must keep running after the command has returned. Video Story writes outside the
agent's workspace (the recording folder, the state folder `~/.video-story`, the
virtual environment next to the code), and the first run needs the network because uv
creates the environment. Both skills tell the agent to run the launcher outside the
host's sandbox and to ask you for that permission instead of retrying inside it:

- **Codex** runs commands in a sandbox by default (`workspace-write`: writes only in the
  workspace, no network) and asks for approval to go beyond it
  ([Sandbox](https://developers.openai.com/codex/sandboxing),
  [Agent approvals & security](https://developers.openai.com/codex/agent-approvals-security)).
  On Windows its MXC sandbox stops "remaining child processes ... when the foreground
  command exits" and the legacy sandboxes "use a private desktop"
  ([Windows](https://learn.chatgpt.com/docs/windows/windows-sandbox)), so the window can
  only live when the command runs outside the sandbox: approve that request.
- **Cursor** sandboxes terminal commands on macOS and Linux and "will indicate when a
  command runs outside the sandbox and ask for your approval"
  ([Run Modes](https://cursor.com/docs/agent/security/run-modes)); the page describes
  no sandbox for Windows.
- **Claude Code** runs the command directly unless you enabled its sandbox.

If the window still does not appear or closes with the command, use the MCP server
(below) for that host.

### Skill links

| Host | Switch | Folder | Source |
| --- | --- | --- | --- |
| Claude Code | `-Claude` / `--claude` | `~/.claude/skills/` | [Claude Code skills](https://code.claude.com/docs/en/skills) ("Personal") |
| Codex (CLI, IDE extension, ChatGPT desktop app) | `-Codex` / `--codex` | `~/.agents/skills/` | [Build skills](https://learn.chatgpt.com/docs/build-skills) ("USER `$HOME/.agents/skills`") |
| Cursor (editor and `cursor-agent`) | `-Cursor` / `--cursor` | `~/.cursor/skills/`, only when needed | [Agent Skills](https://cursor.com/docs/skills) ("Skill directories") |

```powershell
# Windows, in the repository folder; no administrator rights needed
& .\scripts\install-hosts.ps1 -Claude -Codex -Cursor   # any combination of the hosts
& .\scripts\install-hosts.ps1 -Cursor -Remove          # removes only that host's links
```

```bash
scripts/install-hosts.sh --claude --codex --cursor
scripts/install-hosts.sh --cursor --remove
```

The scripts print every link they create, keep or remove, and running them again
changes nothing that is already right. They never delete a real folder, only links.

- **Codex** documents only `~/.agents/skills` for user skills; `~/.codex/skills`, where
  older versions read them, is no longer mentioned. "If two skills share the same
  `name`, Codex doesn't merge them; both can appear in skill selectors", so `-Codex`
  removes a link to Video Story left in `~/.codex/skills`, and `-Remove` removes both.
- **Cursor** reads `~/.cursor/skills` and `~/.agents/skills` and, "for compatibility",
  `~/.claude/skills` and `~/.codex/skills`. When one of those already links to this
  checkout (for example after `-Claude` or `-Codex`), `-Cursor` creates nothing, says
  where Cursor loads the skill from, and drops its own earlier link in
  `~/.cursor/skills` if there is one. With both Claude Code and Codex registered,
  Cursor finds the skill in two folders that lead to the same files; its documentation
  does not say how it lists such a pair (one of the manual checks).

By hand (Windows junctions, Linux and macOS symbolic links):

```powershell
$repo = (Get-Location).Path                   # run it in the repository folder
$dir = "$env:USERPROFILE\.claude\skills"      # Codex: .agents\skills, Cursor: .cursor\skills
New-Item -ItemType Directory -Force $dir | Out-Null
foreach ($s in 'video-story', 'video-story-edit') { cmd /c mklink /J "$dir\$s" "$repo\skills\$s" }
# remove a link with: cmd /c rmdir "$dir\video-story"   (never Remove-Item -Recurse)
```

```bash
dir=~/.claude/skills                          # Codex: ~/.agents/skills, Cursor: ~/.cursor/skills
mkdir -p "$dir"
for s in video-story video-story-edit; do ln -s ~/video-story/skills/$s "$dir/$s"; done
# remove a link with: rm "$dir/video-story"   (never rm -r)
```

Then start a new session and say "Start Video Story" or "Edit Video Story":

- Claude Code: a new session.
- Codex: a new chat; Codex "detects skill changes automatically", restart it if the skill
  does not appear. `/skills` lists them, `$video-story` invokes one explicitly.
- Cursor: a new Agent chat (restart Cursor if the skill is missing); the skills are
  listed in **Customize** and invoked explicitly with `/video-story`.

### Plugin

The repository is a plugin for all three hosts. It carries `.claude-plugin/plugin.json`
(Claude Code) and a root `plugin.json` in the [Agent Plugins](https://agent-plugins.org)
format, which Codex and Cursor read; the skills are found in `skills/`. It bundles no
MCP server on purpose: the skills drive the CLI, and a bundled server would start with
every session and offer a second set of tools. `.claude-plugin/marketplace.json` is a
one-plugin marketplace named `video-story-local`.

A plugin install copies the plugin folder, so install from a fresh clone, not from a
development checkout: that would copy `.venv` and `models/`, and a copied `config.json`
would keep the state in the copy (section 2), where the next update loses it. Settings, the model and FFmpeg live in `~/.video-story` and survive an update; the
virtual environment is created again next to every new copy, so the first run after an
install or update needs the network and permission to run outside the sandbox.

- **Claude Code**: `/plugin marketplace add <path to the clone>` (start a relative path
  with `./`), then `/plugin install video-story@video-story-local`
  ([Discover plugins](https://code.claude.com/docs/en/discover-plugins)).
- **Codex**: Codex reads "a legacy-compatible marketplace at
  `$REPO_ROOT/.claude-plugin/marketplace.json`", so no extra file is needed
  ([Package your plugin](https://developers.openai.com/plugins/build/plugins)). Add it
  with `codex plugin marketplace add <path to the clone>`; in the CLI open `/plugins`,
  pick the `video-story-local` tab and install `video-story`; in the ChatGPT desktop
  app restart it and install from **Plugins**. Then start a new chat. Codex installs
  the copy into `~/.codex/plugins/cache/video-story-local/video-story/local/`; the key
  for enabling or disabling it in `config.toml` is `[plugins."video-story@video-story-local"]`.
  The IDE extension does not support plugins: use skill links there.
- **Cursor**: put a copy into `~/.cursor/plugins/local/video-story` - Cursor "skips a
  symlink that points to a plugin repository elsewhere on disk", so clone it there:
  `git clone <repository> ~/.cursor/plugins/local/video-story`. Restart Cursor or run
  **Developer: Reload Window** and check that **Customize** lists both skills
  ([Plugins](https://cursor.com/docs/plugins), "Test plugins locally"; on Enterprise
  plans the admin setting **Allow Local Plugin Imports** must be on). **Customize**
  also has **Import from Disk**, which the page names but does not describe. The Cursor
  CLI loads a plugin folder for one run with `cursor-agent --plugin-dir <path>` (from
  `cursor-agent --help`, version 2026.06.26; not in the web documentation).

### MCP server

The MCP server (`video-story mcp`) offers the same functions as tools. The host starts
it itself, not as an agent command, so use it for a host that has no shell (Claude
Desktop) or whose sandbox does not let the launcher keep the window open. Add it only
where you need it; whether the window survives there is part of the manual check.

Claude Desktop, `claude_desktop_config.json`:

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

Codex, `~/.codex/config.toml` (or `.codex/config.toml` in a trusted project; the
desktop app, CLI and IDE extension share it -
[Model Context Protocol](https://developers.openai.com/codex/mcp)):

```toml
[mcp_servers.video-story]
command = "uv"
args = ["run", "--project", "C:\\path\\to\\video-story", "video-story", "mcp"]
tool_timeout_sec = 120
```

or `codex mcp add video-story -- uv run --project C:\path\to\video-story video-story mcp`
(then add `tool_timeout_sec`). Codex gives a tool 60 s by default, exactly as long as
`video_story_wait` waits by default; 120 s leaves room. `/mcp` in the CLI lists the
server.

Cursor, `~/.cursor/mcp.json` (or `.cursor/mcp.json` in a project -
[MCP](https://cursor.com/docs/mcp)); `cursor-agent` reads the same files:

```json
{
  "mcpServers": {
    "video-story": {
      "type": "stdio",
      "command": "uv",
      "args": ["run", "--project", "C:\\path\\to\\video-story", "video-story", "mcp"]
    }
  }
}
```

Cursor asks for approval before it uses an MCP tool unless the Run Mode allows it.
The install scripts print these snippets with the real path for the hosts you named.

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
- The window never appears, or `wait` reports `failed` soon after the `run --detach`
  command has returned (Codex, Cursor): the command most likely ran inside
  the host's sandbox. Approve running it outside the sandbox when the host asks, or use
  the MCP server for that host (section 3).
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
