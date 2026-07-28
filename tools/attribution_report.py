"""Classify the files a scan attributed to one audiobook, by their TRUE owner.

`validate_scan_attribution.sh` adds a single audiobook, clears its BasePath so the scan root
falls back to the library root, and scans. This module reads what the scan concluded and
answers the only question that matters: of the files now linked to that record, how many
actually belong to it?

The generator's manifest is the answer key — one entry per file, recording the book that file
really belongs to. A linked file whose true owner is a different book is a misattribution, and
misattribution is silent: nothing errors, the library simply claims a book owns audio it does
not, and the common parent of those files becomes its BasePath.

Exit codes follow verify_scan: 0 pass, 1 fail (a foreign file was claimed), 2 inconclusive
(the observation could not be trusted — an unreadable manifest or linked paths the manifest
does not describe). Inconclusive is deliberately NOT a pass: a checker that cannot see the
answer key must not report success.
"""
from __future__ import annotations

import argparse
import collections
import dataclasses
import json
import os
import pathlib
import sqlite3
import sys
from typing import Any

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_INCONCLUSIVE = 2

LIBRARY_MOUNT = "/audiobooks/"


@dataclasses.dataclass(frozen=True)
class LinkedFile:
    """One row of AudiobookFiles, as the scan left it."""

    path: str


@dataclasses.dataclass
class Report:
    """What the scan claimed, judged against the manifest."""

    label: str
    scanned_asin: str | None
    scanned_title: str
    base_path: str | None
    files_linked: int
    own_files: int
    foreign: dict[str, int]
    foreign_titles: dict[str, str]
    unmapped: int

    @property
    def foreign_files(self) -> int:
        return sum(self.foreign.values())

    @property
    def exit_code(self) -> int:
        if self.unmapped:
            return EXIT_INCONCLUSIVE
        return EXIT_FAIL if self.foreign else EXIT_PASS

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "scanned_asin": self.scanned_asin,
            "scanned_title": self.scanned_title,
            "base_path": self.base_path,
            "files_linked": self.files_linked,
            "own_files": self.own_files,
            "foreign_files": self.foreign_files,
            "foreign_books": {self.foreign_titles.get(a, a): n for a, n in self.foreign.items()},
            "unmapped": self.unmapped,
            "verdict": {EXIT_PASS: "pass", EXIT_FAIL: "fail",
                        EXIT_INCONCLUSIVE: "inconclusive"}[self.exit_code],
        }


def owner_index(manifest: dict[str, Any]) -> tuple[dict[str, str], dict[str, str]]:
    """Map each manifest file path to the ASIN that truly owns it, plus ASIN -> title.

    Raises ValueError when the manifest carries no usable entries — an empty index would
    silently classify every linked file as unmapped and look like a clean inconclusive
    rather than the broken input it is.
    """
    owner: dict[str, str] = {}
    titles: dict[str, str] = {}
    for entry in manifest.get("entries", []):
        asin = entry.get("belongs_to_asin")
        path = entry.get("path")
        if not asin or not path:
            continue
        owner[os.path.normpath(path)] = asin
        titles[asin] = entry.get("true_title") or asin
    if not owner:
        raise ValueError("manifest describes no files — cannot judge attribution")
    return owner, titles


def to_manifest_path(container_path: str) -> str:
    """Rewrite a container-side path so it can be compared with a manifest path."""
    relative = container_path.replace(LIBRARY_MOUNT, "", 1).lstrip("/")
    return os.path.normpath(relative)


def classify(
    linked: list[LinkedFile],
    owner: dict[str, str],
    titles: dict[str, str],
    *,
    label: str,
    scanned_asin: str | None,
    scanned_title: str,
    base_path: str | None,
) -> Report:
    """Split the linked files into the book's own and those belonging to other books."""
    by_owner: collections.Counter[str] = collections.Counter()
    unmapped = 0
    for item in linked:
        true_owner = owner.get(to_manifest_path(item.path))
        if true_owner is None:
            unmapped += 1
        else:
            by_owner[true_owner] += 1
    own = by_owner.get(scanned_asin, 0) if scanned_asin else 0
    foreign = {asin: n for asin, n in by_owner.items() if asin != scanned_asin}
    return Report(
        label=label,
        scanned_asin=scanned_asin,
        scanned_title=scanned_title,
        base_path=base_path,
        files_linked=len(linked),
        own_files=own,
        foreign=foreign,
        foreign_titles=titles,
        unmapped=unmapped,
    )


def render(report: Report) -> str:
    """Human-readable summary, in the shape the other validators print."""
    lines = [
        "",
        f"  === {report.label} ===",
        f"  scanned      : {report.scanned_title} [{report.scanned_asin}]",
        f"  files linked : {report.files_linked}",
        f"  BasePath now : {report.base_path}",
        f"  own files    : {report.own_files}",
        f"  foreign files: {report.foreign_files} across {len(report.foreign)} other book(s)",
    ]
    for asin, count in sorted(report.foreign.items(), key=lambda pair: -pair[1]):
        lines.append(f"      {count:4d}  {report.foreign_titles.get(asin, asin)}  [{asin}]")
    if report.unmapped:
        lines.append(f"  unmapped     : {report.unmapped} (not described by the manifest)")
    lines.append("")
    return "\n".join(lines)


ScanState = tuple[list[LinkedFile], str | None, str, str | None]


def read_scan(db: pathlib.Path, book_id: str) -> ScanState:
    """Read the record and the files the scan linked to it, straight out of SQLite."""
    connection = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        record = connection.execute(
            "SELECT Asin AS asin, Title AS title, BasePath AS base FROM Audiobooks WHERE Id = ?",
            (book_id,),
        ).fetchone()
        rows = connection.execute(
            "SELECT Path AS path FROM AudiobookFiles WHERE AudiobookId = ?", (book_id,)
        ).fetchall()
    finally:
        connection.close()
    linked = [LinkedFile(path=row["path"]) for row in rows]
    if record is None:
        return linked, None, "?", None
    return linked, record["asin"], record["title"] or "?", record["base"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=pathlib.Path)
    parser.add_argument("--db", required=True, type=pathlib.Path)
    parser.add_argument("--book-id", required=True)
    parser.add_argument("--label", default="scan attribution")
    parser.add_argument("--json", dest="json_out", type=pathlib.Path)
    args = parser.parse_args(argv)

    try:
        manifest = json.loads(args.manifest.read_text())
        owner, titles = owner_index(manifest)
    except (OSError, ValueError) as exc:
        print(f"attribution_report: cannot read the answer key: {exc}", file=sys.stderr)
        return EXIT_INCONCLUSIVE

    linked, asin, title, base = read_scan(args.db, args.book_id)
    report = classify(
        linked, owner, titles,
        label=args.label, scanned_asin=asin, scanned_title=title, base_path=base,
    )
    print(render(report))
    if args.json_out:
        args.json_out.write_text(json.dumps(report.to_dict(), indent=2))
    return report.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
