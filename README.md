# Video Story

[![CI](https://github.com/jaromirsivic/video-story/actions/workflows/ci.yml/badge.svg)](https://github.com/jaromirsivic/video-story/actions/workflows/ci.yml)

Video Story is a plugin and skill for Claude Code, Codex and Claude Desktop (and ChatGPT through a remote MCP server). While you work, you narrate what you are doing or what you want done. Video Story records your microphone, takes periodic screenshots of every connected screen and tracks the cursor. When you stop, the audio is transcribed offline with whisper.cpp and the agent receives a timed transcript together with the frames, so it can connect what you said with what was on screen and where the cursor was.

- Windows, Linux (X11) and macOS are supported. Windows and Linux were verified on real machines; macOS is verified on the GitHub Actions macOS runner (screens, cursor, recording, drawing capture) and still needs a hands-on check on a Mac with a microphone and a Retina display.
- Everything runs locally: no audio or images leave the machine. The transcription model is Whisper large-v3-turbo q5_0 (whisper.cpp, 548 MB, downloaded by `video-story setup`).
- The plugin UI and all messages are in English; the transcript keeps the language you spoke.

## How it works

1. Say "Start Video Story" to your agent. A small window stays on top of other windows.
2. Press **Record** and talk while you work. **Draw on Screen** opens a drawing layer (rectangle by default, brush, arrow, 8 colors; the right mouse button always draws an arrow; `Esc` leaves, `Ctrl+Z` undoes, `Ctrl+Shift+C` clears) that is captured in the screenshots and described in each frame's sidecar JSON. **Insert keypoint** (it takes the place of Settings while recording) marks the current moment as important; the button counts down 3, 2, 1 before the next one. Text you copy while recording (Ctrl+C) lands once, in the sidecar of the next frame, so the agent knows what "this text" was. Recording stops automatically after the configured time (2 hours by default).
3. Press **Stop** and choose **Video Editor** (the default), **Continue Recording** or **Discard Recording**. A stopped recording always goes through the editor, which opens at once: the recording is saved and the control window closes without waiting for the transcription. Recording continues until you decide.
4. The audio is transcribed on your CPU (Whisper large-v3-turbo, 5-bit quantised, automatic language detection) in the background of the editor: a green arrow under the audio track shows how far the transcription got and the estimated time to the end (`transcribing (ETA: 2:05)`), subtitles appear as they are produced, and **Render** and **Process** stay disabled until it is complete. You can already watch the recording, move the crop handles, correct subtitles and type **Instructions** meanwhile: master instructions for the AI about the whole recording ("create an implementation plan from what I describe"), empty by default, stored as `instructions.txt`. Closing the editor early keeps what was transcribed; selecting the recording again continues where it stopped. Press the green **Process** button (or tick **Auto process**, which presses it five seconds after the transcription completes unless you click or type) and the agent receives the instructions, `transcript.txt`, `transcript.json`, `manifest.json` (with the keypoints), the crop and `frames/` with one screenshot per screen and interval plus a sidecar JSON holding the cursor screen and position, the finished drawings visible at that moment (each with the time it was drawn; a shape still being dragged is only announced in `drawing_now`, its geometry follows once the mouse button is released) and the text copied just before it. Hallucinated subtitle credits ("Titulky vytvořil ...", "Subtitles by ...") are dropped from the transcript.
5. Recordings older than the configured retention (30 days by default) are deleted when Video Story starts; **Settings** also offers **Open Folder** and **Delete All Recordings**.

Recording layout (`<recording_folder>/<YYYYMMDD-HHMMSS>/`):

```text
manifest.json          screens, interval, format, audio, transcript summary, keypoints [{"index", "time_ms"}]
audio.wav              16-bit PCM mono, device sample rate
crop.json              {"start_ms", "end_ms"} chosen in the editor (absent = whole recording)
instructions.txt       master instructions for the AI typed in the editor (absent = none)
transcript.txt         [HH:MM:SS.mmm --> HH:MM:SS.mmm] text
transcript.json        {"language": "cs", "complete": true, "segments": [{"start_ms", "end_ms", "text"}]}
                       (complete false + transcribed_ms while the editor is still transcribing)
frames/0_500_screen1.jpg   screenshot of screen1 at 500 ms (JPEG 70 % by default, PNG optional)
frames/0_500_screen1.json  {"time_ms": 500, "screen": "screen1", "cursor": {"screen": "screen2", "x": 412, "y": 388},
                            "drawings": [{"id": 1, "tool": "rectangle", "color": "#FF0000", "screen": "screen1",
                                          "stroke_width": 4, "started_ms": 120, "finished_ms": 410,
                                          "from": {"screen": "screen1", "x": 300, "y": 450},
                                          "to": {"screen": "screen1", "x": 750, "y": 675}}],
                            "drawing_now": null,  (while dragging: {"tool", "color", "screen", "started_ms"})
                            "zoom": [],
                            "clipboard": "text copied just before this frame (null in every other frame)"}
```

## Editing and exporting

Say "Edit Video Story" and the **Video Story Editor** opens: a list of the stored recordings on the left (newest selected; each row shows a thumbnail, the date, the length and what was said, with one dot per screen, up to four, to switch the thumbnail - hovering a dot names the screen and its size; move the mouse over a row's text to scrub through that recording, DaVinci Resolve style; right-click a row for **Open Folder Location** and **Delete Video**, or Shift/Ctrl-select several rows for **Delete Video(s)**, which deletes the recording folders for good after a confirmation), a preview with icon transport buttons and subtitles under it (up to five lines, then scrolling), and a timeline with one video track per screen plus the audio track (the toggle in its header stretches the waveform so that the loudest sound fills the track; the audio itself is unchanged). Zoom the timeline with -/+ (the playhead stays centred), drag the In/Out handles (or press `I`/`O`) to crop the start and the end (the crop is remembered per recording in `crop.json`; the handles snap to the green keypoint lines, marked by green arrows on the ruler, inserted while recording), click a screen track to choose what is exported. **Stop** returns to where **Play** was last pressed (a white arrow on the ruler). The bottom row holds **Auto process**, the green **Process** (hands the cropped range and the instructions to the agent that opened the editor, all screens, which then acts only on that part of the recording; Auto process presses it by itself five seconds after the transcript is complete unless you click or type), **Instructions** (master instructions for the AI about the whole recording; the templates **Draft Intent**, **Create Plan** and **Implement** fill in a formal brief, a plan or autonomous implementation) and the blue **Render**: the selected screen becomes an MP4 (H.264 + AAC, constant 24 fps, screen resolution, cursor drawn in) with an SRT subtitle file next to it. The **Edit** button corrects the words of a subtitle (its times stay as whisper set them); the button above it switches between the light and the dark theme. A recording that is still being transcribed shows a green arrow under its audio track with the estimated time to the end and keeps Render and Process disabled until the transcript is complete. The mouse wheel zooms the preview up to 800 % around the cursor. `video-story export --session <id> --screen screen2 --start-ms 10000 --end-ms 60000` does the same without the window. The editor is verified on Windows; Linux and macOS follow.

## Quick start

```bash
uv sync
uv run video-story setup      # verifies FFmpeg and the whisper model, downloads them when missing
uv run video-story doctor     # environment report
uv run video-story run        # opens the control window and waits for the result
```

### Starting without an agent

Video Story also works on its own, without Claude Code or another agent: record, then watch, crop, correct subtitles and render the recording in the editor. Start it from the repository root with:

| | Recording window | Editor with the stored recordings |
| --- | --- | --- |
| Windows | double-click `start-video-story.bat` | `start-video-story.bat edit` |
| Linux, macOS | `./start-video-story.sh` | `./start-video-story.sh edit` |

The starters need only [uv](https://docs.astral.sh/uv/) (Windows: `winget install astral-sh.uv`). The first start creates the Python environment and, when they are missing, downloads FFmpeg and the Whisper model (`video-story setup`, about 550 MB). Then the window opens in its own process and the console closes; when something goes wrong (uv missing, a window already open) the console stays open with the reason. On macOS, Finder opens `.sh` files in an editor: run the starter from Terminal, or make a copy named `start-video-story.command` to double-click it. Without an agent, **Process** (and **Auto process**) only closes the editor and keeps the recording; untick **Auto process** to keep the editor open once the transcript is complete.

Agents use `run --detach`, then `wait --session <id>` until the session is final. They read the user's instructions first, then `timeline --session <id>`: the cropped range as one text timeline (speech with the cursor position, finished drawings with their geometry and press/release times, removed shapes, copied text, cursor moves between screens, zoom, keypoints), so they decide from text which moments need a picture and fetch only those with `frames --session <id> --at <ms> --no-drawings`. See [skills/video-story/SKILL.md](skills/video-story/SKILL.md) for the agent workflow and [docs/installation.md](docs/installation.md) for setup and host registration (Claude Code, Codex, Claude Desktop, ChatGPT).

## Development

```bash
uv run pytest tests/unit -q                                   # fast, offscreen
VIDEO_STORY_INTEGRATION=1 uv run pytest tests/integration -q  # real screens, microphone and model
uv run ruff check src tests && uv run ruff format src tests
```

The code is split into `core` (pure logic), `capture` (recording threads), `platform` (the only OS specific code), `transcribe`, `ui` (PySide6) and `host` (CLI, MCP server, session controller). The design document is [implementation-plan.md](implementation-plan.md) (in Czech).

## License

MIT. The Whisper model and whisper.cpp are MIT licensed; FFmpeg builds carry their own licenses (see `bin/ffmpeg/*/SOURCE.json` when downloaded).
