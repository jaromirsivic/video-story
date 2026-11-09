# Acceptance checklist

Maps the "Ověření splnění" section of `video-story-spec.md` to evidence. Automated
evidence comes from `tests/unit` (offscreen) and `tests/integration` (real screens,
microphone and model; run with `VIDEO_STORY_INTEGRATION=1`). Manual steps are for a
person with the window on screen.

| # | Criterion | Automated evidence | Manual check |
| --- | --- | --- | --- |
| 1 | English window above other windows; Record starts; drawing available only while recording | `tests/unit/test_ui.py` (button states), `tests/integration/test_ui_e2e.py` (real window, Record, overlay) | Open with `video-story run`; the window stays above other apps; **Draw on Screen** is disabled until Record |
| 2 | Settings order; defaults 500 ms and PNG (250 ms in the original spec, changed to 500 ms on 7 Nov 2026 by the author); limits 1 and 1000 ms accepted; values persist after restart | `tests/unit/test_ui.py::test_settings_dialog_order_and_values` (also Keep Recordings and the Recordings row), `tests/unit/test_core.py` (bounds, round trip) | Change values, close and reopen the window, verify they are kept |
| 3 | Two screens: frames of both, audio, JSON per frame; cursor on the second screen gives its id and pixel position; drawings are listed in every sidecar with their color and points in screen pixels | `tests/integration/test_recording.py` (two screens on the Windows dev machine, cursor bounds per screen, empty `drawings` without overlay), `tests/unit/test_core.py::test_cursor_mapping` (150 % + 100 % DPI layout), `tests/unit/test_overlay.py::test_drawings_snapshot_in_screen_pixels`, `tests/integration/test_ui_e2e.py` (rectangle in the sidecar of every screen) | Move the cursor to the second screen while recording, draw a rectangle, and inspect a sidecar JSON |
| 4 | Stop then Continue keeps the recording; declining deletion keeps files; confirmed deletion removes files and returns to Record | `tests/unit/test_ui.py` (continue, discard declined, discard confirmed) | Press Stop, try each button |
| 5 | Process Recording offline produces transcript.txt and timed JSON; agent can use them after the window closed; a spoken instruction maps to the right screen and cursor | `tests/integration/test_transcription.py` (proxies disabled, Czech sample, language `cs`), `tests/integration/test_ui_e2e.py` (processed state, files in the final folder, drawn rectangle found in the frame at the spoken moment), `tests/unit/test_runner.py` (`frames --at` selection by cursor screen), `tests/unit/test_core.py::test_finalize_with_open_ui_log` (finalisation while the detached window holds `ui.log` open; found by the first manual run on Windows, 7 Nov 2026) | Say "here" while pointing; ask the agent to describe the frame |
| 6 | Hosts and operating systems | Windows 11 (two screens, 150 %/100 % DPI): all suites. Fedora 44, X11 through Xvfb + openbox: unit, doctor, recording, transcription, overlay capture, UI e2e all passed (7 Nov 2026). macOS: GitHub Actions `macos-latest` runner (7 Nov 2026): unit tests, `screens`, `record --no-audio` with non-black frames and cursor sidecar, overlay capture test on the real cocoa display, `doctor` (FFmpeg and model absent on the runner as expected). Not yet verified on a physical Mac (Retina pixel_ratio mapping covered by unit tests only). | Claude Code: skill discovered from `~/.claude/skills/video-story` (junction). Codex and Claude Desktop: registration documented, not yet exercised. ChatGPT: HTTP MCP + tunnel documented only. |
| 7 | Retention: recordings older than Keep Recordings are deleted at window start; Delete All asks first and keeps the open session | `tests/unit/test_retention.py`, `tests/unit/test_ui.py::test_settings_delete_all_asks_and_keeps_current_session` | Set Keep Recordings to 1 day with an older recording present, reopen the window; press Delete All Recordings and answer No, then Yes |

## Manual session script (5 minutes)

1. `uv run video-story doctor` - every check `ok`.
2. In the agent: "Start Video Story". The window opens top-right of the primary screen.
3. Press **Record**. Say: "Open the file I am pointing at on the second screen." Move the
   cursor to the second screen. Press **Draw on Screen**, draw a rectangle, press Esc.
4. Press **Stop**, choose **Continue Recording**, say one more sentence.
5. Press **Stop**, choose **Discard Recording**, then **Keep Recording** (files stay).
6. Press **Stop**, choose **Process Recording**. The "Transcribing Audio" dialog shows
   progress; the window closes.
7. The agent reads the transcript, calls `frames --at <ms>` and opens the screenshot of
   the screen the cursor was on; the rectangle is visible in frames taken while drawn.
8. `video-story sessions` lists the recording as `processed`.

## Editor and export (Edit Video Story)

| # | Criterion | Automated evidence | Manual check |
| --- | --- | --- | --- |
| E1 | Export: MP4 is H.264 + AAC, constant 24 fps, the screen's image resolution, cropped to [start, end]; SRT next to it with shifted, clipped segments; cursor drawn only on its screen | `tests/unit/test_export.py` (frame plan boundaries, concat list, cursor keyframes, SRT), `tests/unit/test_export_ffmpeg.py` (ffprobe: codecs, 24/1, size, duration; decoded frame colours; cursor pixels; cancel) | `video-story export --session <id> --screen screen2 --start-ms 5000 --end-ms 20000`; open the MP4 in a player, load the SRT |
| E2 | Editor opens via the agent ("Edit Video Story"), lists stored recordings newest first with the newest selected; `--session` preselects | `tests/unit/test_editor_launcher.py` (lock held by another process, NO_RECORDINGS/UNKNOWN_SESSION, MCP tool `video_story_edit`), `tests/unit/test_editor.py` (recordings model, default selection) | Say "Edit Video Story" in a new Claude Code session; the editor opens with the latest recording |
| E3 | Preview plays with audio, time display, subtitles under the preview (default on, toggle); keyboard transport | `tests/unit/test_editor.py` (subtitle text at a time, player fallback clock) | Space plays with sound; subtitles follow the speech; uncheck Subtitles |
| E4 | Timeline: one video track per screen with thumbnails, one audio track with the waveform, zoom, In/Out handles and `I`/`O`, crop shared by all screens, track click selects the exported screen | `tests/unit/test_editor.py` (timeline model math, I/O keys, Reset Crop, track selection) | Zoom with Ctrl+wheel, drag the handles, click screen2 |
| E5 | Render asks for the MP4 path (default `<recording folder>/<id>_<screenN>.mp4`), shows progress with Cancel, reports MP4 and SRT paths; the editor stays open | `tests/unit/test_editor.py` (Render passes the selected screen and crop to the export) | Render a cropped range of screen2; check the file and the SRT |
| E6 | Platforms | Windows 11: unit tests, real export of recording 20261107-122147, editor window verified on screen (7 Nov 2026). Linux and macOS: unit and export tests run in CI; the editor window is not yet verified there (planned milestone E7). | |

## Version 2 (crop handed to the agent, preview zoom, new defaults)

| # | Criterion | Automated evidence | Manual check |
| --- | --- | --- | --- |
| V1 | New defaults: JPEG at 70 %; Settings shows them; everything works with JPEG frames | `tests/unit/test_core.py::test_config_defaults`, `tests/unit/test_ui.py::test_settings_dialog_order_and_values`, export/editor tests use JPEG and PNG | Record once, check `frames/*.jpg` |
| V2 | Draw on Screen starts with the rectangle tool; 8-colour palette, red first | `tests/unit/test_overlay.py` (palette, default tool) | Press Draw on Screen, draw without choosing a tool |
| V3 | Preview zoom: wheel zooms around the cursor up to 800 %, zoom-out snaps to fit, drag pans, double-click resets; cursor/drawings stay aligned; export unaffected | `tests/unit/test_editor_preview.py` | Zoom into a corner of the preview, play, switch screens |
| V4 | Stop dialog offers Video Editor: the recording is processed and the editor opens with it; the session is `editing` and `wait` keeps waiting | `tests/unit/test_ui.py::test_video_editor_choice_processes_and_hands_over`, `..._falls_back_to_processed`, `tests/unit/test_editor_launcher.py` (attach request to a running editor) | Record, Stop, Video Editor |
| V5 | Green Process in the editor: crop saved, session `processed` with `crop`, the agent acts only on that range; `describe` returns `transcript_in_range` | `tests/unit/test_editor_process.py`, `tests/unit/test_editor_session.py` (events, finish/cancel, describe) | Crop, press Process, watch the agent use only that part |
| V6 | Standalone editor: the agent waits on `wait --editor`; Process sends session id and crop; closing without Process sends `closed` and the agent does nothing | `tests/unit/test_editor_session.py::test_editor_session_events_and_wait`, `tests/unit/test_editor_process.py` | "Edit Video Story", pick an older recording, Process; then repeat and close instead |
| V7 | Closing the editor without Process after Video Editor cancels the session (recording kept) | `tests/unit/test_editor_session.py::test_recording_session_finish_and_cancel`, `tests/unit/test_editor_process.py` | Record, Stop, Video Editor, close the editor |
| V8 | Crop is stored in `crop.json` on every change and restored when the recording is reopened | `tests/unit/test_editor_process.py`, `tests/unit/test_editor_session.py::test_crop_round_trip_and_clamping` | Crop, close the editor, reopen it |
| V9 | Drawing: the left button uses the selected tool, the right button always draws an arrow; the control window stays above the drawing layer and can be dragged and clicked; Esc leaves, Ctrl+Z undoes, Ctrl+Shift+C clears (also with the focus in the control window); the Stop dialog buttons have icons | `tests/unit/test_overlay.py` (right button, kept window, shortcuts), `tests/unit/test_ui.py::test_stop_dialog_buttons_have_icons` | Record, Draw on Screen, draw with both buttons, drag the control window by its title bar, press Ctrl+Z, Ctrl+Shift+C, Esc, then Stop |

### Manual editor script (5 minutes)

1. In the agent: "Edit Video Story". The editor opens; the newest recording is selected.
2. Press Space; the preview plays with sound and the subtitle under it follows the speech.
3. Move the playhead to 5 s and press `I`, to 30 s and press `O`; the timeline darkens outside.
4. Click the `screen2` track; the preview switches to the second screen.
5. Press **Render**, keep the default path, wait for the progress dialog, open the MP4.
6. Check that the SRT next to it starts at 00:00:00,000 and contains the spoken sentences.
