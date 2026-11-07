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

## 2. Open the editor ("Edit Video Story")

1. Run `edit --detach`. Add `--session <id>` when the user named a recording (ids look
   like `20261107-122147`; `sessions` lists them). Without it the newest recording is
   selected.
2. Tell the user in one line that the Video Story Editor is open. Do not wait for it:
   the user works in the window (play, crop, pick a screen, Render) and the result is a
   file they chose; nothing comes back to you.
3. `ALREADY_RUNNING` means the editor window is already open; say so.
4. `NO_RECORDINGS` means there is nothing to edit yet; suggest recording with Video Story.

What the user can do in the window: select a recording on the left; press Space to
play (subtitles under the preview, toggle with the Subtitles box); drag the In/Out
handles on the timeline or press `I`/`O` at the playhead to crop (one crop for all
screens); click a screen track to choose what is previewed and exported; press
**Render** (Ctrl+R), choose the MP4 path, wait for the progress dialog.

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
