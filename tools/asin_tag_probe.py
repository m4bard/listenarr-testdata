#!/usr/bin/env python3
"""Read or stamp the embedded ASIN tag that Listenarr's tag writer targets.

Listenarr writes an ASIN into a file's own tags after an import, in exactly three places
depending on the container (``TagLibAudioTagWriter.ApplyAsinTag``):

    MP4/M4B   a freeform atom ``----:com.apple.iTunes:ASIN``
    ID3v2     a ``TXXX`` frame with description ``ASIN``
    Xiph      a comment field ``ASIN``

``read`` answers one question — is one of those present, and what does it say. ``stamp``
writes the same three, which is what makes a positive control possible: a file that
demonstrably carries the atom the writer targets, so a reader that reports it absent is
a broken reader rather than evidence of anything.

    asin_tag_probe.py read  FILE [--expect-asin B0...] [--ffprobe PATH] [--json PATH]
    asin_tag_probe.py stamp FILE --asin B0...

``read`` exits 0 when an ASIN tag is present (and matches ``--expect-asin`` if given),
1 when it is absent or disagrees, 2 when the file could not be judged at all.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys
from typing import Any

from mutagen.flac import FLAC
from mutagen.id3 import TXXX, ID3FileType
from mutagen.mp4 import MP4, MP4FreeForm
from mutagen.oggvorbis import OggVorbis

MP4_ATOM = "----:com.apple.iTunes:ASIN"
ID3_DESC = "ASIN"
XIPH_FIELD = "asin"


class UnreadableFile(Exception):
    """The file could not be opened as a tagged audio file at all."""


def _open(path: pathlib.Path) -> Any:
    suffix = path.suffix.lower()
    try:
        if suffix in {".m4b", ".m4a", ".mp4", ".aac"}:
            return MP4(path)
        if suffix == ".mp3":
            return ID3FileType(path)
        if suffix == ".flac":
            return FLAC(path)
        if suffix in {".ogg", ".opus"}:
            return OggVorbis(path)
    except Exception as exc:  # a truncated or non-audio file is not a verdict, it is an error
        raise UnreadableFile(f"{path}: {exc}") from exc
    raise UnreadableFile(f"{path}: no tag container is known for '{suffix}'")


def read_asin(path: pathlib.Path) -> tuple[str | None, str | None]:
    """Return ``(asin, where)`` for the first ASIN tag found, or ``(None, None)``."""
    audio = _open(path)
    tags = audio.tags
    if tags is None:
        return None, None

    if isinstance(audio, MP4):
        values = tags.get(MP4_ATOM)
        if values:
            raw = bytes(values[0])
            return raw.decode("utf-8", "replace").strip() or None, MP4_ATOM
        return None, None

    if isinstance(audio, ID3FileType):
        for frame in tags.getall("TXXX"):
            if frame.desc.upper() == ID3_DESC and frame.text:
                return str(frame.text[0]).strip() or None, f"TXXX:{frame.desc}"
        return None, None

    for key in tags:
        if key.lower() == XIPH_FIELD:
            values = tags[key]
            if values:
                return str(values[0]).strip() or None, key
    return None, None


def stamp_asin(path: pathlib.Path, asin: str) -> str:
    """Write ``asin`` into the same tag the Listenarr writer would use. Returns the tag name."""
    audio = _open(path)
    if audio.tags is None:
        audio.add_tags()
    tags = audio.tags
    if isinstance(audio, MP4):
        tags[MP4_ATOM] = [MP4FreeForm(asin.encode("utf-8"))]
        audio.save()
        return MP4_ATOM
    if isinstance(audio, ID3FileType):
        tags.add(TXXX(encoding=3, desc=ID3_DESC, text=[asin]))
        audio.save()
        return f"TXXX:{ID3_DESC}"
    audio[XIPH_FIELD] = [asin]
    audio.save()
    return XIPH_FIELD


def ffprobe_tags(ffprobe: pathlib.Path, path: pathlib.Path) -> dict[str, str]:
    """What Listenarr's own reader would see: the format tags, via its exact ffprobe call."""
    proc = subprocess.run(
        [str(ffprobe), "-v", "quiet", "-print_format", "json",
         "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True, timeout=120, check=False,
    )
    if proc.returncode != 0:
        return {}
    payload = json.loads(proc.stdout or "{}")
    return dict((payload.get("format") or {}).get("tags") or {})


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="mode", required=True)

    reader = sub.add_parser("read", help="report whether an ASIN tag is present")
    reader.add_argument("file", type=pathlib.Path)
    reader.add_argument("--expect-asin", help="also require the tag to carry this value")
    reader.add_argument("--ffprobe", type=pathlib.Path,
                        help="also record what this ffprobe reports for the file")
    reader.add_argument("--json", dest="json_out", type=pathlib.Path)
    reader.add_argument("--label", default="")

    stamper = sub.add_parser("stamp", help="write an ASIN tag, for a positive control")
    stamper.add_argument("file", type=pathlib.Path)
    stamper.add_argument("--asin", required=True)

    args = ap.parse_args()

    if not args.file.is_file():
        print(f"asin-tag: no such file: {args.file}", file=sys.stderr)
        return 2

    if args.mode == "stamp":
        try:
            stamped = stamp_asin(args.file, args.asin)
        except UnreadableFile as exc:
            print(f"asin-tag: {exc}", file=sys.stderr)
            return 2
        print(f"asin-tag: stamped {args.asin} into {stamped}")
        return 0

    try:
        asin, where = read_asin(args.file)
    except UnreadableFile as exc:
        print(f"asin-tag: {exc}", file=sys.stderr)
        return 2

    if asin is None:
        verdict = "untagged"
    elif args.expect_asin and asin.upper() != args.expect_asin.upper():
        verdict = "wrong-asin"
    else:
        verdict = "tagged"

    label = f"{args.label}: " if args.label else ""
    detail = f"{asin} in {where}" if asin else "no ASIN tag in any container Listenarr writes"
    print(f"asin-tag: {label}{verdict} — {detail}")

    probe: dict[str, str] = {}
    if args.ffprobe and args.ffprobe.is_file():
        probe = ffprobe_tags(args.ffprobe, args.file)
        seen = sorted(k for k in probe if "asin" in k.lower())
        print(f"asin-tag: {label}ffprobe reports "
              f"{', '.join(f'{k}={probe[k]}' for k in seen) if seen else 'no ASIN-bearing tag'}")

    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps({
            "file": args.file.name, "label": args.label, "verdict": verdict,
            "asin": asin, "tag": where, "expected": args.expect_asin,
            "ffprobe_format_tags": probe,
        }, indent=2) + "\n")

    return 0 if verdict == "tagged" else 1


if __name__ == "__main__":
    raise SystemExit(main())
