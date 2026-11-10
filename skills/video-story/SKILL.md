---
name: video-story
description: Record the user's narrated screen session (microphone, every screen, cursor position) with a small always-on-top window, transcribe it offline, then act on what the user said while looking at the matching screenshots. Use when the user says "Video Story", "Start Video Story", asks to record their screen while they talk, or wants to show and tell what to do on screen. Do not use for transcribing existing media files or for plain screenshots.
---

# Video Story

The user talks while working; you receive a timed transcript plus screenshots of every
screen with the cursor position, all on one timeline (milliseconds since the recording
started). Recording and speech-to-text run on the user's machine; what you read of the
result (transcript, copied texts, the screenshots you open) reaches you like any other
file content. You act on a recording only after the user pressed **Process** in the
editor (status `processed`); every other ending means: do nothing with it.

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
recording), `editing` (the recording ended and is open in the editor, which transcribes
the audio in the background - `progress` and `message` say how far - while the user
crops it; keep waiting), `processed` (the user pressed Process: act on it), `cancelled`
(nothing to do: the window was closed without a recording, the editor was closed or
ended without Process, or the editor could not be opened; a recording that exists is
kept, `recording_dir` names it), `failed` (the window ended unexpectedly or the
recording could not be saved; see `message`. The next start of Video Story recovers
such a recording into the editor's list, but it is not yours to act on). For
`cancelled` and `failed` tell the user in one line what happened and stop.

While recording, the control window offers **Keypoint** (`Ctrl+Alt+K`): it marks the
current moment as important (the button counts down 3, 2, 1 before the next one), and
**Draw** (`Ctrl+Alt+D`) switches the drawing layer on and off. The user can press
**Stop** (`Ctrl+Alt+S`), then choose **Video Editor** (the default: the editor opens at once and
transcribes while the user crops - the green arrow under the audio track says
`transcribing (ETA: m:ss)` -, optionally corrects subtitles with **Edit**, types master
**Instructions** for you, and presses its green **Process** button, which closes the
editor; Render and Process are available only once the transcript is complete, and
**Auto process** - unticked whenever the editor opens or another recording is selected -
presses Process by itself five seconds after that unless the user clicks or types),
**Continue Recording** or **Discard Recording**; a stopped recording always goes
through the editor (discarding returns the window to the Record state; the same session
continues). Recording also stops by itself after the configured auto-stop time (default
2 hours), when less than 1 GB is left on the disk of the recording folder, or when it
can no longer be written; the recording is then saved and opened in the editor like
after Stop, and you keep waiting for Process. Hallucinated subtitle credits ("Titulky
vytvořil ...", "Subtitles by ...") are dropped from the transcript.

## 4. Read the material (status `processed`)

The final result holds `recording_dir`, `manifest`, `transcript_txt`, `transcript_json`,
`frames_dir`, **`instructions`** and **`crop`** `{start_ms, end_ms}`.

Work text first: everything that happened is available as text, and a screenshot costs
far more than the lines describing it. Follow these steps in order.

**Step 1 - the instructions.** `instructions` are the user's master instructions for the
whole recording, typed with the editor's **Instructions** button (also stored as
`instructions.txt` in the recording folder and returned by `timeline` and `describe`).
They say what the recording is for and what you must produce or respect - "create an
implementation plan from what I describe", "answer in Czech", "only fix the bug, do not
refactor" - and they override anything a single sentence of the transcript might
suggest: read them before anything else, keep them in mind for every request you carry
out, and let them shape the result (its form, language, scope). An empty string means
the user typed none; then the speech alone is your brief.

**Step 2 - the timeline.** One call returns the whole part to act on as text:

```text
<launcher> timeline --session <session_id>
```

`crop` is the part of the recording the user wants you to act on (the whole recording
unless they cropped it in the editor); `timeline` covers exactly that range
(`--start-ms`/`--end-ms` or `--all` change it). Its `timeline` field holds one event per
line, ordered by time in milliseconds since the recording started:

```text
0 START cursor screen1 2208,202
15360-20140 SAY The Instructions button down here. [cursor screen1 654,1545 -> screen1 2238,300]
24000 DRAW #1 rectangle #FF0000 screen1 corners 2118,1434 2334,1494 (pressed 22400, released 24000; frames before 22001, after 24500)
32500 REMOVED #1 (last seen 32000: Undo, Clear or the drawing layer closed)
43000 CLIPBOARD "Say what you want to get out of this recording, ..." (225 chars)
51000 CURSOR -> screen2 412,388
60000 KEYPOINT 1
70500 ZOOM screen1 2.441x region 135,244 1049x656
```

- `SAY` is a transcript segment with the cursor screen and position at its start and
  end. The text keeps the spoken language; do not translate or clean it up silently,
  quote it when you need to confirm what was said. "Here", "this" and "that" usually
  mean the cursor position or a shape drawn around that time.
- `DRAW #id` is a finished shape: `tool` (`rectangle`, `arrow`, `brush`), color, screen
  and geometry in pixels of that screen's image, origin top-left - `corners` (two
  opposite corners) of a rectangle, `tail`/`head` of an arrow, `bbox` of a brush stroke
  (its full `path` is in the sidecar). `pressed`/`released` are the mouse press and
  release; `frames before` is the last frame without the shape, `frames after` the
  first one showing it. A shape enters the timeline only once the mouse button is
  released, so the geometry is final. Ids count 1, 2, ... in the order the shapes were
  finished. `(drawn before the range)` marks a shape already on screen when the crop
  starts. Recordings made before shapes had ids say `seen <ms> to <ms>` instead of
  press and release times.
- `REMOVED` - shapes that disappeared (Undo, Clear, or the drawing layer was closed).
- `CLIPBOARD` - text the user copied (Ctrl+C) just before that moment, cut after 200
  characters; `frames --at <ms>` returns the whole text in `clipboard` (texts over 65535
  characters end with `...`; what was in the clipboard before the recording is never
  stored). When the user says "this text" or "what I copied", it is the nearest
  preceding `CLIPBOARD` line.
- `CURSOR` - the cursor moved to another screen.
- `ZOOM` / `ZOOM END` - while drawing, the user zoomed a screen in (mouse wheel, up to
  800 %): its image then shows the region `x,y widthxheight` of the real screen magnified
  `factor` times, and cursor and drawing coordinates are in pixels of that zoomed image
  (`(zoomed view)` on a `DRAW` line). To map an image point back to the real screen:
  `x + px / factor`, `y + py / factor`; `frames --at` returns the exact `zoom` of a frame.
- `KEYPOINT n` - a moment the user marked with **Keypoint** as important (the
  green lines in the editor); use them as anchors when the user refers to "the first
  keypoint" or when you look for the important parts of a long recording.

The result also carries `screens` (ids and image sizes; `screen1` is the primary),
`counts` and `transcript_complete`; when it is false, run `transcribe` first (see
below).

**Step 3 - pick the moments that need a picture.** Read the whole timeline once, work
out what the user asks for and list the requests in order. For each, decide whether the
text already answers it: a request without a reference to the screen, a copied text, or
a place you can name from the cursor or shape coordinates in code you can read needs no
image. A picture is needed when you must see what is at a place ("this button", "the
dialog here", the look of something). Skip the rest.

**Step 4 - fetch only those frames.** One frame per moment, on the screen it concerns:

```text
<launcher> frames --session <session_id> --at <ms> --screen <screenId> --no-drawings
```

- For a shape use the `after` time of its `DRAW` line (the shape on top of the content);
  add the `before` time only when the shape hides what you need to see. For speech
  without a shape use a moment inside the segment (its start, or the middle of a long
  one) and the cursor screen the `SAY` line names (or `--screen cursor`).
- The frame returned is the latest one at or before `--at`. Each entry has `image`
  (open it with your image viewing tool), `sidecar`, `cursor` `{screen, x, y}` (pixels
  of that image), `drawing_now`, `zoom` and `clipboard`; `--no-drawings` replaces the
  repeated list of shapes (already in the timeline) by `drawing_ids`. Without it each
  frame carries `drawings`: every finished shape on screen with `id`, `tool`, `color`,
  `screen`, `started_ms`, `finished_ms` and `from`/`to` or `path` (points
  `{screen, x, y}`).
- Avoid `--screen all` and `--window` unless one image does not settle it; they list
  many frames.

Frames are named `<seconds>_<millis>_<screenId>.<png|jpg>` in `frames_dir`; their
sidecars `<same>.json` lie in `frames_info_dir` (`frames_info/`), the audio in
`audio/audio.wav`. An archived recording (the editor's **Archive Video**, or any
recording older than `archive_after_days`) has no frame images and no audio any more:
`archive/<screenId>.mp4` (with `<screenId>.srt` and a still `<screenId>.jpg`) replace
them; `timeline` still works (sidecars and transcript stay), `frames` returns
`image: null`, and `describe`/`timeline`/`frames` carry an `archive` object with the
paths. `describe --session <session_id>` returns the paths, the
manifest (screens, capture interval, keypoints), `instructions`, the whole transcript and
`transcript_in_range`; you rarely need it next to `timeline`.

## 5. Act

Apply the `instructions` to the whole task, then carry out the requests the user spoke,
in order, combining the words with the shapes, cursor positions and the few frames you
fetched. When speech and image disagree or something is unclear, ask, citing the time
stamp. When the instructions and the speech disagree, the instructions win; say so.
Mention the detected language if it is unexpected. Keep the recording folder untouched
(the user may want it later).

## Other commands

- `config get|set <key> <value>|path` - settings (`recording_folder`,
  `capture_interval_ms` 1-1000, `image_format` png|jpeg, `jpeg_quality` 20-100,
  `audio_input`, `auto_stop_minutes`, `archive_after_days`). The user can also use
  the **Settings** button, which also opens the recording folder and can delete all
  recordings. Recordings older than `archive_after_days` (default 30) are archived when
  the editor starts (one MP4 per screen; frames and audio are deleted); nothing is
  deleted automatically.
- `doctor` - checks screens, audio input, FFmpeg and the whisper model;
  `doctor --fix` downloads FFmpeg when missing; `setup` also downloads the model.
- `transcribe <recording_dir>` - redo (or complete) the transcription of an existing
  recording, when the user asks you to work with a recording whose transcript is not
  complete (`describe` and `timeline` report `transcript_complete` false with a
  `transcript_note`).
- `sessions` - recent sessions; `status --session <id>` - one session.
- `mcp` - run the MCP server (stdio by default) for hosts without shell access.

## Limits

Windows, Linux X11 and macOS are supported; Wayland is not. On macOS the host application needs Screen Recording and Microphone permission. Transcription uses
the multilingual Whisper large-v3-turbo model (5-bit) on the CPU: expect mistakes in names, numbers and
punctuation, approximate segment times, and no speaker separation.
