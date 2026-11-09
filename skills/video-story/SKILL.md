---
name: video-story
description: Record the user's narrated screen session (microphone, every screen, cursor position) with a small always-on-top window, transcribe it offline, then act on what the user said while looking at the matching screenshots. Use when the user says "Video Story", "Start Video Story", asks to record their screen while they talk, or wants to show and tell what to do on screen. Do not use for transcribing existing media files or for plain screenshots.
---

# Video Story

The user talks while working; you receive a timed transcript plus screenshots of every
screen with the cursor position, all on one timeline (milliseconds since the recording
started). Everything runs locally; nothing is uploaded.

## 1. Locate and run the tool

The skill directory contains `scripts/video-story.cmd` (Windows) and
`scripts/video-story.sh` (Linux, macOS). Call them with an absolute path; they run the
plugin through `uv` (the first run creates the environment, which can take a minute).
Every command prints one JSON object on stdout; errors are a JSON object on stderr with
`code` and `message` and a non-zero exit code.

```text
<skill-dir>\scripts\video-story.cmd run --detach
<skill-dir>/scripts/video-story.sh run --detach
```

## 2. Start a session

1. Run `run --detach`. The result contains `session_id`. Tell the user in one line that
   the Video Story window is open and that recording starts when they press **Record**.
2. If the error code is `ALREADY_RUNNING`, a window is already open: use the session id
   named in the message and continue with the wait loop.
3. Do not run `run` without `--detach`: it blocks until the user finishes.

## 3. Wait for the user

Loop until `final` is `true`:

```text
<launcher> wait --session <session_id> --timeout 240
```

Each call returns after at most 240 s with the current `status` and `timed_out`. Call it
again when `timed_out` is true. Do not start other work while waiting; the user is
recording. Statuses: `ready` (window open), `recording`, `processing` (saving the
recording), `editing` (the user chose **Video Editor** after Stop: the editor is open,
transcribes the audio in the background - `progress` and `message` say how far - and
the user crops the recording; keep waiting), `processed` (done), `cancelled` (window
closed without a recording, or the editor was closed without Process: do nothing),
`failed` (see `message` and `recording.log` in the work folder).

While recording, the control window offers **Insert keypoint**: it marks the current
moment as important (the button counts down 3, 2, 1 before the next one). The user can
press **Stop**, then choose **Video Editor** (the default: the editor opens at once and
transcribes while the user crops - the green arrow under the audio track says
`transcribing (ETA: m:ss)` -, optionally corrects subtitles with **Edit**, types master
**Instructions** for you, and presses its green **Process** button, which closes the
editor; Render and Process are available only once the transcript is complete, and
**Auto process** presses Process by itself five seconds after that unless the user
clicks or types), **Continue Recording** or **Discard Recording**; a stopped recording
always goes through the editor (discarding returns the window to the Record state; the
same session continues). Recording also stops automatically after the configured
auto-stop time (default 2 hours) and is then transcribed in the control window and
processed without the editor. Hallucinated subtitle credits ("Titulky vytvořil ...",
"Subtitles by ...") are dropped from the transcript.

## 4. Read the material (status `processed`)

The final result holds `recording_dir`, `manifest`, `transcript_txt`, `transcript_json`,
`frames_dir`, **`instructions`** and **`crop`** `{start_ms, end_ms}`.

**`instructions` come first.** They are the user's master instructions for the whole
recording, typed with the editor's **Instructions** button (also stored as
`instructions.txt` in the recording folder and returned by `describe`). They say what
the recording is for and what you must produce or respect - "create an implementation
plan from what I describe", "answer in Czech", "only fix the bug, do not refactor" -
and they override anything a single sentence of the transcript might suggest: read them
before the transcript, keep them in mind for every request you carry out, and let them
shape the result (its form, language, scope). An empty string means the user typed
none; then the transcript alone is your brief.

`crop` is the part of the recording the user wants you to act on (the whole recording
unless they cropped it in the editor). Act only on speech and frames inside that range;
`describe` returns `transcript_in_range` with just those segments. Then read the
transcript. Lines look like:

```text
[00:00:03.480 --> 00:00:07.120] And here, where my cursor is, add a button.
```

The transcript keeps the spoken language; do not translate or clean it up silently.
Quote it when you need to confirm what was said.

For every instruction that refers to something on screen, fetch the matching frame:

```text
<launcher> frames --session <session_id> --at <milliseconds> --screen cursor
```

- `--at` is a point on the timeline (use the segment start, or the middle for long
  segments). The frame returned is the latest one at or before that moment.
- `--screen cursor` (default) returns the screen the cursor was on; `--screen all`
  returns every screen; `--screen screen2` a specific one. `--window 1000` also lists the
  frames within one second for context.
- Each frame entry has `image` (open it with your image viewing tool), `sidecar`,
  `cursor`: `{screen, x, y}` in pixels of that image, origin top-left, and `drawings`:
  what the user had drawn on the screens at that moment. "Here", "this" and "that" in
  the speech usually mean the cursor location or the drawing.
- `drawings` is a list of the finished shapes (empty when nothing was drawn). A shape
  enters it only when the user releases the mouse button, so the geometry is always
  final. Each entry has `id` (1, 2, ... in the order the shapes were finished; it stays
  the same in every later frame, and disappears after Undo or Clear), `tool`
  (`rectangle`, `arrow` or `brush`), `color` (`#RRGGBB`), `screen`, `started_ms` and
  `finished_ms` (press and release, on the same timeline as the transcript; match them
  with what was said around that time) and the geometry in pixels of that screen's
  image: `from`/`to` for a rectangle (two opposite corners) or an arrow (tail, head),
  `path` (a list of points) for a brush stroke. Every point is `{screen, x, y}`. The
  list is ordered by `id` and stored in the sidecar of every screen of that tick, so
  read it from any frame; the last frame of a segment usually holds every shape it
  talks about, there is no need to scan the frames in between.
- `drawing_now` is `{tool, color, screen, started_ms}` while the user is dragging a
  shape (no geometry yet) and `null` otherwise. The image of such a frame already shows
  the half-drawn shape, which is not in `drawings` yet: take its final geometry from a
  later frame, where it appears in `drawings` with the same `started_ms`.
- `zoom` is a list (empty normally). While drawing, the user can zoom a screen in with
  the mouse wheel (up to 800 %) and pan it: the image of that screen then shows a frozen,
  magnified picture. An entry `{screen, factor, x, y, width, height}` says that the
  image shows the region `x, y, width, height` (pixels of the unzoomed screen) magnified
  `factor` times; `cursor` and `drawings` are in pixels of the zoomed image as shown.
  To map an image point back to the real screen: `x + px / factor`, `y + py / factor`.
- `clipboard` is the text the user copied (Ctrl+C) just before that frame: it appears in
  the sidecars of exactly one tick after each change of the text clipboard (`null` in
  every other frame; texts over 65535 characters are cut and end with `...`). What was
  in the clipboard before the recording started is never stored. When the user says
  "this text", "what I copied" or pastes something, look for the nearest preceding
  frame whose `clipboard` is not null (`--window` helps), or scan the sidecars in
  `frames_dir` for the `clipboard` key.
- `keypoints` (in `describe` and in the manifest, `[{index, time_ms}]`) are the moments
  the user marked with **Insert keypoint** while recording - "this is important", a
  place they may crop from. The editor shows them as green lines; use them as anchors
  when the user refers to "the first keypoint" or when you look for the important parts
  of a long recording. The list is empty when none were inserted.
- `describe --session <session_id>` returns the manifest, paths, `instructions`,
  `keypoints` and the whole transcript at once. The manifest lists the screens
  (`screen1` is the primary) with their sizes and the capture interval.

Frames are named `<seconds>_<millis>_<screenId>.<png|jpg>` with a sidecar
`<same>.json`; you can also browse `frames_dir` directly.

## 5. Act

Apply the `instructions` to the whole task, then carry out the requests the user spoke,
in order, combining the words with the frames and cursor positions. When speech and
image disagree or something is unclear, ask, citing the time stamp. When the
instructions and the speech disagree, the instructions win; say so. Mention the detected
language if it is unexpected. Keep the recording folder untouched (the user may want it
later).

## Other commands

- `config get|set <key> <value>|path` - settings (`recording_folder`,
  `capture_interval_ms` 1-1000, `image_format` png|jpeg, `jpeg_quality` 20-100,
  `audio_input`, `auto_stop_minutes`, `retention_days`). The user can also use the
  **Settings** button, which also opens the recording folder and can delete all
  recordings. Recordings older than `retention_days` (default 30) are deleted when the
  window starts.
- `doctor` - checks screens, audio input, FFmpeg and the whisper model;
  `doctor --fix` downloads FFmpeg when missing; `setup` also downloads the model.
- `transcribe <recording_dir>` - redo (or complete) the transcription of an existing
  recording. `describe` reports `transcript_complete` false with a `transcript_note`
  when the editor's background transcription did not finish (the editor died); run
  `transcribe` first in that case.
- `sessions` - recent sessions; `status --session <id>` - one session.
- `mcp` - run the MCP server (stdio by default) for hosts without shell access.

## Limits

Windows, Linux X11 and macOS are supported; Wayland is not. On macOS the host application needs Screen Recording and Microphone permission. Transcription uses
the multilingual Whisper large-v3-turbo model (5-bit) on the CPU: expect mistakes in names, numbers and
punctuation, approximate segment times, and no speaker separation.
