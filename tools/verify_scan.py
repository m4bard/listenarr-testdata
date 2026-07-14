#!/usr/bin/env python3
"""Turn a generated library into a conformance result: expected outcome vs observed.

    # 1. what SHOULD happen — the answer key, written by generate_library.py
    python3 tools/verify_scan.py --manifest build/mixed/manifest.json --db listenarr.db

    # 2. what a rename MUST NOT do — the destructive check
    python3 tools/verify_scan.py --manifest build/hz/manifest.json --snapshot before.json
    #   ... point Listenarr's renamer at the library ...
    python3 tools/verify_scan.py --manifest build/hz/manifest.json --audit before.json

The manifest records, for every file, the book it actually belongs to. This reads what
Listenarr concluded and prints the difference. That is the whole idea: a generated library
is fixtures, a generated library plus an answer key is evidence.

Observations come from whichever source is available:

  --db PATH      the SQLite database (Audiobooks, AudiobookFiles). No server needed.
  --api URL      a running instance, via /api/v1/library.
  --observed F   a JSON list of {path, asin} someone else produced.

Paths inside a container are not paths on the host, so --root-map rewrites the prefix:

    --root-map /audiobooks=./build/mixed
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import pathlib
import sqlite3
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

# Clutter that is audio, reaches the candidate set, and must still not be attached to a book.
# The sidecars and cover art are not audio at all and are excluded from the link check; they
# are read by the parser, which is a different question from being linked.
AUDIO_CLUTTER = {
    "sample-track", "intro-outro", "bonus-content", "zero-byte", "corrupt-audio",
    "os-detritus",
}


@dataclass
class Observation:
    """What Listenarr concluded about one file on disk."""

    path: str
    asin: str | None = None
    title: str | None = None
    book_id: int | None = None
    base_path: str | None = None


@dataclass
class Result:
    entry: dict[str, Any]
    observed: Observation | None
    verdict: str          # pass | fail | missing | unexpected
    why: str = ""


@dataclass
class Report:
    results: list[Result] = field(default_factory=list)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.verdict == "pass")

    @property
    def failed(self) -> int:
        return len(self.results) - self.passed


# --------------------------------------------------------------------------
# Sources of observation
# --------------------------------------------------------------------------

def from_sqlite(db: pathlib.Path) -> list[Observation]:
    """Read what the scan concluded, straight out of the SQLite database.

    Schema, as of the checkout this was written against: Audiobooks(Id, Title, Asin,
    BasePath) and AudiobookFiles(AudiobookId, Path). A file with no row in AudiobookFiles
    was never linked to anything, which is itself an observation — and for most of the
    interesting scenarios it is THE observation.
    """
    connection = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT f.Path      AS path,
                   b.Id        AS book_id,
                   b.Asin      AS asin,
                   b.Title     AS title,
                   b.BasePath  AS base_path
            FROM AudiobookFiles f
            LEFT JOIN Audiobooks b ON b.Id = f.AudiobookId
            """
        ).fetchall()
    except sqlite3.OperationalError as exc:
        sys.exit(
            f"FATAL: cannot read {db}: {exc}\n"
            "Expected tables Audiobooks and AudiobookFiles. If the schema has moved, this "
            "query is the only thing that needs to change."
        )
    finally:
        connection.close()

    return [
        Observation(
            path=row["path"],
            asin=row["asin"],
            title=row["title"],
            book_id=row["book_id"],
            base_path=row["base_path"],
        )
        for row in rows
        if row["path"]
    ]


def from_api(base_url: str, api_key: str | None) -> list[Observation]:
    """Read the same thing from a running instance via /api/v1/library."""
    def get(path: str) -> Any:
        request = urllib.request.Request(f"{base_url.rstrip('/')}{path}")
        if api_key:
            request.add_header("X-Api-Key", api_key)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            sys.exit(f"FATAL: GET {path} -> HTTP {exc.code}")
        except OSError as exc:
            sys.exit(f"FATAL: GET {path} -> {exc}")

    observations: list[Observation] = []
    books = get("/api/v1/library")
    if isinstance(books, dict):
        books = books.get("items") or books.get("records") or []

    for entry in books:
        book_id = entry.get("id")
        files = entry.get("files")
        if files is None and book_id is not None:
            files = get(f"/api/v1/library/{book_id}/files-debug") or []
        for audio in files or []:
            path = audio.get("path") if isinstance(audio, dict) else audio
            if not path:
                continue
            observations.append(
                Observation(
                    path=path,
                    asin=entry.get("asin"),
                    title=entry.get("title"),
                    book_id=book_id,
                    base_path=entry.get("basePath"),
                )
            )
    return observations


def from_observed(path: pathlib.Path) -> list[Observation]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return [Observation(**row) for row in raw]


# --------------------------------------------------------------------------
# Comparison
# --------------------------------------------------------------------------

def normalize(path: str, root_map: tuple[str, str] | None) -> str:
    """Rewrite an observed path so it can be compared with a manifest path."""
    if root_map:
        remote, local = root_map
        if path.startswith(remote):
            path = str(pathlib.Path(local) / path[len(remote):].lstrip("/"))
    return str(pathlib.Path(path))


def compare(
    manifest: dict[str, Any],
    observations: list[Observation],
    library_root: pathlib.Path,
    root_map: tuple[str, str] | None,
) -> Report:
    """Expected outcome vs observed, one row per generated file."""
    by_path: dict[str, Observation] = {}
    for observation in observations:
        by_path[normalize(observation.path, root_map)] = observation

    report = Report()
    for entry in manifest["entries"]:
        if entry["kind"] == "clutter" and entry.get("clutter_kind") not in AUDIO_CLUTTER:
            continue  # not audio: never a scan candidate, so there is nothing to link

        absolute = str(library_root / entry["path"])
        observed = by_path.get(absolute) or by_path.get(entry["path"])
        expected = entry["expect_linked_asin"]

        if expected is None:
            # Clutter. A correct scanner attaches none of it to any book.
            if observed is None:
                report.results.append(Result(entry, None, "pass"))
            else:
                report.results.append(Result(
                    entry, observed, "fail",
                    f"attached to {observed.asin or observed.title!r}, but this file is "
                    f"{entry['clutter_kind']} — not the book",
                ))
            continue

        if observed is None:
            report.results.append(Result(
                entry, None, "missing",
                "never linked to any book — the file was scanned and dropped, or never "
                "discovered at all",
            ))
            continue

        # A correct scanner links this file to the book it actually belongs to, whatever the
        # tags claim. Prefer the ASIN; fall back to the title when the record carries none,
        # which is the common case for a library that was never Audible-tagged.
        if observed.asin:
            ok = observed.asin == expected
            why = "" if ok else (
                f"linked to {observed.asin} ({observed.title!r}), expected {expected} "
                f"({entry['true_title']!r})"
            )
        else:
            ok = (observed.title or "").strip().lower() == entry["true_title"].strip().lower()
            why = "" if ok else (
                f"linked to {observed.title!r}, expected {entry['true_title']!r}"
            )
        report.results.append(Result(entry, observed, "pass" if ok else "fail", why))

    return report


def check_base_paths(report: Report, library_root: pathlib.Path) -> list[str]:
    """A book's BasePath must be the book's own folder, never a parent that holds siblings.

    The multi-disc case is what this is for: the files live in per-disc subfolders and share
    no direct parent, so the common-parent walk has to climb — and climbing one level too far
    makes BasePath the AUTHOR folder, which swallows every other book by that author.
    """
    problems: list[str] = []
    owners: dict[str, set[str]] = collections.defaultdict(set)
    for result in report.results:
        if result.observed and result.observed.base_path:
            owners[result.observed.base_path].add(str(result.entry["belongs_to_asin"]))

    for base_path, asins in owners.items():
        if len(asins) > 1:
            problems.append(
                f"BasePath {base_path!r} is shared by {len(asins)} books "
                f"({', '.join(sorted(asins))}) — it climbed past the book folder and "
                "swallowed a sibling"
            )
        if pathlib.Path(base_path) == library_root:
            problems.append(
                f"BasePath {base_path!r} IS the library root — every scan will fall back to "
                "walking the entire library"
            )
    return problems


# --------------------------------------------------------------------------
# The destructive check: a rename must not lose a file or escape the root
# --------------------------------------------------------------------------

def digest(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def snapshot(library_root: pathlib.Path) -> dict[str, Any]:
    """Inventory every file by content hash, so a rename can be audited afterwards."""
    files: dict[str, list[str]] = collections.defaultdict(list)
    for path in sorted(library_root.rglob("*")):
        if path.is_file() and path.name != "manifest.json":
            files[digest(path)].append(str(path.relative_to(library_root)))
    return {"root": str(library_root), "files": files}


def audit(before: dict[str, Any], library_root: pathlib.Path) -> list[str]:
    """After a rename: assert NO file was lost and NO path escaped the library root.

    A matching bug leaves a file unlinked, which is annoying. A renaming bug loses data,
    which is unforgivable — and the two are one line apart in the same code path. Files are
    tracked by CONTENT, not by name, because a rename is precisely a change of name: the
    question is not "is this path still here" but "does every byte we had still exist
    somewhere under the root".
    """
    problems: list[str] = []
    root = library_root.resolve()
    after = snapshot(library_root)

    # Counts per hash, not mere presence. Two untagged files are byte-identical, so a rename
    # that clobbered one of them with the other would leave the hash present and this check
    # would report clean while a file was, in fact, destroyed.
    for content, was in sorted(before["files"].items()):
        still = after["files"].get(content, [])
        if len(still) < len(was):
            lost = len(was) - len(still)
            problems.append(
                f"DATA LOSS: {lost} of {len(was)} copies of {was[0]!r} no longer exist "
                f"anywhere under the root"
            )

    for path in library_root.rglob("*"):
        if path.is_symlink():
            target = path.resolve()
            if not target.is_relative_to(root):
                problems.append(f"ESCAPE: {path} points outside the library root, at {target}")
        if not path.resolve().is_relative_to(root):
            problems.append(f"ESCAPE: {path} resolved outside the library root")

    return problems


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------

def print_table(report: Report, verbose: bool) -> None:
    """Group by the axes, because a failure is only interesting as a pattern."""
    groups: dict[tuple[str, str], list[Result]] = collections.defaultdict(list)
    for result in report.results:
        groups[(result.entry.get("layout", "-"),
                result.entry.get("tag_state", result.entry.get("clutter_kind", "-")))].append(
            result
        )

    print(f"{'layout':<26} {'case':<26} {'pass':>5} {'fail':>5}  outcome")
    print("-" * 100)
    for (layout, case), results in sorted(groups.items()):
        passed = sum(1 for r in results if r.verdict == "pass")
        failed = len(results) - passed
        expect = results[0].entry.get("expect", "")
        mark = "ok " if failed == 0 else "FAIL"
        print(f"{layout:<26} {case:<26} {passed:>5} {failed:>5}  {mark}  {expect[:36]}")

    print("-" * 100)
    print(f"{'TOTAL':<26} {'':<26} {report.passed:>5} {report.failed:>5}")

    if verbose:
        print("\nfailures:")
        for result in report.results:
            if result.verdict != "pass":
                print(f"  [{result.verdict}] {result.entry['path']}")
                if result.why:
                    print(f"           {result.why}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--manifest", type=pathlib.Path, required=True,
                    help="manifest.json from generate_library.py")
    ap.add_argument("--db", type=pathlib.Path, help="Listenarr's SQLite database")
    ap.add_argument("--api", help="base URL of a running instance, e.g. http://localhost:8080")
    ap.add_argument("--api-key", help="X-Api-Key for --api")
    ap.add_argument("--observed", type=pathlib.Path, help="a JSON list of {path, asin}")
    ap.add_argument("--root-map", help="REMOTE=LOCAL prefix rewrite, e.g. /audiobooks=./build/lib")
    ap.add_argument("--snapshot", type=pathlib.Path,
                    help="write a pre-rename inventory to this file and exit")
    ap.add_argument("--audit", type=pathlib.Path,
                    help="compare against a --snapshot: assert no file lost, no path escaped")
    ap.add_argument("--verbose", action="store_true", help="list every failure")
    ap.add_argument("--strict", action="store_true", help="exit non-zero if any case fails")
    args = ap.parse_args()

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    library_root = args.manifest.parent

    if args.snapshot:
        args.snapshot.write_text(json.dumps(snapshot(library_root), indent=2) + "\n")
        count = sum(len(v) for v in snapshot(library_root)["files"].values())
        print(f"snapshot: {count} files under {library_root} -> {args.snapshot}")
        return 0

    if args.audit:
        before = json.loads(args.audit.read_text(encoding="utf-8"))
        problems = audit(before, library_root)
        if problems:
            print(f"RENAME AUDIT FAILED — {len(problems)} problem(s):")
            for problem in problems:
                print(f"  {problem}")
            return 1
        total = sum(len(v) for v in before["files"].values())
        print(f"rename audit OK — all {total} files still exist under the root; "
              "no path escaped.")
        return 0

    root_map: tuple[str, str] | None = None
    if args.root_map:
        if "=" not in args.root_map:
            ap.error("--root-map must be REMOTE=LOCAL")
        remote, local = args.root_map.split("=", 1)
        root_map = (remote, local)

    if args.db:
        observations = from_sqlite(args.db)
    elif args.api:
        observations = from_api(args.api, args.api_key)
    elif args.observed:
        observations = from_observed(args.observed)
    else:
        ap.error("one of --db, --api or --observed is required")

    print(f"scenario   {manifest['scenario']}")
    print(f"expect     {manifest['expect']}")
    print(f"observed   {len(observations)} linked files\n")

    report = compare(manifest, observations, library_root, root_map)
    print_table(report, args.verbose)

    for problem in check_base_paths(report, library_root):
        print(f"\nBASEPATH: {problem}")

    return 1 if (args.strict and report.failed) else 0


if __name__ == "__main__":
    raise SystemExit(main())
