# Acceptance checklist

Maps the "Ověření splnění" section of `video-story-spec.md` to evidence. Automated
evidence comes from `tests/unit` (offscreen) and `tests/integration` (real screens,
microphone and model; run with `VIDEO_STORY_INTEGRATION=1`). Manual steps are for a
person with the window on screen.

| # | Criterion | Automated evidence | Manual check |
| --- | --- | --- | --- |
| 1 | English window above other windows; Record starts; drawing available only while recording | `tests/unit/test_ui.py` (button states), `tests/integration/test_ui_e2e.py` (real window, Record, overlay) | Open with `video-story run`; the window stays above other apps; **Draw on Screen** is disabled until Record |
| 2 | Settings order; defaults 250 ms and PNG; limits 1 and 1000 ms accepted; values persist after restart | `tests/unit/test_ui.py::test_settings_dialog_order_and_values`, `tests/unit/test_core.py` (bounds, round trip) | Change values, close and reopen the window, verify they are kept |
| 3 | Two screens: frames of both, audio, JSON per frame; cursor on the second screen gives its id and pixel position | `tests/integration/test_recording.py` (two screens on the Windows dev machine, cursor bounds per screen), `tests/unit/test_core.py::test_cursor_mapping` (150 % + 100 % DPI layout) | Move the cursor to the second screen while recording and inspect a sidecar JSON |
| 4 | Stop then Continue keeps the recording; declining deletion keeps files; confirmed deletion removes files and returns to Record | `tests/unit/test_ui.py` (continue, discard declined, discard confirmed) | Press Stop, try each button |
| 5 | Process Recording offline produces transcript.txt and timed JSON; agent can use them after the window closed; a spoken instruction maps to the right screen and cursor | `tests/integration/test_transcription.py` (proxies disabled, Czech sample, language `cs`), `tests/integration/test_ui_e2e.py` (processed state, files in the final folder, drawn rectangle found in the frame at the spoken moment), `tests/unit/test_runner.py` (`frames --at` selection by cursor screen), `tests/unit/test_core.py::test_finalize_with_open_ui_log` (finalisation while the detached window holds `ui.log` open; found by the first manual run on Windows, 7 Nov 2026) | Say "here" while pointing; ask the agent to describe the frame |
| 6 | Hosts and operating systems | Windows 11 (two screens, 150 %/100 % DPI): all suites. Fedora 44, X11 through Xvfb + openbox: unit, doctor, recording, transcription, overlay capture, UI e2e all passed (7 Nov 2026). macOS: GitHub Actions `macos-latest` runner (7 Nov 2026): unit tests, `screens`, `record --no-audio` with non-black frames and cursor sidecar, overlay capture test on the real cocoa display, `doctor` (FFmpeg and model absent on the runner as expected). Not yet verified on a physical Mac (Retina pixel_ratio mapping covered by unit tests only). | Claude Code: skill discovered from `~/.claude/skills/video-story` (junction). Codex and Claude Desktop: registration documented, not yet exercised. ChatGPT: HTTP MCP + tunnel documented only. |

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
