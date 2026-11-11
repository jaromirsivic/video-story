"""Benchmark of the live transcription on finished recordings (milestone L0).

For every recording the audio (microphone + computer sound, like the default of the
editor) is transcribed

* in one go, like the editor does today (``baseline``), and
* in windows by :class:`video_story.transcribe.live.LiveCore`, in two regimes:
  ``keepup`` - the worker always waits for the next window (audio arrives in 2 s
  steps, as when the transcription keeps up with the narration) - and ``lagging`` -
  the whole audio is there (as when the transcription is behind).

It reports the speed (real-time factor = processing time / audio time) and how far the
live text differs from the baseline (word error rate between the two, after lower
casing and removing punctuation) and writes the transcripts and a word diff, so the
differences can be judged by reading. With ``--reference`` (a corrected
``transcript.json`` of the same recording) both are also scored against it.

    uv run python scripts/live_benchmark.py REC_DIR [REC_DIR ...] --threads 8 \
        --priority light --regime keepup --regime lagging --baseline --out OUT_DIR
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import sys
import time
import unicodedata
from dataclasses import replace
from pathlib import Path

import numpy as np

from video_story.core.audio_mix import COMPUTER, MICROPHONE, existing_tracks
from video_story.core.config import load_config
from video_story.core.session import SessionPaths
from video_story.core.transcript import Segment, read_transcript
from video_story.platform.process import lower_own_priority
from video_story.transcribe.ffmpeg import ensure_ffmpeg, resample_to_16k
from video_story.transcribe.growing_wav import GrowingMix
from video_story.transcribe.live import LiveCore, LiveParams
from video_story.transcribe.pipeline import transcribe_audio
from video_story.transcribe.whisper_backend import PyWhisperCppBackend


def words(text: str) -> list[str]:
    cleaned = "".join(
        " " if unicodedata.category(ch).startswith(("P", "S")) else ch for ch in text.casefold()
    )
    return cleaned.split()


def wer(reference: list[str], hypothesis: list[str]) -> float:
    """Word error rate (Levenshtein distance / reference length)."""
    if not reference:
        return 0.0 if not hypothesis else 1.0
    previous = np.arange(len(hypothesis) + 1)
    for i, ref in enumerate(reference, 1):
        current = np.empty_like(previous)
        current[0] = i
        for j, hyp in enumerate(hypothesis, 1):
            current[j] = min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ref != hyp))
        previous = current
    return float(previous[-1]) / len(reference)


def word_diff(a: list[str], b: list[str], context: int = 4) -> list[str]:
    out = []
    matcher = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        before = " ".join(a[max(0, i1 - context) : i1])
        after = " ".join(a[i2 : i2 + context])
        out.append(
            f"... {before} [baseline: {' '.join(a[i1:i2]) or '-'} | live: "
            f"{' '.join(b[j1:j2]) or '-'}] {after} ..."
        )
    return out


def text_of(segments: list[Segment]) -> str:
    return " ".join(s.text for s in segments)


def run_live(
    paths: SessionPaths,
    base: Path,
    backend,
    ffmpeg: Path,
    threads: int,
    regime: str,
    params: LiveParams,
) -> dict:
    tracks = existing_tracks(paths, base)
    mix = GrowingMix(tracks[MICROPHONE], tracks.get(COMPUTER))
    total_ms = mix.available_ms()
    core = LiveCore(
        mix,
        backend,
        lambda samples, rate: resample_to_16k(ffmpeg, samples, rate),
        threads,
        params,
    )
    segments: list[Segment] = []
    windows = []
    whisper_s = 0.0
    available = total_ms if regime == "lagging" else 0
    started = time.monotonic()
    while core.done_ms < total_ms:
        if regime == "keepup":
            available = max(available, core.done_ms)
            window = None
            while window is None and available < total_ms:
                available = min(total_ms, available + params.poll_ms)
                window = core.next_window(available)
            if window is None:
                window = core.next_window(total_ms, final=True)
        else:
            window = core.next_window(total_ms, final=True)
        if window is None:
            break
        result = core.transcribe(window)
        segments.extend(result.segments)
        whisper_s += result.seconds if result.speech else 0.0
        windows.append(
            {
                "start_ms": window.start_ms,
                "end_ms": window.end_ms,
                "cut": str(window.cut),
                "speech": result.speech,
                "seconds": round(result.seconds, 2),
                "segments": len(result.segments),
                "next_ms": result.done_ms,
                "dropped": result.dropped,
            }
        )
        print(
            f"  {regime} {window.start_ms / 1000:7.1f}-{window.end_ms / 1000:7.1f} s "
            f"{window.cut:9} speech={result.speech!s:5} {result.seconds:5.1f} s "
            f"-> {result.done_ms / 1000:.1f}",
            flush=True,
        )
    elapsed = time.monotonic() - started
    cuts: dict[str, int] = {}
    for w in windows:
        key = w["cut"] if w["speech"] else "silence"
        cuts[key] = cuts.get(key, 0) + 1
    return {
        "segments": segments,
        "language": core.language,
        "audio_s": total_ms / 1000,
        "seconds": round(elapsed, 1),
        "rtf": round(elapsed / max(0.001, total_ms / 1000), 3),
        "windows": windows,
        "cuts": cuts,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("recordings", nargs="+", type=Path)
    parser.add_argument("--threads", type=int, default=8)
    parser.add_argument("--baseline-threads", type=int, default=None)
    parser.add_argument("--priority", choices=["normal", "light", "faster"], default="normal")
    parser.add_argument("--regime", action="append", choices=["keepup", "lagging"])
    parser.add_argument("--baseline", action="store_true")
    parser.add_argument("--prompt", action="store_true", help="pass the text before")
    parser.add_argument("--no-lock", action="store_true")
    parser.add_argument("--early-cut", action="store_true")
    parser.add_argument("--reference", type=Path, action="append", default=[])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--label", default="", help="suffix of the result files")
    args = parser.parse_args()

    if args.priority != "normal":
        print("priority:", lower_own_priority(args.priority))
    config = load_config()
    ffmpeg = ensure_ffmpeg(config, download=False)
    backend = PyWhisperCppBackend(config.resolved_model_path())
    params = LiveParams(
        use_prompt=args.prompt, lock_language=not args.no_lock, early_cut=args.early_cut
    )
    args.out.mkdir(parents=True, exist_ok=True)
    summary = []
    for index, recording in enumerate(args.recordings):
        recording = recording.resolve()
        paths = SessionPaths(recording.parent, recording.name)
        base = recording
        name = recording.name
        print(f"== {name}", flush=True)
        row: dict = {"recording": name, "threads": args.threads, "priority": args.priority}
        texts: dict[str, list[str]] = {}
        cache = args.out / f"{name}.baseline.json"
        if args.baseline and cache.exists():
            cached = json.loads(cache.read_text(encoding="utf-8"))
            row["baseline"] = cached["row"]
            segments = [Segment.from_dict(s) for s in cached["segments"]]
            texts["baseline"] = words(text_of(segments))
            print(f"  baseline from {cache.name}", flush=True)
        elif args.baseline:
            started = time.monotonic()
            result = transcribe_audio(
                paths,
                replace(config, whisper_threads=args.baseline_threads or args.threads),
                base_dir=base,
                backend=backend,
            )
            seconds = time.monotonic() - started
            audio_s = result.audio_duration_ms / 1000
            row["baseline"] = {
                "seconds": round(seconds, 1),
                "rtf": round(seconds / max(0.001, audio_s), 3),
                "language": result.transcript.language,
                "segments": len(result.transcript.segments),
            }
            texts["baseline"] = words(text_of(result.transcript.segments))
            cache.write_text(
                json.dumps(
                    {
                        "row": row["baseline"],
                        "segments": [s.to_dict() for s in result.transcript.segments],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (args.out / f"{name}.baseline.txt").write_text(
                "\n".join(
                    f"[{s.start_ms / 1000:8.2f}-{s.end_ms / 1000:8.2f}] {s.text}"
                    for s in result.transcript.segments
                ),
                encoding="utf-8",
            )
            print(f"  baseline {seconds:.1f} s, rtf {row['baseline']['rtf']}", flush=True)
        for regime in args.regime or []:
            live = run_live(paths, base, backend, ffmpeg, args.threads, regime, params)
            live["segments_count"] = len(live["segments"])
            live["mean_segment_s"] = round(
                sum(s.end_ms - s.start_ms for s in live["segments"])
                / max(1, len(live["segments"]))
                / 1000,
                1,
            )
            regime = regime + args.label
            segments = live.pop("segments")
            texts[regime] = words(text_of(segments))
            (args.out / f"{name}.{regime}.txt").write_text(
                "\n".join(
                    f"[{s.start_ms / 1000:8.2f}-{s.end_ms / 1000:8.2f}] {s.text}" for s in segments
                ),
                encoding="utf-8",
            )
            (args.out / f"{name}.{regime}.windows.json").write_text(
                json.dumps(live.pop("windows"), indent=1), encoding="utf-8"
            )
            row[regime] = live
            if "baseline" in texts:
                live["wer_vs_baseline"] = round(wer(texts["baseline"], texts[regime]), 4)
                (args.out / f"{name}.{regime}.diff.txt").write_text(
                    "\n".join(word_diff(texts["baseline"], texts[regime])), encoding="utf-8"
                )
            print(f"  {regime}: {live}", flush=True)
        if index < len(args.reference):
            reference = words(text_of(read_transcript(args.reference[index]).segments))
            row["reference_wer"] = {k: round(wer(reference, v), 4) for k, v in texts.items()}
        summary.append(row)
        (args.out / f"summary{args.label}.json").write_text(
            json.dumps(summary, indent=2), encoding="utf-8"
        )
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    os.environ.setdefault("PYTHONUTF8", "1")
    raise SystemExit(main())
