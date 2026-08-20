#!/usr/bin/env python3
"""Judge how an unmatched scan grouped a multi-file book's files.

One book that happens to be stored as many files is still one book. So for every book
folder in the generated library, a correct Library Import produces exactly ONE scan item
covering every audio file in that folder. Anything else is either an explosion (one item
per file, so the operator is asked to identify the same book dozens of times) or a partial
grouping (some files gathered, some stranded).

The verdicts are deliberately separate from "did the scan see the files at all". A scan
that indexes nothing looks identical to a scan that grouped nothing if you only count
items, and Listenarr#822 is exactly that failure. So coverage is checked first and a
shortfall is reported as INCONCLUSIVE rather than as a grouping fault.

The control matters as much as the subjects. The check runs the same book, the same
layout and the same tag state through several filename conventions; at least one of them
(the control) is a convention the grouper already handles. If the control does not group,
this run is measuring "multi-file books never group here" and says nothing about any
individual convention, so the verdict is INCONCLUSIVE and not a pass.

Exit codes: 0 every case matched its expectation, 1 a case did not, 2 inconclusive.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any

GROUPED = "grouped"
EXPLODED = "exploded"
PARTIAL = "partial"
MISSING = "missing"

EXIT_PASS, EXIT_FAIL, EXIT_INCONCLUSIVE = 0, 1, 2


def case_of(container_path: str, container_root: str) -> str:
    """The top-level directory under the library root, which names the case."""
    normalized = container_path.replace("\\", "/")
    root = container_root.replace("\\", "/").rstrip("/")
    if not normalized.startswith(root + "/"):
        return ""
    return normalized[len(root) + 1:].split("/")[0]


def load_cases(library: pathlib.Path) -> dict[str, dict[str, Any]]:
    """Read every per-case manifest and record the truth about what is on disk."""
    cases: dict[str, dict[str, Any]] = {}
    for manifest_path in sorted(library.glob("*/manifest.json")):
        name = manifest_path.parent.name
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        entries = [e for e in manifest.get("entries", []) if e.get("kind") == "book"]
        if not entries:
            continue
        cases[name] = {
            "case": name,
            "structure": entries[0].get("structure", "?"),
            "tag_state": entries[0].get("tag_state", "?"),
            "title_tags": title_tags(entries),
            "files_on_disk": len(entries),
            "example": pathlib.PurePosixPath(entries[0]["path"]).name,
        }
    return cases


def title_tags(entries: list[dict[str, Any]]) -> str:
    """What the title tags say across a book's files — which is what grouping can use.

    Three states behave differently and the tag-state key alone does not separate them:
    no title tag at all, one shared book title, or a different title per file. The last is
    ordinary for a chapter split and carries no information about which files are one book.
    """
    titles = {(e.get("tags_written") or {}).get("title") for e in entries}
    titles.discard(None)
    if not titles:
        return "none"
    return "book title" if len(titles) == 1 else "per-chapter"


def judge(case: dict[str, Any], items: list[dict[str, Any]]) -> dict[str, Any]:
    """Classify one case from the scan items attributed to it."""
    on_disk = case["files_on_disk"]
    claimed: set[str] = set()
    for item in items:
        sources = item.get("sourceFiles") or []
        if not sources and item.get("fullPath"):
            sources = [item["fullPath"]]
        claimed.update(sources)

    result = dict(case)
    result["scan_items"] = len(items)
    result["files_claimed"] = len(claimed)

    if not items:
        result["observed"] = MISSING
    elif len(items) == 1:
        result["observed"] = GROUPED if len(claimed) == on_disk else PARTIAL
    elif len(items) == on_disk:
        result["observed"] = EXPLODED
    else:
        result["observed"] = PARTIAL
    # One book is one scan item, whatever its files are called. That is the expectation
    # for every case here; only the filename convention differs between them.
    result["expected"] = GROUPED
    result["pass"] = result["observed"] == GROUPED
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--library", type=pathlib.Path, required=True,
                    help="host path of the generated library root (holds one dir per case)")
    ap.add_argument("--items", type=pathlib.Path, required=True,
                    help="JSON file holding the unmatched-scan 'items' array")
    ap.add_argument("--container-root", default="/audiobooks",
                    help="library root as the container sees it (default: /audiobooks)")
    ap.add_argument("--control", action="append", default=None, metavar="CASE",
                    help="case name whose grouping is already expected to work; if it "
                         "does not, the whole run is inconclusive. Repeatable.")
    ap.add_argument("--label", default="", help="label for the report header")
    ap.add_argument("--json", dest="json_out", help="also write the result as JSON")
    args = ap.parse_args()

    cases = load_cases(args.library)
    if not cases:
        print("INCONCLUSIVE: no per-case manifests under "
              f"{args.library} — nothing was generated to judge.")
        return EXIT_INCONCLUSIVE

    items = json.loads(args.items.read_text(encoding="utf-8"))
    if isinstance(items, dict):
        items = items.get("items", [])

    by_case: dict[str, list[dict[str, Any]]] = {name: [] for name in cases}
    unattributed = 0
    for item in items:
        anchor = (item.get("bookFolder")
                  or (item.get("sourceFiles") or [None])[0]
                  or item.get("fullPath") or "")
        name = case_of(anchor, args.container_root)
        if name in by_case:
            by_case[name].append(item)
        else:
            unattributed += 1

    results = [judge(cases[name], by_case[name]) for name in sorted(cases)]

    width = max(len(r["case"]) for r in results)
    print(f"label      {args.label or args.library}")
    print(f"scan items {len(items)} total"
          + (f" ({unattributed} outside the cases)" if unattributed else ""))
    print()
    header = (f"{'case'.ljust(width)}  {'structure':<12} {'title tags':<12} "
              f"{'files':>5} {'items':>5} {'claimed':>7}  outcome")
    print(header)
    print("-" * len(header))
    for r in results:
        mark = "OK  " if r["pass"] else "FAIL"
        control = " (control)" if args.control and r["case"] in args.control else ""
        print(f"{r['case'].ljust(width)}  {r['structure']:<12} {r['title_tags']:<12} "
              f"{r['files_on_disk']:>5} {r['scan_items']:>5} {r['files_claimed']:>7}  "
              f"{mark} {r['observed']}{control}")
    print("-" * len(header))
    for r in results:
        print(f"  {r['case']}: filenames look like {r['example']!r}")

    # Coverage first. A scan that indexed nothing (Listenarr#822) produces zero items and
    # would otherwise read as a total grouping failure, which is a different bug entirely.
    total_on_disk = sum(r["files_on_disk"] for r in results)
    total_claimed = sum(r["files_claimed"] for r in results)
    verdict = EXIT_PASS
    if total_claimed == 0:
        print("\nINCONCLUSIVE: the scan claimed no files at all. Nothing was indexed, so "
              "there is no grouping to judge (compare Listenarr#822).")
        verdict = EXIT_INCONCLUSIVE
    elif total_claimed < total_on_disk:
        print(f"\nINCONCLUSIVE: the scan accounted for {total_claimed} of {total_on_disk} "
              "audio files on disk. Files went missing before grouping, so a low item "
              "count would not be evidence about grouping.")
        verdict = EXIT_INCONCLUSIVE
    else:
        controls = [r for r in results if args.control and r["case"] in args.control]
        broken_controls = [r for r in controls if not r["pass"]]
        if args.control and not controls:
            print(f"\nINCONCLUSIVE: no case matched --control {args.control}.")
            verdict = EXIT_INCONCLUSIVE
        elif broken_controls:
            names = ", ".join(r["case"] for r in broken_controls)
            print(f"\nINCONCLUSIVE: the control case(s) {names} did not group either. "
                  "This run shows that multi-file books do not group here at all, which "
                  "is not evidence about any one filename convention.")
            verdict = EXIT_INCONCLUSIVE
        else:
            failed = [r for r in results if not r["pass"]]
            if failed:
                names = ", ".join(f"{r['case']} ({r['observed']})" for r in failed)
                print(f"\nFAIL: {len(failed)} of {len(results)} cases did not group into "
                      f"one scan item: {names}.")
                if controls:
                    kept = ", ".join(r["case"] for r in controls)
                    print(f"      The control(s) {kept} did group, so the difference is "
                          "the filename convention and not multi-file books as such.")
                verdict = EXIT_FAIL
            else:
                print(f"\nPASS: all {len(results)} cases grouped into one scan item each.")

    if args.json_out:
        pathlib.Path(args.json_out).write_text(
            json.dumps({
                "label": args.label,
                "container_root": args.container_root,
                "controls": args.control or [],
                "scan_items": len(items),
                "unattributed_items": unattributed,
                "files_on_disk": total_on_disk,
                "files_claimed": total_claimed,
                "verdict": {EXIT_PASS: "pass", EXIT_FAIL: "fail",
                            EXIT_INCONCLUSIVE: "inconclusive"}[verdict],
                "cases": results,
            }, indent=2) + "\n", encoding="utf-8")

    return verdict


if __name__ == "__main__":
    sys.exit(main())
