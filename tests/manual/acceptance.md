# Acceptance checklist

Maps the behaviour specified in `video-story-spec.md` to evidence. Automated evidence
comes from `tests/unit` (offscreen) and `tests/integration` (real screens, microphone
and model; run with `VIDEO_STORY_INTEGRATION=1`). Manual steps are for a person with the
window on screen.

## Recording

| # | Criterion | Automated evidence | Manual check |
| --- | --- | --- | --- |
| 1 | English window above other windows; Record starts; drawing and Keypoint are available only while recording; Stop / Keypoint / Draw have the shortcuts Ctrl+Alt+S / K / D, named on the buttons | `tests/unit/test_ui.py` (button states, keypoint countdown, shortcuts), `tests/integration/test_ui_e2e.py` (real window, Record, overlay) | Open with `video-story run`; the window stays above other apps; **Draw** is disabled until Record; while recording, with another program in front, Ctrl+Alt+K inserts a keypoint, Ctrl+Alt+D toggles drawing and Ctrl+Alt+S opens the Stop dialog in front |
| 2 | Settings order; defaults 500 ms, JPEG 70 %, auto stop 120 min, keep 30 days; limits 1 and 1000 ms accepted; values persist after restart | `tests/unit/test_ui.py::test_settings_dialog_order_and_values`, `tests/unit/test_core.py` (bounds, round trip, defaults) | Change values, close and reopen the window, verify they are kept |
| 3 | A new Recording Folder applies at once to the open window: the recording, the editor and the waiting agent use the new folder | `tests/unit/test_ui.py::test_new_recording_folder_applies_to_the_open_session`, `..._failed_settings_save_keeps_the_session_where_it_was` | Open the window, change the folder in Settings, record, Stop, Video Editor: the recording is in the new folder and the agent's `wait` still follows the session |
| 4 | Two screens: frames of both, audio, JSON per frame; cursor on the second screen gives its id and pixel position; finished drawings are listed in every sidecar with their color, times and points in screen pixels; copied text lands once | `tests/integration/test_recording.py`, `tests/unit/test_core.py::test_cursor_mapping`, `tests/unit/test_overlay.py`, `tests/unit/test_recorder_sidecars.py`, `tests/integration/test_ui_e2e.py` | Move the cursor to the second screen while recording, draw a rectangle, copy a text, inspect a sidecar JSON |
| 5 | Stop then Continue keeps the recording; declining deletion keeps files; confirmed deletion removes files and returns to Record | `tests/unit/test_ui.py` (continue, discard declined, discard confirmed) | Press Stop, try each button |
| 6 | Stop offers Video Editor (default), Continue Recording, Discard Recording. Video Editor saves the recording at once (no transcription in the window), opens the editor and leaves the session `editing` | `tests/unit/test_ui.py::test_video_editor_choice_saves_and_hands_over`, `tests/integration/test_editor_flow.py` | Record, Stop, Video Editor |
| 7 | Auto stop ends like Stop -> Video Editor: saved, editor opens, nothing is handed to the agent | `tests/unit/test_ui.py::test_auto_stop_opens_the_editor_and_processes_nothing` | Set Auto Stop Recording to 1 minute and wait |
| 8 | Less than 1 GB free on the disk of the recording folder: Record does not start; a running recording stops, a message names the 1 GB limit, the recording is saved and opens in the editor | `tests/unit/test_resilience.py` (`..._does_not_start_below_one_gigabyte`, `..._stops_by_itself_when_the_disk_runs_low`), `tests/unit/test_ui.py::test_low_disk_stops_tells_the_user_and_opens_the_editor` | Record to a nearly full volume |
| 9 | A screen that cannot be captured (lock screen, UAC prompt, a monitor that went away) does not end the recording; that screen has no frames meanwhile and capture resumes by itself; the manifest counts `failed_grabs` | `tests/unit/test_resilience.py::test_a_screen_that_cannot_be_grabbed_does_not_end_the_recording` | While recording press Win+L, unlock, continue; the recording goes on |
| 10 | A recording that can no longer be written stops with a message and is saved; Stop never hangs | `tests/unit/test_resilience.py::test_audio_writer_that_cannot_write_reports_it_and_stop_returns`, `tests/unit/test_ui.py::test_write_error_stops_and_saves_what_there_is` | - |
| 11 | Record tells at once when the Whisper model or FFmpeg is missing | `tests/unit/test_ui.py::test_record_reports_missing_transcription_parts` | Rename the model file, press Record |
| 12 | A recording whose window died is finished at the next start (manifest from frames and audio, keypoints kept) and listed in the editor; it is never handed to the agent | `tests/unit/test_resilience.py` (recovery tests) | Kill the window process while recording, then open the editor |
| 13 | Archiving: Archive Video / Archive Videos (N) and Delete Videos (N) in the list menu; every screen becomes archive/<screen>.mp4 + .srt + .jpg, its frames are deleted at once, audio and frames/ at the end (locked files stay, the archive still completes); an editor closed half way resumes; recordings older than Archive After are queued (transcribed first) when the editor starts, nothing is deleted automatically; list labels TRANSCRIBING... / ARCHIVING... with one decimal, ARCHIVED; an archived recording shows its stills and subtitles, everything in the bottom row disabled; Delete All asks first and keeps the open session | `tests/unit/test_archive.py`, `tests/unit/test_editor_archive.py`, `tests/unit/test_retention.py`, `tests/unit/test_ui.py::test_settings_delete_all_asks_and_keeps_current_session` | Archive two recordings, close the editor while the first one runs, reopen it; open the archived one and drag the playhead; set Archive After to 1 day with an older recording present and open the editor |
| 14 | Hosts and operating systems | Windows 11 (two screens, 150 %/100 % DPI): all suites. Fedora 44, X11 through Xvfb + openbox: unit, doctor, recording, transcription, overlay capture, UI e2e passed (7 Nov 2026). macOS: GitHub Actions `macos-latest` runner: unit tests, `screens`, `record --no-audio`, overlay capture, `doctor`. Not verified on a physical Mac (Retina mapping covered by unit tests only). | Claude Code: skill discovered from `~/.claude/skills/video-story` (junction). Codex and Claude Desktop: registration documented, not yet exercised. ChatGPT: HTTP MCP + tunnel documented only. |

## Handing a recording to the agent

Only **Process** in the editor hands a recording over.

| # | Criterion | Automated evidence | Manual check |
| --- | --- | --- | --- |
| H1 | Process: crop saved, session `processed` with `crop` and `instructions`, the agent acts only on that range; `timeline` and `describe` cover it | `tests/unit/test_editor_process.py`, `tests/unit/test_editor_session.py`, `tests/unit/test_events.py` | Crop, press Process, watch the agent use only that part |
| H2 | Closing the editor without Process: session `cancelled`, recording kept, the agent does nothing | `tests/unit/test_editor_process.py::test_close_without_process_reports_closed_and_cancels` | Record, Stop, Video Editor, close the editor |
| H3 | An editor that crashes or is killed: the same as closing it (`cancelled`), never "act on the whole recording" | `tests/unit/test_editor_session.py::test_session_status_turns_a_dead_editor_into_cancelled`, `tests/unit/test_transcript_flags.py`, `tests/unit/test_instructions.py` | Kill the editor process while the agent waits |
| H4 | The editor cannot be opened: session `cancelled`, recording kept | `tests/unit/test_ui.py::test_editor_that_cannot_open_hands_nothing_to_the_agent` | - |
| H5 | Auto process is never ticked when the editor opens (not remembered), is unticked when another recording is selected or handed to the window, and counts down only for the recording it was ticked for | `tests/unit/test_editor_transcription.py` (`..._is_never_ticked_when_the_editor_opens`, `test_selecting_another_recording_unticks_auto_process`, countdown tests) | Tick Auto process, select another recording: the box is unticked and nothing is sent |
| H6 | While the agent waits for a recording that is still being transcribed, Process over another recording is refused | `tests/unit/test_resilience.py::test_process_waits_for_the_transcript_of_the_awaited_recording` | Stop -> Video Editor on a long recording, select an older one, press Process |
| H7 | A failed transcription can be retried in the editor without closing it; the agent keeps waiting | `tests/unit/test_resilience.py::test_failed_transcription_can_be_retried_without_closing` | - |
| H8 | Standalone editor: the agent waits on `wait --editor`; Process sends session id, crop and instructions; closing sends `closed` | `tests/unit/test_editor_session.py::test_editor_session_events_and_wait`, `tests/unit/test_editor_process.py` | "Edit Video Story", pick an older recording, Process; then repeat and close instead |

## Manual session script (5 minutes)

1. `uv run video-story doctor` - every check `ok`.
2. In the agent: "Start Video Story". The window opens top-right of the primary screen.
3. Press **Record**. Say: "Open the file I am pointing at on the second screen." Move the
   cursor to the second screen. Press **Draw**, draw a rectangle, press Esc.
   Press **Keypoint**.
4. Press **Stop**, choose **Continue Recording**, say one more sentence.
5. Press **Stop**, choose **Discard Recording**, then **Keep Recording** (files stay).
6. Press **Stop**, choose **Video Editor**. The control window closes and the editor
   opens at once; a green arrow under the audio track shows the transcription progress
   and **Process** stays disabled until it is complete. **Auto process** is unticked.
7. Press the green **Process**. The editor closes; the agent reads the instructions and
   the timeline, calls `frames --at <ms>` for the moments it needs and opens the
   screenshot of the screen the cursor was on; the rectangle is visible in frames taken
   while it was drawn.
8. `video-story sessions` lists the recording as `processed`.
9. Repeat steps 3 and 6, then close the editor instead of pressing Process: the agent
   reports that nothing was handed over and `sessions` shows `cancelled`.

## Editor and export (Edit Video Story)

| # | Criterion | Automated evidence | Manual check |
| --- | --- | --- | --- |
| E1 | Export: MP4 is H.264 + AAC, constant 24 fps, the screen's image resolution, cropped to [start, end]; SRT next to it with shifted, clipped segments; cursor drawn only on its screen | `tests/unit/test_export.py`, `tests/unit/test_export_ffmpeg.py` (ffprobe: codecs, 24/1, size, duration; decoded frame colours; cursor pixels; cancel) | `video-story export --session <id> --screen screen2 --start-ms 5000 --end-ms 20000`; open the MP4 in a player, load the SRT |
| E2 | Editor opens via the agent ("Edit Video Story"), lists stored recordings newest first with the newest selected; `--session` preselects; a running editor is reused | `tests/unit/test_editor_launcher.py`, `tests/unit/test_editor.py`, `tests/unit/test_editor_requests.py` | Say "Edit Video Story" in a new Claude Code session; the editor opens with the latest recording |
| E3 | Preview plays with audio, time display, subtitles under the preview (up to five lines, then scrolling); icon transport; Stop returns to where Play was pressed; wheel zooms the preview up to 800 % | `tests/unit/test_editor.py`, `tests/unit/test_editor_preview.py`, `tests/unit/test_editor_features.py` | Space plays with sound; subtitles follow the speech |
| E4 | Timeline: one video track per screen with thumbnails, one audio track with the waveform, zoom with -/+, In/Out handles and `I`/`O`, crop shared by all screens and stored in `crop.json`, handles snap to keypoints, track click selects the exported screen | `tests/unit/test_editor.py`, `tests/unit/test_editor_process.py`, `tests/unit/test_editor_features.py` | Drag the handles, click screen2, reopen the recording: the crop is restored |
| E5 | Transcription runs in the background of the editor with progress and ETA, resumes after the editor was closed, subtitles can be corrected with Edit | `tests/unit/test_editor_transcription.py`, `tests/unit/test_pipeline_resume.py`, `tests/unit/test_editor_subtitles.py` | Stop -> Video Editor on a longer recording |
| E6 | Instructions: typed text and templates are stored in `instructions.txt` and travel with Process | `tests/unit/test_instructions.py`, `tests/unit/test_editor_features.py` | Type instructions, Process, check the agent follows them |
| E7 | Render asks for the MP4 path (default `<recording folder>/<id>_<screenN>.mp4`), shows progress with Cancel, reports MP4 and SRT paths; the editor stays open | `tests/unit/test_editor.py` | Render a cropped range of screen2; check the file and the SRT |
| E8 | Recordings can be deleted from the list (context menu, multi-selection) after a confirmation | `tests/unit/test_editor_list_rows.py`, `tests/unit/test_editor_features.py` | Right-click a row, Delete Video |
| E9 | Platforms | Windows 11: unit tests, real export, editor window verified on screen. Linux and macOS: unit and export tests run in CI; the editor window is not yet verified there. | |

### Manual editor script (5 minutes)

1. In the agent: "Edit Video Story". The editor opens; the newest recording is selected.
2. Press Space; the preview plays with sound and the subtitle under it follows the speech.
3. Move the playhead to 5 s and press `I`, to 30 s and press `O`; the timeline darkens outside.
4. Click the `screen2` track; the preview switches to the second screen.
5. Press **Render**, keep the default path, wait for the progress dialog, open the MP4.
6. Check that the SRT next to it starts at 00:00:00,000 and contains the spoken sentences.
