#!/usr/bin/env python3
"""Lay out one completed "download" on disk, the way a download client would leave it.

The library generator builds a LIBRARY: a root folder with author and title directories under
it, named by a pattern. That is the wrong shape for measuring an import, which starts from a
download directory whose layout the client chose and Listenarr had no say in. This builds that
instead — one torrent's worth of files, in a named entry directly under a save path — and
prints what it built as JSON so a harness can hand the same facts to the qBittorrent stub.

It reuses ``generate_library``'s ``SilenceFactory`` and ``write_tags`` deliberately, rather
than calling ffmpeg again with its own flags. The audio and the tags are then the same ones
the rest of this repository generates, so a difference between a scan case and an import case
cannot be blamed on the fixture.

Layouts, which are the file-count and depth axes of an import matrix:

    single   one file sitting directly in the save path.        <save>/Title.mp3
    folder   a directory of N files, as a multi-file torrent.   <save>/Title/Title - Part 01.mp3
    nested   a directory with the audio one level further down. <save>/Title/Disc 1/... .mp3

The ``--ext`` choices come from the generator's own container map, ``.mp4`` included.

    ./tools/make_download_fixture.py --out /tmp/dl --ext mp3 --layout single
    ./tools/make_download_fixture.py --out /tmp/dl --ext mp4 --layout single
    ./tools/make_download_fixture.py --out /tmp/dl --ext mp3 --layout folder --count 3
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import generate_library as gen

LAYOUTS = ("single", "folder", "nested")


def build(
    out: pathlib.Path,
    ext: str,
    layout: str,
    count: int,
    meta: gen.Meta,
    entry_name: str | None = None,
    nested_dir: str = "Disc 1",
    ffmpeg: str = "ffmpeg",
) -> dict[str, Any]:
    """Write the fixture and return {entry, files, total_bytes}.

    ``entry`` is the single top-level name under ``out`` that constitutes the torrent, which
    is what ``qbittorrent_stub.py --reflect-entry`` wants. ``files`` are paths relative to
    ``out``, in the same form qBittorrent reports them in ``torrents/files``.
    """
    if layout not in LAYOUTS:
        raise ValueError(f"unknown layout {layout!r}")
    if ext not in gen.DIALECT_FOR_EXT:
        raise ValueError(f"unknown extension {ext!r}")
    if count < 1:
        raise ValueError("--count must be at least 1")

    out.mkdir(parents=True, exist_ok=True)
    silence = gen.SilenceFactory(out / ".silence-cache", ffmpeg=ffmpeg)
    dialect = gen.DIALECT_FOR_EXT[ext]
    title = gen.posix_component(meta.title)

    if layout == "single":
        entry = entry_name or f"{title}.{ext}"
        relatives = [entry]
    else:
        entry = entry_name or title
        prefix = f"{entry}/{nested_dir}" if layout == "nested" else entry
        relatives = [f"{prefix}/{title} - Part {n:02d}.{ext}" for n in range(1, count + 1)]

    for relative in relatives:
        destination = out / relative
        silence.place(ext, destination)
        gen.write_tags(destination, meta, dialect)

    # The cache is an implementation detail of the generator and must not look like part of
    # the torrent: a stub reflecting the save path would otherwise report it as content.
    cache = out / ".silence-cache"
    if cache.exists():
        for child in sorted(cache.iterdir()):
            child.unlink()
        cache.rmdir()

    return {
        "entry": entry,
        "files": relatives,
        "total_bytes": sum((out / relative).stat().st_size for relative in relatives),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--out", required=True, help="the save path to populate")
    parser.add_argument(
        "--ext",
        default="mp3",
        choices=sorted(gen.DIALECT_FOR_EXT),
        help="container extension to emit",
    )
    parser.add_argument("--layout", default="single", choices=LAYOUTS)
    parser.add_argument("--count", type=int, default=1, help="files, for folder/nested layouts")
    parser.add_argument("--title", default="The Valley of Fear")
    parser.add_argument("--author", default="Arthur Conan Doyle")
    parser.add_argument("--narrator", default="John Bolen")
    parser.add_argument(
        "--asin",
        default="B002UUFXKU",
        help="a REAL Audible ASIN from corpus/corpus.json; never invent one",
    )
    parser.add_argument("--year", default="2006")
    parser.add_argument("--entry-name", help="override the top-level entry name")
    parser.add_argument("--nested-dir", default="Disc 1")
    parser.add_argument("--ffmpeg", default="ffmpeg")
    args = parser.parse_args()

    meta = gen.Meta(
        title=args.title,
        authors=[args.author],
        narrators=[args.narrator] if args.narrator else [],
        asin=args.asin or None,
        year=args.year or None,
    )
    built = build(
        pathlib.Path(args.out),
        args.ext,
        args.layout,
        args.count,
        meta,
        entry_name=args.entry_name,
        nested_dir=args.nested_dir,
        ffmpeg=args.ffmpeg,
    )
    print(json.dumps(built))
    return 0


if __name__ == "__main__":
    sys.exit(main())
