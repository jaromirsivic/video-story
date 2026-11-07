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
recording. Statuses: `ready` (window open), `recording`, `processing` (transcribing),
`processed` (done), `cancelled` (window closed without a recording), `failed` (see
`message` and `recording.log` in the work folder).

The user can press **Stop**, then choose **Process Recording**, **Continue Recording**
or **Discard Recording** (discarding returns the window to the Record state; the same
session continues). Recording also stops automatically after the configured
auto-stop time (default 2 hours) and is then processed.

## 4. Read the material (status `processed`)

The final result holds `recording_dir`, `manifest`, `transcript_txt`, `transcript_json`
and `frames_dir`. Read `transcript_txt` first. Lines look like:

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
- `drawings` is a list (empty when nothing was drawn). Each entry has `tool`
  (`rectangle`, `arrow` or `brush`), `color` (`#RRGGBB`), `screen` and the geometry in
  pixels of that screen's image: `from`/`to` for a rectangle (two opposite corners) or
  an arrow (tail, head), `path` (a list of points) for a brush stroke. Every point is
  `{screen, x, y}`. The same list is stored in the sidecar of every screen of that tick,
  so read it from any frame.
- `describe --session <session_id>` returns the manifest, paths and the whole
  transcript at once. The manifest lists the screens (`screen1` is the primary) with
  their sizes and the capture interval.

Frames are named `<seconds>_<millis>_<screenId>.<png|jpg>` with a sidecar
`<same>.json`; you can also browse `frames_dir` directly.

## 5. Act

Carry out the requests the user spoke, in order, combining the words with the frames
and cursor positions. When speech and image disagree or something is unclear, ask,
citing the time stamp. Mention the detected language if it is unexpected. Keep the
recording folder untouched (the user may want it later).

## Other commands

- `config get|set <key> <value>|path` - settings (`recording_folder`,
  `capture_interval_ms` 1-1000, `image_format` png|jpeg, `jpeg_quality` 20-100,
  `audio_input`, `auto_stop_minutes`, `retention_days`). The user can also use the
  **Settings** button, which also opens the recording folder and can delete all
  recordings. Recordings older than `retention_days` (default 30) are deleted when the
  window starts.
- `doctor` - checks screens, audio input, FFmpeg and the whisper model;
  `doctor --fix` downloads FFmpeg when missing; `setup` also downloads the model.
- `transcribe <recording_dir>` - redo the transcription of an existing recording.
- `sessions` - recent sessions; `status --session <id>` - one session.
- `mcp` - run the MCP server (stdio by default) for hosts without shell access.

## Limits

Windows, Linux X11 and macOS are supported; Wayland is not. On macOS the host application needs Screen Recording and Microphone permission. Transcription uses
the multilingual Whisper small model on the CPU: expect mistakes in names, numbers and
punctuation, approximate segment times, and no speaker separation.
