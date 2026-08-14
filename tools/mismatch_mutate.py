"""Break the agreement between a book's folder name, its filename, and its record.

Path attribution has more than one way to bite. It looks for an identifier in the path, then for a
title-bearing ancestor directory with author context around it, then for a filename that matches
the title. A file only reaches the embedded-metadata pass when every one of those declines, so
"which construction starves attribution" is a question with several candidate answers and no way
to settle it by reading.

This mutates an already-generated library so each candidate can be measured separately:

* **folder** appends a book-number suffix to the book directory, of the shape a library gets when
  the folders were named from an edition whose title carries it and the matched record's title
  does not. Worth knowing that the title-plus-series-number variant the scanner builds joins them
  with a space, so it expects `Title 7` and a folder reading `Title, Book 7` does not match it.
  The literal word defeats the variant.
* **file** renames the audio away from the title entirely, to the sort of name a ripper leaves.
* **both** applies the two together.

The manifest is the answer key, so it is rewritten to match. A manifest that still describes the
pre-mutation layout would make every downstream byte total silently wrong, and the check that
reads it would report a shortfall that is really just a stale path.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

FOLDER_SUFFIX = ", Book 7"
FILE_STEM = "track"


def load(manifest: pathlib.Path) -> dict:
    return json.loads(manifest.read_text())


def book_entries(data: dict, asin: str) -> list[dict]:
    return [e for e in data.get("entries", [])
            if e.get("belongs_to_asin") == asin and e.get("kind") == "book"]


def mutate_folder(library: pathlib.Path, entries: list[dict]) -> list[str]:
    """Rename each distinct book directory, appending a suffix its record does not carry."""
    notes: list[str] = []
    seen: dict[str, str] = {}
    for entry in entries:
        rel = pathlib.PurePosixPath(entry["path"])
        if len(rel.parts) < 2:
            raise SystemExit(
                f"cannot apply a folder mutation to a file at the library root: {rel}")
        parent_rel = str(rel.parent)
        if parent_rel not in seen:
            old = library / parent_rel
            new_name = old.name + FOLDER_SUFFIX
            new = old.with_name(new_name)
            if not old.is_dir():
                raise SystemExit(f"expected a directory at {parent_rel}")
            old.rename(new)
            seen[parent_rel] = str(pathlib.PurePosixPath(rel.parent.parent) / new_name) \
                if len(rel.parts) > 2 else new_name
            notes.append(f"folder  {old.name}  ->  {new_name}")
        entry["path"] = str(pathlib.PurePosixPath(seen[parent_rel]) / rel.name)
    return notes


def mutate_file(library: pathlib.Path, entries: list[dict]) -> list[str]:
    """Rename each audio file to something that matches neither the title nor the folder."""
    notes: list[str] = []
    for index, entry in enumerate(entries, start=1):
        rel = pathlib.PurePosixPath(entry["path"])
        old = library / rel
        if not old.is_file():
            raise SystemExit(f"expected a file at {rel}")
        new_name = f"{FILE_STEM}{index:02d}{old.suffix}"
        old.rename(old.with_name(new_name))
        entry["path"] = str(rel.parent / new_name)
        notes.append(f"file    {rel.name}  ->  {new_name}")
    return notes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library", required=True, type=pathlib.Path)
    parser.add_argument("--asin", required=True)
    parser.add_argument("--mutation", required=True,
                        choices=("none", "folder", "file", "both"))
    args = parser.parse_args(argv)

    manifest = args.library / "manifest.json"
    if not manifest.exists():
        print(f"no manifest at {manifest}", file=sys.stderr)
        return 2

    data = load(manifest)
    entries = book_entries(data, args.asin)
    if not entries:
        print(f"no generated files for {args.asin}", file=sys.stderr)
        return 2

    notes: list[str] = []
    # Each step rewrites the manifest entry it touched before the next one reads it, so either
    # order works. File first only so the printed notes read in the order a person would expect.
    if args.mutation in ("file", "both"):
        notes += mutate_file(args.library, entries)
    if args.mutation in ("folder", "both"):
        notes += mutate_folder(args.library, entries)

    if args.mutation != "none":
        manifest.write_text(json.dumps(data, indent=2) + "\n")

    for note in notes:
        print(f"  mutate  {note}")
    if not notes:
        print("  mutate  nothing changed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
