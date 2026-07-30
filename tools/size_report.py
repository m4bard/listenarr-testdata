"""Judge the size a library reports for an audiobook against the bytes actually on disk.

`validate_reported_size.sh` scans a generated multi-file book and then asks this module a single
question: does the size shown for the audiobook equal the size of the files that belong to it?

There are three numbers, and keeping them apart is the whole point:

* **on-disk** — the real bytes, summed from the generated files via the manifest. Ground truth.
* **linked sum** — what the library recorded per file, summed. Says whether the scan saw the files.
* **reported** — the single value the UI shows for the book as a whole.

Splitting them separates two very different faults. If the linked sum matches disk but the reported
value does not, the per-file records are fine and the summary is derived wrongly. If the linked sum
itself is short, the scan missed files and the summary is only wrong downstream of that.

Exit codes follow verify_scan: 0 pass, 1 the reported size is wrong, 2 inconclusive.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import pathlib
import sqlite3
import sys
from typing import Any

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_INCONCLUSIVE = 2


@dataclasses.dataclass
class SizeReport:
    """What the library says about one book's size, next to what is true."""

    label: str
    asin: str | None
    title: str
    on_disk_bytes: int
    on_disk_files: int
    linked_sum_bytes: int
    linked_files: int
    reported_bytes: int | None

    @property
    def scan_found_everything(self) -> bool:
        return (self.linked_files == self.on_disk_files
                and self.linked_sum_bytes == self.on_disk_bytes)

    @property
    def verdict(self) -> str:
        if self.linked_files == 0:
            return "inconclusive"
        if self.reported_bytes is None:
            return "missing"
        if self.reported_bytes == self.linked_sum_bytes:
            return "correct"
        return "wrong"

    @property
    def exit_code(self) -> int:
        return {
            "inconclusive": EXIT_INCONCLUSIVE,
            "missing": EXIT_FAIL,
            "wrong": EXIT_FAIL,
            "correct": EXIT_PASS,
        }[self.verdict]

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "asin": self.asin,
            "title": self.title,
            "on_disk_bytes": self.on_disk_bytes,
            "on_disk_files": self.on_disk_files,
            "linked_sum_bytes": self.linked_sum_bytes,
            "linked_files": self.linked_files,
            "reported_bytes": self.reported_bytes,
            "scan_found_everything": self.scan_found_everything,
            "verdict": self.verdict,
        }


def on_disk_totals(manifest: dict[str, Any], library: pathlib.Path, asin: str) -> tuple[int, int]:
    """Sum the real bytes of every generated file belonging to `asin`."""
    total = 0
    count = 0
    for entry in manifest.get("entries", []):
        if entry.get("belongs_to_asin") != asin:
            continue
        path = library / entry["path"]
        if path.exists():
            total += path.stat().st_size
            count += 1
    if count == 0:
        raise ValueError(f"the manifest describes no files on disk for {asin}")
    return total, count


def read_reported(db: pathlib.Path, book_id: str) -> tuple[str | None, str, int | None, int, int]:
    """Read the stored size, plus the per-file rows the scan linked."""
    connection = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        record = connection.execute(
            "SELECT Asin AS asin, Title AS title, FileSize AS size FROM Audiobooks WHERE Id = ?",
            (book_id,),
        ).fetchone()
        files = connection.execute(
            "SELECT COALESCE(SUM(Size), 0) AS total, COUNT(*) AS n "
            "FROM AudiobookFiles WHERE AudiobookId = ?",
            (book_id,),
        ).fetchone()
    finally:
        connection.close()
    if record is None:
        return None, "?", None, 0, 0
    return record["asin"], record["title"] or "?", record["size"], files["total"], files["n"]


def render(report: SizeReport) -> str:
    lines = [
        "",
        f"  === {report.label} ===",
        f"  book            : {report.title} [{report.asin}]",
        f"  on disk         : {report.on_disk_bytes:,} bytes across {report.on_disk_files} files",
        f"  linked by scan  : {report.linked_sum_bytes:,} bytes across {report.linked_files} files",
        "  reported size   : "
        + ("not set" if report.reported_bytes is None else f"{report.reported_bytes:,} bytes"),
    ]
    if not report.scan_found_everything and report.linked_files:
        lines.append("  NOTE            : the scan did not link every file, so the summary is")
        lines.append("                    downstream of discovery, not only of summing")
    if report.verdict == "missing":
        lines.append("  VERDICT         : the book has files but no size is stored for it")
    elif report.verdict == "wrong":
        short = report.linked_sum_bytes - (report.reported_bytes or 0)
        lines.append(f"  VERDICT         : reported size is off by {short:,} bytes")
    elif report.verdict == "correct":
        lines.append("  VERDICT         : reported size matches the linked files")
    else:
        lines.append("  VERDICT         : no files linked, nothing to judge")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=pathlib.Path)
    parser.add_argument("--library", required=True, type=pathlib.Path)
    parser.add_argument("--db", required=True, type=pathlib.Path)
    parser.add_argument("--book-id", required=True)
    parser.add_argument("--asin", required=True)
    parser.add_argument("--label", default="reported size")
    parser.add_argument("--json", dest="json_out", type=pathlib.Path)
    args = parser.parse_args(argv)

    try:
        manifest = json.loads(args.manifest.read_text())
        on_disk, on_disk_files = on_disk_totals(manifest, args.library, args.asin)
    except (OSError, ValueError) as exc:
        print(f"size_report: cannot establish ground truth: {exc}", file=sys.stderr)
        return EXIT_INCONCLUSIVE

    asin, title, reported, linked_sum, linked_files = read_reported(args.db, args.book_id)
    report = SizeReport(
        label=args.label,
        asin=asin or args.asin,
        title=title,
        on_disk_bytes=on_disk,
        on_disk_files=on_disk_files,
        linked_sum_bytes=linked_sum,
        linked_files=linked_files,
        reported_bytes=reported,
    )
    print(render(report))
    if args.json_out:
        args.json_out.write_text(json.dumps(report.to_dict(), indent=2))
    return report.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
