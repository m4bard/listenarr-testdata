"""Does a generated library survive Audiobookshelf's own parser?

Listenarr can already lay out a library in the shape Audiobookshelf reads best, without any code
change, because `{Asin}` is an existing naming token. That makes the recommendation documentation
rather than a feature. Documentation is only worth writing if somebody checks it, though, and the
two ways it fails are both silent:

* an ASIN that is not exactly ten uppercase alphanumerics is not merely ignored, it stays glued to
  the title, so the book is titled `Some Book [b0015t963c]` and nothing anywhere errors
* a non-numeric series position stays in the title the same way and the sequence is lost, which is
  not a user typo but ordinary data

So this compares what Audiobookshelf's real parser extracts from each generated book directory
against the manifest, which is the answer key. Nothing here reimplements a regex: the parsing runs
inside `abs_parse_bridge.js` against a real Audiobookshelf checkout, so when ABS changes its rules
this check changes its answer instead of quietly agreeing with a stale copy.

The sidecar is judged the same way and for one specific reason. ABS's folder parser accepts only
digits for a sequence, while its `metadata.json` parser accepts any non-whitespace token, so the
sidecar is the only channel that can carry a position the folder cannot express. That is the
narrow job worth doing, not a general metadata dump.

Exit codes follow verify_scan: 0 pass, 1 a book did not survive the round trip, 2 inconclusive.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import pathlib
import shutil
import subprocess
import sys
from typing import Any

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_INCONCLUSIVE = 2

# The fields worth asserting. Narrators and subtitle are parsed by ABS too, but the layouts under
# test do not encode them, so demanding them would fail for the wrong reason.
COMPARED = ("title", "asin", "series", "sequence", "author")


@dataclasses.dataclass
class BookResult:
    """One generated book, as the manifest describes it and as Audiobookshelf reads it."""

    directory: str
    expected: dict[str, str | None]
    observed: dict[str, str | None]
    ignored: tuple[str, ...] = ()

    @property
    def mismatches(self) -> list[str]:
        out = []
        for field in COMPARED:
            if field in self.ignored:
                continue
            want, got = self.expected.get(field), self.observed.get(field)
            # A book with no series cannot be faulted for ABS not finding one.
            if want in (None, "") and got in (None, ""):
                continue
            if want != got:
                out.append(field)
        return out

    @property
    def survived(self) -> bool:
        return not self.mismatches


def book_directories(manifest: pathlib.Path) -> list[dict[str, Any]]:
    """One entry per distinct book directory, with the truth the manifest records for it."""
    data = json.loads(manifest.read_text())
    seen: dict[str, dict[str, Any]] = {}
    for entry in data.get("entries", []):
        if entry.get("kind") != "book":
            continue
        rel = pathlib.PurePosixPath(entry["path"])
        directory = str(rel.parent)
        if directory in ("", "."):
            # A loose file at the library root has no book directory for ABS to read.
            continue
        if directory in seen:
            continue
        authors = entry.get("true_authors") or []
        seen[directory] = {
            "directory": directory,
            "title": entry.get("true_title"),
            "asin": entry.get("belongs_to_asin"),
            "series": entry.get("true_series"),
            "sequence": entry.get("true_series_position"),
            "author": authors[0] if authors else None,
        }
    return list(seen.values())


def run_bridge(abs_repo: pathlib.Path, dirs: list[str],
               sidecars: list[dict[str, str]]) -> dict[str, Any]:
    node = shutil.which("node")
    if not node:
        raise SystemExit("node is required to run Audiobookshelf's parser")
    bridge = pathlib.Path(__file__).resolve().parent / "abs_parse_bridge.js"
    payload = json.dumps({
        "absRepo": str(abs_repo.resolve()),
        "dirs": dirs,
        "sidecars": sidecars,
    })
    proc = subprocess.run(
        [node, str(bridge)],
        input=payload, capture_output=True, text=True, timeout=120)
    if not proc.stdout.strip():
        raise SystemExit(
            f"the Audiobookshelf bridge produced no output (exit {proc.returncode}): "
            f"{proc.stderr.strip()[:400]}")
    return json.loads(proc.stdout)


def render(results: list[BookResult], stubbed: list[str], label: str,
           sidecars: list[dict[str, Any]]) -> str:
    survived = sum(1 for r in results if r.survived)
    lines = [
        f"label      {label}",
        f"books      {len(results)} directories read by Audiobookshelf's own parser",
        f"survived   {survived} of {len(results)} round-tripped intact",
    ]
    if results and results[0].ignored:
        lines.append("ignored    " + ", ".join(results[0].ignored)
                     + "  (this layout does not claim to encode them)")
    lines += [
    ]
    for result in results:
        if result.survived:
            continue
        lines.append(f"  MISMATCH {result.directory}")
        # Only claim the leftover-text mechanism when the title actually shows it. A layout that
        # never encoded an ASIN in the first place fails for a duller reason, and saying otherwise
        # would put a mechanism in the report that did not happen.
        title_polluted = result.expected.get("title") != result.observed.get("title")
        for field in result.mismatches:
            want = result.expected.get(field)
            got = result.observed.get(field)
            lines.append(f"    {field:9s} manifest={want!r}  audiobookshelf={got!r}")
            if field == "title":
                lines.append("              the title did not survive, so something the parser "
                             "declined to strip is still attached to it")
            elif got in (None, "") and want and not title_polluted:
                lines.append(f"              the folder carries no {field} for ABS to read; it "
                             f"would have to come from a sidecar or a match")

    for entry in sidecars:
        label = entry["label"]
        if entry.get("error"):
            lines.append(f"sidecar    {label}: ERRORED, {entry['error']}")
            continue
        parsed = entry.get("parsed")
        if parsed is None:
            lines.append(f"sidecar    {label}: REJECTED outright by the schema validator")
            continue
        series = [s for s in (entry.get("series") or []) if s]
        offered, kept = entry.get("offeredSeries", 0), entry.get("keptSeries", 0)
        rendered = ", ".join(f"{s.get('name')!r} #{s.get('sequence')!r}" for s in series)
        if offered and not kept:
            # The failure worth catching: the file is accepted, so nothing looks wrong, and the
            # series is thrown away on the way through.
            lines.append(f"sidecar    {label}: ACCEPTED but all {offered} series entries were "
                          "discarded, which is silent")
        elif series:
            lines.append(f"sidecar    {label}: accepted, series {rendered}")
        else:
            lines.append(f"sidecar    {label}: accepted, no series offered")

    if stubbed:
        lines.append(f"note       stubbed absent packages: {', '.join(stubbed)}")
    lines.append(f"verdict    {'PASS' if survived == len(results) else 'FAIL'}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=pathlib.Path)
    parser.add_argument("--abs-repo", required=True, type=pathlib.Path,
                        help="path to an Audiobookshelf checkout")
    parser.add_argument("--sidecar", action="append", default=[], metavar="LABEL=PATH",
                        help="also run a metadata.json through ABS's schema validator")
    parser.add_argument("--ignore", default="",
                        help="comma-separated fields the layout does not claim to encode, so a "
                             "miss is not a defect. The flat shape carries no series, for "
                             "instance. Stated per run rather than inferred, so a layout cannot "
                             "quietly excuse itself from a field it should have carried.")
    parser.add_argument("--label", default="audiobookshelf conformance")
    parser.add_argument("--json", dest="json_out", type=pathlib.Path)
    args = parser.parse_args(argv)

    if not args.manifest.exists():
        print(f"no manifest at {args.manifest}", file=sys.stderr)
        return EXIT_INCONCLUSIVE
    if not (args.abs_repo / "server" / "utils" / "scandir.js").exists():
        print(f"{args.abs_repo} does not look like an Audiobookshelf checkout "
              f"(no server/utils/scandir.js)", file=sys.stderr)
        return EXIT_INCONCLUSIVE

    ignored = tuple(f.strip() for f in args.ignore.split(",") if f.strip())
    unknown = [f for f in ignored if f not in COMPARED]
    if unknown:
        print(f"--ignore names fields this check does not compare: {unknown}", file=sys.stderr)
        return EXIT_INCONCLUSIVE

    books = book_directories(args.manifest)
    if not books:
        print("the manifest describes no book directories, so there is nothing to judge",
              file=sys.stderr)
        return EXIT_INCONCLUSIVE

    sidecar_inputs = []
    for spec in args.sidecar:
        label, _, path = spec.partition("=")
        sidecar_inputs.append({"label": label, "json": pathlib.Path(path).read_text()})

    payload = run_bridge(args.abs_repo, [b["directory"] for b in books], sidecar_inputs)
    if not payload.get("ok"):
        print(payload.get("error", "the Audiobookshelf bridge failed"), file=sys.stderr)
        return EXIT_INCONCLUSIVE

    by_dir = {d["dir"]: d for d in payload.get("dirs", [])}
    results = []
    for book in books:
        got = by_dir.get(book["directory"], {})
        results.append(BookResult(
            directory=book["directory"],
            expected={k: book[k] for k in COMPARED},
            observed={
                "title": got.get("title"),
                "asin": got.get("asin"),
                "series": got.get("seriesName"),
                "sequence": got.get("seriesSequence"),
                "author": (got.get("authors") or [None])[0],
            },
            ignored=ignored,
        ))

    print(render(results, payload.get("stubbed", []), args.label, payload.get("sidecars", [])))
    if args.json_out:
        args.json_out.write_text(json.dumps({
            "label": args.label,
            "books": [dataclasses.asdict(r) | {"survived": r.survived,
                                               "mismatches": r.mismatches}
                      for r in results],
            "sidecars": payload.get("sidecars", []),
            "stubbed": payload.get("stubbed", []),
        }, indent=2) + "\n")

    return EXIT_PASS if all(r.survived for r in results) else EXIT_FAIL


if __name__ == "__main__":
    raise SystemExit(main())
