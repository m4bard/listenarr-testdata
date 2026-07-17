#!/usr/bin/env python3
"""Diff two conformance runs and surface only what CHANGED between them.

A full conformance table answers "is this build correct". When you are reviewing a PR the more
useful question is "what did this change *do*" — which cases it fixed, and, the one that gates a
merge, which it *regressed*. Run the harness against the base image and the head image, capture
each as a `verify_scan --json` report, then:

    python3 tools/conformance_diff.py base.json head.json

It prints the regressions (pass -> fail) and the fixes (fail -> pass), and nothing about the
hundreds of cases that did not move. With --strict it exits non-zero if anything regressed, so a
PR that fixes one bug and quietly breaks another cannot pass review as "still green overall".

Both inputs are the JSON that `verify_scan.py --json` emits. An `inconclusive` report on either
side is not a diff — you cannot compare against a run that could not look — so it is refused
loudly rather than silently treated as all-fail.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
from dataclasses import dataclass, field
from typing import Any


class DiffError(RuntimeError):
    """The two runs cannot be meaningfully compared (e.g. one is inconclusive)."""


def _passed(verdict: str) -> bool:
    # Only "pass" is a pass. missing / fail / unexpected are all "not linked correctly", and for a
    # diff they collapse to the same "not passing" — what changed is pass-ness, not the flavour.
    return verdict == "pass"


def _index(report: dict[str, Any], side: str) -> dict[str, dict[str, Any]]:
    overall = report.get("summary", {}).get("overall")
    if overall == "inconclusive":
        raise DiffError(
            f"the {side} report is inconclusive ({report.get('error', 'no detail')}) — there is "
            "nothing to diff against a run that could not observe the scan"
        )
    return {r["path"]: r for r in report.get("results", [])}


@dataclass
class Diff:
    regressed: list[dict[str, Any]] = field(default_factory=list)  # pass -> not pass
    fixed: list[dict[str, Any]] = field(default_factory=list)      # not pass -> pass
    added: list[dict[str, Any]] = field(default_factory=list)      # only in head
    dropped: list[dict[str, Any]] = field(default_factory=list)    # only in base

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": {
                "regressed": len(self.regressed),
                "fixed": len(self.fixed),
                "added": len(self.added),
                "dropped": len(self.dropped),
                "verdict": "regressed" if self.regressed else "clean",
            },
            "regressed": self.regressed,
            "fixed": self.fixed,
            "added": self.added,
            "dropped": self.dropped,
        }


def diff_reports(base: dict[str, Any], head: dict[str, Any]) -> Diff:
    base_by_path = _index(base, "base")
    head_by_path = _index(head, "head")
    diff = Diff()

    for path, head_row in sorted(head_by_path.items()):
        base_row = base_by_path.get(path)
        if base_row is None:
            diff.added.append(head_row)
            continue
        was, now = _passed(base_row["verdict"]), _passed(head_row["verdict"])
        if was and not now:
            diff.regressed.append({**head_row, "was": base_row["verdict"]})
        elif not was and now:
            diff.fixed.append({**head_row, "was": base_row["verdict"]})

    for path, base_row in sorted(base_by_path.items()):
        if path not in head_by_path:
            diff.dropped.append(base_row)

    return diff


def print_diff(diff: Diff, base_label: str, head_label: str) -> None:
    print(f"conformance diff: {base_label} -> {head_label}\n")

    def section(title: str, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        print(f"{title} ({len(rows)}):")
        for row in rows:
            change = f"{row.get('was', '-')} -> {row['verdict']}"
            print(f"  [{change:>18}] {row.get('case', '-')}  {row['path']}")
            if row.get("why"):
                print(f"                       {row['why']}")
        print()

    section("REGRESSED", diff.regressed)
    section("fixed", diff.fixed)
    section("added (head only)", diff.added)
    section("dropped (base only)", diff.dropped)

    s = diff.to_dict()["summary"]
    print(f"summary: {s['regressed']} regressed, {s['fixed']} fixed, "
          f"{s['added']} added, {s['dropped']} dropped")
    if diff.regressed:
        print("VERDICT: regressed — the head run fails cases the base run passed.")
    else:
        print("VERDICT: clean — nothing that passed on base fails on head.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("base", type=pathlib.Path, help="verify_scan --json report for the base image")
    ap.add_argument("head", type=pathlib.Path, help="verify_scan --json report for the head image")
    ap.add_argument("--json", metavar="PATH",
                    help="write the diff as JSON to PATH ('-' for stdout, suppresses the text)")
    ap.add_argument("--strict", action="store_true",
                    help="exit non-zero if any case regressed (pass -> fail)")
    args = ap.parse_args()

    try:
        base = json.loads(args.base.read_text(encoding="utf-8"))
        head = json.loads(args.head.read_text(encoding="utf-8"))
        diff = diff_reports(base, head)
    except DiffError as exc:
        print(f"CANNOT DIFF: {exc}", file=sys.stderr)
        return 2

    json_to_stdout = args.json == "-"
    if args.json:
        text = json.dumps(diff.to_dict(), indent=2) + "\n"
        if json_to_stdout:
            print(text, end="")
        else:
            pathlib.Path(args.json).write_text(text)
    if not json_to_stdout:
        print_diff(diff, str(args.base), str(args.head))

    return 1 if (args.strict and diff.regressed) else 0


if __name__ == "__main__":
    raise SystemExit(main())
