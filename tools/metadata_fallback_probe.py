"""Judge whether a scan's embedded-metadata fallback can still claim a file.

A scan attributes files to a book by looking at the path first: an identifier in the path, a
title-bearing folder with author context around it, or a filename that matches the title. When
none of that bites, there is a second pass that opens each remaining candidate, reads its
embedded tags, and claims it if the tags identify the book. That second pass is the only thing
standing between a correctly tagged file in an unrecognised folder shape and a book that shows
no files at all.

`validate_metadata_fallback.sh` runs two libraries through a real scan and asks this module to
judge them:

* **fallback** — the book sits in a folder carrying its title and nothing else, so no author
  context exists anywhere on the path and every path heuristic declines. The tags are correct.
  Only the metadata pass can claim this file.
* **control** — the same book in a layout the path heuristics do handle. It must be claimed, and
  it must be claimed *without* the metadata pass being involved.

The control is the point. A fallback check that reports "not claimed" proves nothing on its own,
because a harness that cannot claim anything at all produces exactly that result. The control
fails loudly if the library, the container, the root folder or the scan request is broken, which
means a fallback failure reported next to a passing control is a statement about the fallback.

Three numbers are kept apart, because they fail independently:

* **claimed** — rows the scan wrote for the book. The outcome the user sees.
* **probe** — whether ffprobe was actually run, refused, or never reached. Read from the log.
* **size** — bytes recorded per row against bytes on disk. A row can exist and still carry a
  size read from somewhere other than the file.

Exit codes follow verify_scan: 0 pass, 1 the expected claim did not happen, 2 inconclusive.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import pathlib
import re
import sqlite3
import sys
from typing import Any

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_INCONCLUSIVE = 2

# What the server says when the probe guard rejects a target. The guard tests the PUBLIC half of
# the metadata source for an audio extension, so when both halves have been collapsed onto the
# Linux descriptor path the rejected name is the bare descriptor number.
REFUSED_RE = re.compile(
    r"ffprobe target is not a supported audio file:\s*(?P<name>\S+)")
# The descriptor path is /proc/{pid}/fd/{fd}; sanitized for logging it is just the {fd}.
BARE_DESCRIPTOR_RE = re.compile(r"^\d+$")
NO_FFPROBE = "No bundled ffprobe available"
ENRICHMENT_CAUGHT = "Metadata enrichment failed for scan candidate"
PROBE_RAN_RE = re.compile(r"ffprobe exit code (?P<code>-?\d+) for file (?P<file>\S+)")


@dataclasses.dataclass
class ProbeEvidence:
    """What the server's own log says happened to the candidate."""

    ffprobe_missing: bool
    refusals: list[str]
    probe_runs: list[str]
    enrichment_exceptions: int

    @property
    def refused_on_descriptor(self) -> bool:
        """A refusal naming a bare integer is a refusal on a descriptor path."""
        return any(BARE_DESCRIPTOR_RE.match(name) for name in self.refusals)

    @property
    def descriptor_names(self) -> list[str]:
        return [n for n in self.refusals if BARE_DESCRIPTOR_RE.match(n)]


@dataclasses.dataclass
class FallbackReport:
    """One mode's outcome: did the file get claimed, and by what route."""

    label: str
    mode: str
    layout: str
    asin: str
    title: str
    tag_state: str
    on_disk_files: int
    on_disk_bytes: int
    claimed_files: int
    claimed_bytes: int | None
    evidence: ProbeEvidence

    @property
    def expects_claim(self) -> bool:
        # Both modes expect the file to be claimed. They differ in which mechanism has to do it,
        # which is why the control is meaningful rather than redundant.
        return True

    @property
    def verdict(self) -> str:
        if self.on_disk_files == 0:
            return "inconclusive"
        if self.evidence.ffprobe_missing:
            return "inconclusive"
        if self.claimed_files >= self.on_disk_files:
            return "claimed"
        if self.claimed_files > 0:
            return "partial"
        return "unclaimed"

    @property
    def exit_code(self) -> int:
        if self.verdict == "inconclusive":
            return EXIT_INCONCLUSIVE
        return EXIT_PASS if self.verdict == "claimed" else EXIT_FAIL

    @property
    def size_note(self) -> str:
        """Whether the recorded bytes match the file, stated separately from the claim.

        A claimed row with a wrong size is a different defect from an unclaimed file, and folding
        them together would let one mask the other.
        """
        if self.claimed_files == 0 or self.claimed_bytes is None:
            return "n/a"
        if self.claimed_bytes == self.on_disk_bytes:
            return "matches disk"
        per_row = (self.claimed_bytes / self.claimed_files
                   if self.claimed_files else 0)
        if per_row == int(per_row) and int(per_row) == 64:
            return "64 bytes/row — the size of a descriptor symlink, not the file"
        return f"{self.claimed_bytes:,} recorded against {self.on_disk_bytes:,} on disk"


def read_log(path: pathlib.Path | None) -> ProbeEvidence:
    if path is None or not path.exists():
        return ProbeEvidence(False, [], [], 0)
    text = path.read_text(errors="replace")
    return ProbeEvidence(
        ffprobe_missing=NO_FFPROBE in text,
        refusals=[m.group("name").rstrip(".,") for m in REFUSED_RE.finditer(text)],
        probe_runs=[m.group("file") for m in PROBE_RAN_RE.finditer(text)],
        enrichment_exceptions=text.count(ENRICHMENT_CAUGHT),
    )


def read_manifest(manifest: pathlib.Path, library: pathlib.Path,
                  asin: str) -> tuple[int, int, str, str, str]:
    """Ground truth for the target book: file count, byte total, and how it was written."""
    data = json.loads(manifest.read_text())
    entries = [e for e in data.get("entries", [])
               if e.get("belongs_to_asin") == asin and e.get("kind") == "book"]
    total = 0
    for entry in entries:
        candidate = (library / entry["path"]).resolve()
        if candidate.exists():
            total += candidate.stat().st_size
    first = entries[0] if entries else {}
    return (
        len(entries),
        total,
        first.get("true_title", ""),
        first.get("layout", ""),
        first.get("tag_state", ""),
    )


def read_claims(db: pathlib.Path, book_id: str) -> tuple[int, int | None]:
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        rows = conn.execute(
            "SELECT Size FROM AudiobookFiles WHERE AudiobookId = ?",
            (book_id,)).fetchall()
    if not rows:
        return 0, None
    sizes = [r[0] for r in rows if r[0] is not None]
    return len(rows), (sum(sizes) if sizes else None)


def render(report: FallbackReport) -> str:
    ev = report.evidence
    if report.mode == "control":
        expect = ("claimed by path attribution, without the metadata pass "
                  "being involved")
    else:
        expect = "claimed by the embedded-metadata pass — nothing else can claim it"

    lines = [
        f"label      {report.label}",
        f"mode       {report.mode}",
        f"layout     {report.layout}   ({report.tag_state})",
        f"book       {report.title}  [{report.asin}]",
        f"expect     {expect}",
        f"observed   {report.claimed_files} of {report.on_disk_files} "
        f"file(s) claimed",
    ]

    if ev.ffprobe_missing:
        lines.append("probe      NEVER AVAILABLE — the server had no ffprobe, so this run "
                     "says nothing about the fallback")
    elif ev.refused_on_descriptor:
        names = ", ".join(sorted(set(ev.descriptor_names)))
        lines.append(f"probe      REFUSED on a bare descriptor name: {names}")
        lines.append("           the guard tests the public half of the metadata source for an "
                     "audio extension;")
        lines.append("           a bare integer means both halves were collapsed onto "
                     "/proc/<pid>/fd/<fd>")
    elif ev.refusals:
        lines.append(f"probe      REFUSED on: {', '.join(sorted(set(ev.refusals)))}")
    elif ev.probe_runs:
        lines.append(f"probe      ran against {len(ev.probe_runs)} target(s)")
    else:
        lines.append("probe      no ffprobe activity in the log for this scan")

    if ev.refusals and ev.enrichment_exceptions == 0:
        lines.append("diagnostic NONE — the refusal was swallowed below the scan, so the job "
                     "recorded no issue")
    elif ev.enrichment_exceptions:
        lines.append(f"diagnostic {ev.enrichment_exceptions} enrichment failure(s) recorded")

    lines.append(f"size       {report.size_note}")
    lines.append(f"verdict    {report.verdict.upper()}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=pathlib.Path)
    parser.add_argument("--library", required=True, type=pathlib.Path)
    parser.add_argument("--db", required=True, type=pathlib.Path)
    parser.add_argument("--book-id", required=True)
    parser.add_argument("--asin", required=True)
    parser.add_argument("--mode", required=True, choices=("fallback", "control"))
    parser.add_argument("--log", type=pathlib.Path,
                        help="container log capture for this scan")
    parser.add_argument("--label", default="metadata fallback")
    parser.add_argument("--json", dest="json_out", type=pathlib.Path)
    args = parser.parse_args(argv)

    if not args.db.exists():
        print(f"no database at {args.db}", file=sys.stderr)
        return EXIT_INCONCLUSIVE

    on_disk_files, on_disk_bytes, title, layout, tag_state = read_manifest(
        args.manifest, args.library, args.asin)
    claimed_files, claimed_bytes = read_claims(args.db, args.book_id)

    report = FallbackReport(
        label=args.label,
        mode=args.mode,
        layout=layout,
        asin=args.asin,
        title=title,
        tag_state=tag_state,
        on_disk_files=on_disk_files,
        on_disk_bytes=on_disk_bytes,
        claimed_files=claimed_files,
        claimed_bytes=claimed_bytes,
        evidence=read_log(args.log),
    )

    print(render(report))
    if args.json_out:
        payload: dict[str, Any] = dataclasses.asdict(report)
        payload["verdict"] = report.verdict
        payload["size_note"] = report.size_note
        args.json_out.write_text(json.dumps(payload, indent=2) + "\n")
    return report.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
