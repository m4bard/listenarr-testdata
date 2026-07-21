#!/usr/bin/env python3
"""Differential-equivalence check for two ffprobe builds — the gate for a source swap or update.

Listenarr uses ffprobe exactly one way:

    ffprobe -v quiet -print_format json -show_format -show_streams <file>

and reads only a handful of fields from the result (see FfprobeMetadataMapper). So "functionally
equivalent for Listenarr" has a precise, testable meaning: for the same file, two ffprobe builds
produce the SAME values for those fields — not the same *whole* output (build strings, encoder tags
and demuxer lists differ harmlessly), only the fields Listenarr actually parses.

This runs Listenarr's command with a BASELINE and a CANDIDATE ffprobe across a corpus covering every
audio format Listenarr supports, extracts that functional view, and diffs it. It is the gate for two
things: proving a source swap (johnvansickle -> a durable GitHub-release build) is behaviour-safe,
and — the end goal — auto-updating the pinned build only when a new release is proven equivalent to
the current one, so security/patch fixes flow in without silently changing behaviour.

    python3 tools/ffprobe_equivalence.py --baseline /path/to/ffprobeA --candidate /path/to/ffprobeB

Exit non-zero if any functional field differs. Whole-output noise is deliberately ignored.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Any

# Listenarr's exact invocation (FfmpegService.Probing.cs).
LISTENARR_ARGS = ["-v", "quiet", "-print_format", "json", "-show_format", "-show_streams"]

# The audio formats Listenarr accepts (FileUtils.AudioExtensions), mapped to a host-ffmpeg recipe
# that produces one tagged file per format. Codecs the host ffmpeg lacks are skipped, not failed —
# the two ffprobe builds read the SAME files, so host codec availability never biases the diff.
FORMAT_RECIPES: dict[str, list[str]] = {
    "wav": ["-c:a", "pcm_s16le"],
    "mp3": ["-c:a", "libmp3lame"],
    "flac": ["-c:a", "flac"],
    "ogg": ["-c:a", "libvorbis"],
    "opus": ["-c:a", "libopus"],
    "aac": ["-c:a", "aac"],
    "m4a": ["-c:a", "aac"],
    "m4b": ["-c:a", "aac", "-f", "mp4"],
}

# Tags exercise the metadata path that is the whole point of the extraction.
TAGS = ["-metadata", "title=Ayesha The Return of She",
        "-metadata", "artist=H. Rider Haggard",
        "-metadata", "album=She"]


class ffprobeError(RuntimeError):
    pass


@dataclass
class FieldDiff:
    file: str
    field: str
    baseline: Any
    candidate: Any


def build_corpus(out_dir: pathlib.Path) -> list[pathlib.Path]:
    """Create one tagged, 1-second file per supported format the host ffmpeg can encode."""
    if shutil.which("ffmpeg") is None:
        raise ffprobeError("ffmpeg is required to build the comparison corpus")
    out_dir.mkdir(parents=True, exist_ok=True)
    files: list[pathlib.Path] = []
    for ext, codec_args in FORMAT_RECIPES.items():
        dest = out_dir / f"sample.{ext}"
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
               "-f", "lavfi", "-i", "anullsrc=r=22050:cl=mono", "-t", "1",
               *codec_args, *TAGS, str(dest)]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0 and dest.exists():
            files.append(dest)
        else:
            print(f"  (skip .{ext}: host ffmpeg cannot encode it — {result.stderr.strip()[:60]})",
                  file=sys.stderr)
    return files


def probe(ffprobe: str, file: pathlib.Path) -> dict[str, Any]:
    result = subprocess.run([ffprobe, *LISTENARR_ARGS, str(file)],
                            capture_output=True, text=True)
    if result.returncode != 0:
        raise ffprobeError(f"{ffprobe} failed on {file.name}: {result.stderr.strip()[:120]}")
    parsed: dict[str, Any] = json.loads(result.stdout or "{}")
    return parsed


def functional_view(probe_json: dict[str, Any]) -> dict[str, Any]:
    """Exactly the fields Listenarr's FfprobeMetadataMapper reads — nothing else."""
    fmt = probe_json.get("format", {})
    audio: dict[str, Any] = next((s for s in probe_json.get("streams", [])
                                  if s.get("codec_type") == "audio"), {})
    return {
        "format.duration": fmt.get("duration"),
        "format.format_name": fmt.get("format_name"),
        "format.bit_rate": fmt.get("bit_rate"),
        "format.tags": fmt.get("tags"),
        "stream.sample_rate": audio.get("sample_rate"),
        "stream.channels": audio.get("channels"),
        "stream.bit_rate": audio.get("bit_rate"),
        "stream.codec_name": audio.get("codec_name"),
        "stream.tags": audio.get("tags"),
    }


def compare(baseline: str, candidate: str, files: list[pathlib.Path]) -> list[FieldDiff]:
    """Diff the functional view produced by each build, per file."""
    diffs: list[FieldDiff] = []
    for file in files:
        base_view = functional_view(probe(baseline, file))
        cand_view = functional_view(probe(candidate, file))
        for field, base_value in base_view.items():
            if base_value != cand_view[field]:
                diffs.append(FieldDiff(file.name, field, base_value, cand_view[field]))
    return diffs


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--baseline", required=True, help="path to the baseline ffprobe binary")
    ap.add_argument("--candidate", required=True, help="path to the candidate ffprobe binary")
    ap.add_argument("--corpus", type=pathlib.Path,
                    help="a dir of audio files to compare (default: build a per-format corpus)")
    ap.add_argument("--keep-corpus", action="store_true", help="don't delete a generated corpus")
    args = ap.parse_args()

    for label, path in (("baseline", args.baseline), ("candidate", args.candidate)):
        version = subprocess.run([path, "-version"], capture_output=True, text=True)
        print(f"{label:<10} {version.stdout.splitlines()[0] if version.stdout else path}")

    generated: pathlib.Path | None = None
    if args.corpus:
        files = sorted(p for p in args.corpus.iterdir() if p.is_file())
    else:
        generated = pathlib.Path(subprocess.run(
            ["mktemp", "-d"], capture_output=True, text=True).stdout.strip())
        files = build_corpus(generated)

    print(f"\ncomparing the fields Listenarr reads across {len(files)} file(s):\n")
    try:
        diffs = compare(args.baseline, args.candidate, files)
    finally:
        if generated and not args.keep_corpus:
            shutil.rmtree(generated, ignore_errors=True)

    formats = sorted({f.suffix.lstrip(".") for f in files})
    if not diffs:
        print(f"EQUIVALENT: every field Listenarr reads matches across {', '.join(formats)}.")
        return 0

    print(f"DIFFERENCES in {len({d.file for d in diffs})} file(s):")
    for d in diffs:
        print(f"  {d.file}  {d.field}")
        print(f"    baseline : {d.baseline!r}\n    candidate: {d.candidate!r}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
