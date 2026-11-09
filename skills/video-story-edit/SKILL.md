---
name: video-story-edit
description: Open the Video Story Editor, a simple cutting room for recordings made with Video Story (list of recordings, preview with subtitles, timeline per screen, crop start/end, Render to MP4 + SRT at 24 fps), or export a recording to MP4 without the window. Use when the user says "Edit Video Story", asks to edit, trim, cut or export a Video Story recording, or wants an MP4 video of what they recorded. Do not use for starting a recording (that is the video-story skill) or for editing other video files.
---

# Video Story Editor

Recordings made with Video Story (screens as frames, microphone audio, transcript) can
be turned into an MP4 video: the editor shows every recording still stored in the
recording folder, plays it back with subtitles, lets the user crop the start and the
end and renders the selected screen to MP4 (H.264 + AAC, constant 24 fps, screen
resolution) with an SRT subtitle file next to it.

## 1. Locate and run the tool

The skill directory contains `scripts/video-story-edit.cmd` (Windows) and
`scripts/video-story-edit.sh` (Linux, macOS). Call them with an absolute path; they run
the plugin through `uv`. Every command prints one JSON object on stdout; errors are a
JSON object on stderr with `code` and `message` and a non-zero exit code.

```text
<skill-dir>\scripts\video-story-edit.cmd edit --detach
<skill-dir>/scripts/video-story-edit.sh edit --detach
```

## 2. Open the editor ("Edit Video Story") and wait for Process

1. Run `edit --detach`. Add `--session <id>` when the user named a recording (ids look
   like `20261107-122147`; `sessions` lists them). Without it the newest recording is
   selected. The result holds `editor_session` (a file path). If an editor was already
   open, it is reused (`attached` is true) and loads the recording.
2. Tell the user in one line that the editor is open and that you will act once they
   press the green **Process** button.
3. Loop until the event is `process` or `closed`:

   ```text
   <launcher> wait --editor <editor_session> --timeout 240
   ```

   `timed_out` true: call it again. Event `process`: the result carries `session_id`,
   `crop` `{start_ms, end_ms}` and **`instructions`** (the user's master instructions
   for the whole recording, `""` when none - read them first, they frame everything
   said in the recording and win over single sentences of the transcript). Then work
   text first, exactly as in section 4 of the video-story skill: run
   `timeline --session <session_id>` (the cropped range as one text timeline: speech
   with the cursor position, finished drawings with geometry and frame times, copied
   text, cursor moves between screens, zoom, keypoints), decide from it which moments
   really need a picture and fetch only those with
   `frames --session <session_id> --at <ms> --screen <screenId> --no-drawings` (for a
   shape the `after` time of its `DRAW` line). The crop applies to every screen.
   Event `closed`: the user closed the editor without Process; say so in one line and
   do nothing else.
4. `NO_RECORDINGS` means there is nothing to edit yet; suggest recording with Video Story.

What the user can do in the window: select a recording on the left; press Space to
play (icon transport buttons under the preview: go to In, step back, play/pause, stop,
step forward, go to Out; **Stop** returns to where Play was last pressed, marked by a
white arrow on the ruler; subtitles always shown under the preview, up to five lines and
then scrolling; the mouse wheel zooms the preview up to 800 %); switch between the light
and the dark theme with the button in the right corner above **Edit** (Ctrl+T); correct
the words of a subtitle with the **Edit** button next to it (the From/To times are
read-only; Save rewrites `transcript.json` and `transcript.txt`, so the transcript you
read is the user's corrected version of what was said); drag the In/Out handles on the
timeline or press `I`/`O` at the playhead to crop (one crop for all screens, remembered
per recording in `crop.json`; the handles and the playhead snap to the green keypoint
lines the user inserted while recording, which show "Keypoint N" on hover); zoom the
timeline with -/+ (the playhead stays in the middle); click a screen track to choose
what is previewed and exported. The bottom row holds **Auto process**, the green
**Process** (Ctrl+Enter), **Instructions** (Ctrl+I: the master instructions for you,
stored in `instructions.txt`, empty by default; a bullet on the button marks stored
ones; the templates **Draft Intent**, **Create Plan** and **Implement** fill the box
with a ready-made instruction), a separator and the blue **Render** (Ctrl+R) for an MP4. Process hands the
cropped range and the instructions to you and closes the editor. A recording that came
straight from the Stop dialog is transcribed in the background of the editor: a green
arrow under the audio track marks how far whisper got with `transcribing (ETA: m:ss)`
under it, subtitles appear as they are produced, and Render and Process stay disabled
until the transcript is complete (closing the editor early keeps the partial transcript;
selecting the recording again resumes it). **Auto process** presses Process by itself
five seconds after the transcript completes unless the user clicks or types.

## 3. Export without the window

When the user asks you directly for a video file ("make an MP4 of my last recording",
"export screen 2 from 10 s to 1 min"), run the export yourself:

```text
<launcher> export --session <id> [--screen screen2] [--start-ms 10000] [--end-ms 60000] [--output <path.mp4>] [--no-cursor] [--no-srt]
```

- Default output: `<recording folder>/<id>_<screen>.mp4`; the SRT gets the same name
  with `.srt`. Times are milliseconds on the recording timeline (the transcript uses
  the same timeline). The whole recording is exported when `--start-ms`/`--end-ms`
  are omitted.
- The result lists `output`, `srt`, `duration_ms`, `width`, `height`, `fps` (24) and
  `frames_used`. Rendering takes roughly real time or longer for long recordings;
  the command blocks until it finishes, so run it in the background when your shell
  has a timeout and tell the user where the file will appear.
- The cursor is drawn into the video as a small arrow at the recorded position
  (`--no-cursor` disables it); drawings made with Draw on Screen are part of the frames.

## 4. Requirements and limits

FFmpeg with `libx264` and `aac` is needed (`doctor` reports it; `doctor --fix` or
`setup` downloads a build). The source frames change only every capture interval
(500 ms by default), so the video looks like a smooth slideshow at 24 fps. Only the
start and the end can be cut; there is one audio track and no effects. Verified on
Windows; Linux and macOS support of the editor window is planned.
