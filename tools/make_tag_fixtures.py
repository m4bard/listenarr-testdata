#!/usr/bin/env python3
"""Build a fixture set for code that adopts an identifier from a book's tagged files.

The existing ``tag-dialects`` scenario answers "can the extractor read every spelling ffprobe
might surface an ASIN under". That is a different question from the one a *unanimity* guard
asks, which is "do the files in this book agree, and what should happen when they do not".
A guard like that is only interesting across several files in ONE book, and the generator
writes the same metadata to every file of a book, so the disagreement cases cannot come out
of it.

This emits one directory per case, each a single book whose files are tagged individually:

    agree-same-dialect      every file carries the same ASIN, one dialect
    agree-mixed-dialect     same ASIN, but m4b/mp3/flac spell it differently
    disagree               two files carry DIFFERENT ASINs, both real
    partial-one-tagged     one file tagged, the rest bare
    partial-lone-file      a single tagged file and nothing else
    untagged               no file carries an identifier in any spelling

``partial-lone-file`` is the case worth having. A lone tagged file is trivially unanimous, so
a guard that adopts on agreement will adopt from it, and there is no second file to disagree
later. It is the difference between "the files agree" and "there was only ever one file".

Every ASIN is a real, verified one from the corpus. Audio is one second of generated silence.
A ``manifest.json`` records, per file, which ASIN was written and under which keys, so a test
can assert against the manifest rather than against hardcoded expectations.

    python3 tools/make_tag_fixtures.py --out fixtures/tag-adoption
"""
from __future__ import annotations

import argparse
import json
import pathlib
import shutil
import sys
from dataclasses import dataclass, field
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from generate_library import Meta, SilenceFactory, load_corpus, write_tags


class FixtureError(RuntimeError):
    """The fixture set cannot be built as specified. Never swallowed."""


@dataclass(frozen=True)
class FileSpec:
    """One file in a fixture book: its extension, dialect, and whose ASIN it claims."""

    name: str
    ext: str
    dialect: str
    asin_from: int | None  # index into the chosen books, or None to write no identifier


@dataclass(frozen=True)
class Case:
    key: str
    note: str
    expect: str
    files: list[FileSpec] = field(default_factory=list)


CASES: list[Case] = [
    Case(
        key="agree-same-dialect",
        note="Three files, one dialect, all carrying the same ASIN.",
        expect="the identifier is adopted",
        files=[
            FileSpec("Part 01.m4b", "m4b", "mp4-atoms", 0),
            FileSpec("Part 02.m4b", "m4b", "mp4-atoms", 0),
            FileSpec("Part 03.m4b", "m4b", "mp4-atoms", 0),
        ],
    ),
    Case(
        key="agree-mixed-dialect",
        note="Same ASIN, but each container spells it its own way. Agreement has to be "
             "decided after extraction, not by comparing raw tag keys.",
        expect="the identifier is adopted",
        files=[
            FileSpec("Part 01.m4b", "m4b", "mp4-atoms", 0),
            FileSpec("Part 02.mp3", "mp3", "id3v24", 0),
            FileSpec("Part 03.flac", "flac", "vorbis", 0),
        ],
    ),
    Case(
        key="disagree",
        note="Two files claiming different books. Both ASINs are real, so this is not a "
             "malformed-tag case: the files genuinely disagree.",
        expect="no identifier is adopted",
        files=[
            FileSpec("Part 01.m4b", "m4b", "mp4-atoms", 0),
            FileSpec("Part 02.m4b", "m4b", "mp4-atoms", 1),
        ],
    ),
    Case(
        key="partial-one-tagged",
        note="One tagged file among untagged ones. Unanimous among files that carry an "
             "identifier at all, which is a choice the guard has to make explicitly.",
        expect="decide whether silence counts as agreement",
        files=[
            FileSpec("Part 01.m4b", "m4b", "mp4-atoms", 0),
            FileSpec("Part 02.m4b", "m4b", "none", None),
            FileSpec("Part 03.m4b", "m4b", "none", None),
        ],
    ),
    Case(
        key="partial-lone-file",
        note="A single tagged file and nothing else. Trivially unanimous, and the case "
             "raised in review: on a first scan there is no second file to disagree, and "
             "an identifier adopted here cannot be retracted once one appears.",
        expect="decide whether one file is enough to adopt from",
        files=[FileSpec("Part 01.m4b", "m4b", "mp4-atoms", 0)],
    ),
    Case(
        key="untagged",
        note="No identifier in any spelling. The common real-world case, and the baseline "
             "that proves a passing test is not passing by accident.",
        expect="no identifier is adopted",
        files=[
            FileSpec("Part 01.m4b", "m4b", "none", None),
            FileSpec("Part 02.m4b", "m4b", "none", None),
        ],
    ),
]


def pick_books(corpus: list[dict[str, Any]], count: int) -> list[dict[str, Any]]:
    """Two distinct books with real ASINs, chosen deterministically.

    Deterministic on purpose: a fixture set that changes between runs cannot be used to
    assert anything stable, and a reviewer regenerating it must get the same bytes.
    """
    usable = sorted(
        (b for b in corpus if b.get("asin") and b.get("title")),
        key=lambda b: str(b["asin"]),
    )
    if len(usable) < count:
        raise FixtureError(f"corpus has {len(usable)} usable books, need {count}")
    return usable[:count]


def build(out: pathlib.Path, cache_dir: pathlib.Path, ffmpeg: str) -> dict[str, Any]:
    corpus = load_corpus()
    books = pick_books(corpus, 2)
    audio = SilenceFactory(cache_dir, ffmpeg=ffmpeg)

    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    manifest: dict[str, Any] = {
        "note": "Generated fixtures for identifier adoption across a book's files. "
                "Audio is one second of silence. Every ASIN is a real catalogue value.",
        "books": [{"asin": b["asin"], "title": b["title"], "authors": b["authors"]} for b in books],
        "cases": [],
    }

    for case in CASES:
        book_dir = out / case.key
        entry: dict[str, Any] = {
            "key": case.key,
            "note": case.note,
            "expect": case.expect,
            "files": [],
        }
        for spec in case.files:
            dest = book_dir / spec.name
            audio.place(spec.ext, dest)
            source = books[spec.asin_from] if spec.asin_from is not None else None
            meta = Meta(
                title=str(source["title"]) if source else str(books[0]["title"]),
                authors=list(source["authors"]) if source else list(books[0]["authors"]),
                asin=str(source["asin"]) if source else None,
            )
            written = write_tags(dest, meta, spec.dialect)
            entry["files"].append({
                "file": spec.name,
                "dialect": spec.dialect,
                "asin": meta.asin,
                "tags_written": written,
            })
        manifest["cases"].append(entry)

    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--out", type=pathlib.Path, required=True, help="directory to create")
    ap.add_argument("--cache", type=pathlib.Path, default=pathlib.Path("build/ffmpeg-cache"))
    ap.add_argument("--ffmpeg", default="ffmpeg")
    args = ap.parse_args()

    manifest = build(args.out, args.cache, args.ffmpeg)
    total = sum(len(c["files"]) for c in manifest["cases"])
    print(f"{len(manifest['cases'])} cases, {total} files -> {args.out}")
    for case in manifest["cases"]:
        tagged = sum(1 for f in case["files"] if f["asin"])
        print(f"  {case['key']:<22} {len(case['files'])} file(s), {tagged} tagged")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except FixtureError as exc:
        print(f"make_tag_fixtures: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
