#!/usr/bin/env python3
"""Reproduce three Listenarr release-selection defects on a stock instance, with their controls.

What this demonstrates
----------------------

B2, the profile's quality ORDERING never reaches release selection. Scoring turns a quality into a
number with a hardcoded ladder and consults the operator's profile only afterwards, for a flat
allowed / not-allowed veto. Reordering a profile therefore changes nothing. Worse, the ladder gives
every AAC rung the same number while putting MP3 320kbps above all of them, which inverts the order
the shipped default profile itself sets.

B3, for a result classified as NZB the minimum and maximum size gate, the quality deduction and the
allowed-quality veto all sit inside one if (!isNzb) block and never run. Every NZB scores 100,
so NZB outranks torrent structurally whatever it contains, and a quality the operator switched off
downloads over Usenet with no rejection reason.

B4, the score is capped at 100 and starts at 100, so an operator preference lands on a number that
is about to be truncated away. Two releases that the profile ranks differently come back with the
same score, and the grab then falls to whatever order the indexer answered in.

The controls, and what each one proves
--------------------------------------

B2 control, a profile edit that has to change the answer. The same profile as the ordering test,
with one quality refused instead of reordered. If the scores move, the endpoint is reading the
profile, so the null result in the ordering test is about the ordering specifically rather than
about a profile that never arrived.

B3 control 1, a gate that genuinely is protocol-specific. Minimum seeders, against releases that
have none. It has to reject the torrent and pass the NZB. That is the shape a deliberate protocol
exemption has, and it proves the apparatus can tell the two protocols apart.

B3 control 2, a gate that is not protocol-conditional. A forbidden word in the title. It has to
reject BOTH protocols. This is the control that must come out differently from the findings: it
rules out the alternative reading, that NZB results never reach the scorer or that the scorer
cannot reject one, which would make every B3 result an artifact.

B4 control, the identical preference scored on a rung far enough down the ladder that the cap was
never in reach. It has to separate the two releases. That rules out the alternative reading, that
preferred words and the seeder bonus never reach the score at all, which would make the tie at the
top an artifact rather than the cap.

The apparatus
-------------

POST /api/v1/qualityprofile/{id}/score calls QualityProfileService.ScoreSearchResults, the
same method DownloadService and AutomaticSearchService call to pick a release. Posting the
releases directly removes the indexer and the torznab response parser from between the input and
the code under test, which is what makes the releases a controlled variable.

Every profile below is created with isDefault false on purpose. The service injects its
required qualities only into default profiles, so a non-default profile keeps exactly the ordering
it was given.

Not covered here: how SearchResult.Quality gets populated in the first place, and the separate
measurement where a result recognised as Usenet only through its indexer's type takes the size gate
but skips the quality gates. Both need an indexer configured, and this tool deliberately needs none.

Exit codes
----------

0, every finding reproduced and every control discriminated.
1, a finding did not reproduce. That is what a fix landing looks like, so this doubles as a
   regression check.
2, a control did not discriminate. The run proves nothing and the findings are void.
3, the apparatus could not be set up.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

PROBE = Path(__file__).resolve().parent / "probe.sh"
MB = 1024 * 1024

FINDING = "finding"
CONTROL = "control"

EXIT_OK = 0
EXIT_FINDING_GONE = 1
EXIT_CONTROL_INERT = 2
EXIT_APPARATUS = 3


class ApparatusError(RuntimeError):
    """The instance or the endpoint did not behave well enough for a result to mean anything."""


@dataclass(frozen=True)
class Row:
    """One scored release, as the endpoint came back with it."""

    rid: str
    quality: str
    protocol: str
    size_mb: int
    score: int
    rejected: bool
    reasons: str

    def outcome(self) -> tuple[int, bool]:
        """The part of a row a profile edit is expected to move."""
        return self.score, self.rejected


@dataclass(frozen=True)
class Check:
    """One finding or one control, and whether it came out the way the report says it does."""

    key: str
    kind: str
    claim: str
    held: bool
    note: str


def run_probe(args: Sequence[str]) -> str:
    """Call tools/probe.sh, which owns every rule about not colliding with a live instance."""
    proc = subprocess.run(
        [str(PROBE), *args], capture_output=True, text=True, check=False
    )
    if proc.returncode != 0:
        detail = (proc.stderr.strip() or proc.stdout.strip() or "no output").splitlines()[-1]
        raise ApparatusError(f"probe.sh {args[0]} failed: {detail}")
    return proc.stdout


def api(name: str, method: str, path: str, body: object = None) -> Any:
    args = ["api", name, method, path]
    if body is not None:
        args.append(json.dumps(body))
    return json.loads(run_probe(args) or "null")


def profile(
    name: str,
    rungs: Sequence[tuple[str, bool]],
    *,
    minimum_size_mb: int = 0,
    maximum_size_mb: int = 0,
    minimum_seeders: int = 0,
    forbidden: Sequence[str] = (),
    preferred_words: Sequence[str] = (),
) -> dict[str, object]:
    """A profile whose ordering is the order of rungs, with everything else neutralised.

    Neutralised on purpose, so that a torrent and an NZB can only score differently because of the
    protocol guards under test:

    preferredLanguages empty, or a missing language costs a torrent points and an NZB nothing.
    preferredFormats empty, for the same reason, and because a preferred format silently widens the
    allowed-quality set.
    preferredWords empty unless a caller asks for them, because every matched word is worth 5 and
    that is the term B4 measures.
    minimumSeeders zero, or every zero-seeder torrent is rejected before anything else is measured.
    maximumAge zero and no published date, so neither side takes an age penalty.
    """
    allowed = [rung for rung, ok in rungs if ok]
    return {
        "name": name,
        "description": "release-selection probe",
        "qualities": [
            {"quality": rung, "allowed": ok, "priority": index}
            for index, (rung, ok) in enumerate(rungs)
        ],
        "cutoffQuality": allowed[0] if allowed else rungs[0][0],
        "minimumSize": minimum_size_mb,
        "maximumSize": maximum_size_mb,
        "preferredFormats": [],
        "preferredWords": list(preferred_words),
        "mustNotContain": list(forbidden),
        "mustContain": [],
        "preferredLanguages": [],
        "minimumSeeders": minimum_seeders,
        "minimumScore": 0,
        "isDefault": False,
        "preferNewerReleases": True,
        "maximumAge": 0,
    }


def release(
    rid: str,
    quality: str,
    protocol: str,
    size_mb: int,
    *,
    title: str | None = None,
    seeders: int = 0,
) -> dict[str, object]:
    return {
        "id": rid,
        "title": title or f"Some Book {quality} {protocol}",
        "artist": "Some Author",
        "quality": quality,
        "downloadType": protocol,
        "size": size_mb * MB,
        "seeders": seeders,
        "publishedDate": "",
        "format": "",
        "language": "",
        "source": "stub",
        "nzbUrl": "",
        "torrentUrl": "",
        "resultUrl": "",
    }


def create_profile(instance: str, payload: dict[str, object]) -> int:
    created = cast(dict[str, Any], api(instance, "POST", "/qualityprofile", payload))
    profile_id = created.get("id")
    if not isinstance(profile_id, int):
        raise ApparatusError(f"creating profile {payload['name']!r} returned no id")
    return profile_id


def score(instance: str, profile_id: int, releases: Sequence[dict[str, object]]) -> list[Row]:
    """Score releases and return them in the order they were posted, not the order returned.

    The endpoint sorts its answer, so rows are matched back by release id. Matching by position
    would silently compare a torrent against an NZB as soon as a score changed.
    """
    scored = cast(
        list[dict[str, Any]],
        api(instance, "POST", f"/qualityprofile/{profile_id}/score", list(releases)),
    )
    by_id: dict[str, Row] = {}
    for entry in scored:
        result = cast(dict[str, Any], entry.get("searchResult") or {})
        reasons = cast(list[str], entry.get("rejectionReasons") or [])
        rid = str(result.get("id", ""))
        by_id[rid] = Row(
            rid=rid,
            quality=str(result.get("quality", "")),
            protocol=str(result.get("downloadType", "")),
            size_mb=int(result.get("size", 0)) // MB,
            score=int(entry.get("totalScore", 0)),
            rejected=bool(entry.get("isRejected")),
            reasons="; ".join(reasons) or "-",
        )

    ordered: list[Row] = []
    for item in releases:
        rid = str(item["id"])
        if rid not in by_id:
            raise ApparatusError(f"the scorer returned no row for release {rid!r}")
        ordered.append(by_id[rid])
    return ordered


def show(caption: str, rows: Iterable[Row]) -> None:
    print(f"  {caption}")
    for row in rows:
        print(
            f"    score={row.score:>4}  rejected={str(row.rejected).lower():<5} "
            f" {row.quality:<12} {row.protocol:<8} {row.size_mb:>5} MB  [{row.reasons}]"
        )


def check_b2(instance: str) -> list[Check]:
    print("B2: does the operator's ordering decide anything?")
    print()

    pair = [
        release("hi", "MP3 320kbps", "torrent", 300),
        release("lo", "MP3 128kbps", "torrent", 300),
    ]
    first_320 = create_profile(
        instance, profile("B2 320 first", [("MP3 320kbps", True), ("MP3 128kbps", True)])
    )
    first_128 = create_profile(
        instance, profile("B2 128 first", [("MP3 128kbps", True), ("MP3 320kbps", True)])
    )
    refused_320 = create_profile(
        instance, profile("B2 320 refused", [("MP3 320kbps", False), ("MP3 128kbps", True)])
    )

    rows_320_first = score(instance, first_320, pair)
    rows_128_first = score(instance, first_128, pair)
    rows_refused = score(instance, refused_320, pair)

    show("operator ranks MP3 320kbps first", rows_320_first)
    show("operator ranks MP3 128kbps first, the only difference", rows_128_first)
    show("control, same ordering as the first profile but MP3 320kbps refused", rows_refused)
    print()

    ordering_ignored = [row.outcome() for row in rows_320_first] == [
        row.outcome() for row in rows_128_first
    ]
    refusal_landed = [row.outcome() for row in rows_320_first] != [
        row.outcome() for row in rows_refused
    ]

    checks = [
        Check(
            "B2 ordering",
            FINDING,
            "reordering a profile does not change release scores",
            ordering_ignored,
            "scores identical across the two orderings"
            if ordering_ignored
            else "the ordering moved the scores, so selection now reads the profile",
        ),
        Check(
            "B2 refusal control",
            CONTROL,
            "the same profile with a quality refused does change the answer",
            refusal_landed,
            "refusing a quality moved the answer, so the profile is reaching the scorer"
            if refusal_landed
            else "a refused quality changed nothing, so the scorer may not be reading the "
            "profile at all and the ordering result proves nothing",
        ),
    ]

    print("B2b: can the ladder separate two rungs the shipped default profile puts apart?")
    print()
    shipped_order = create_profile(
        instance,
        profile(
            "B2 shipped default order",
            [
                ("AAC 320kbps", True),
                ("AAC 256kbps", True),
                ("AAC 192kbps", True),
                ("AAC 128kbps", True),
                ("AAC 64kbps", True),
                ("MP3 320kbps", True),
            ],
        ),
    )
    rungs = [
        release("aac-top", "AAC 320kbps", "torrent", 300),
        release("aac-bottom", "AAC 64kbps", "torrent", 300),
        release("mp3", "MP3 320kbps", "torrent", 300),
    ]
    rows = score(instance, shipped_order, rungs)
    show(
        "AAC 320 > AAC 256 > AAC 192 > AAC 128 > AAC 64 > MP3 320, the order Listenarr ships",
        rows,
    )
    print()

    aac_top, aac_bottom, mp3 = rows
    inverted = aac_top.score == aac_bottom.score and mp3.score > aac_top.score
    checks.append(
        Check(
            "B2 ladder inversion",
            FINDING,
            "the ladder ties every AAC rung and puts MP3 320kbps above them all",
            inverted,
            f"AAC 320kbps and AAC 64kbps both score {aac_top.score}, MP3 320kbps scores {mp3.score}"
            if inverted
            else f"AAC 320kbps {aac_top.score}, AAC 64kbps {aac_bottom.score}, "
            f"MP3 320kbps {mp3.score}, which no longer inverts the shipped order",
        )
    )
    return checks


def check_b3(instance: str) -> list[Check]:
    print("B3: do the profile's gates run for a result classified as NZB?")
    print()

    checks: list[Check] = []
    both = [("MP3 320kbps", True), ("MP3 128kbps", True), ("MP3 64kbps", True)]

    ceiling = create_profile(instance, profile("B3 max size", both, maximum_size_mb=100))
    rows = score(
        instance,
        ceiling,
        [
            release("over-torrent", "MP3 320kbps", "torrent", 500),
            release("over-nzb", "MP3 320kbps", "usenet", 500),
            release("under-torrent", "MP3 320kbps", "torrent", 50),
            release("under-nzb", "MP3 320kbps", "usenet", 50),
        ],
    )
    show("maximumSize 100 MB, so 500 MB is over the ceiling and 50 MB is under it", rows)
    print()
    over_torrent, over_nzb = rows[0], rows[1]
    held = over_torrent.rejected and not over_nzb.rejected
    checks.append(
        Check(
            "B3a maximum size",
            FINDING,
            "the size ceiling rejects an oversized torrent and lets an oversized NZB through",
            held,
            "torrent rejected, NZB accepted at five times the ceiling"
            if held
            else f"torrent rejected={over_torrent.rejected}, NZB rejected={over_nzb.rejected}",
        )
    )

    floor = create_profile(instance, profile("B3 min size", both, minimum_size_mb=200))
    rows = score(
        instance,
        floor,
        [
            release("under-torrent", "MP3 320kbps", "torrent", 50),
            release("under-nzb", "MP3 320kbps", "usenet", 50),
            release("over-torrent", "MP3 320kbps", "torrent", 500),
            release("over-nzb", "MP3 320kbps", "usenet", 500),
        ],
    )
    show("minimumSize 200 MB, so 50 MB is under the floor and 500 MB is over it", rows)
    print()
    under_torrent, under_nzb = rows[0], rows[1]
    held = under_torrent.rejected and not under_nzb.rejected
    checks.append(
        Check(
            "B3b minimum size",
            FINDING,
            "the size floor rejects an undersized torrent and lets an undersized NZB through",
            held,
            "torrent rejected, NZB accepted at a quarter of the floor"
            if held
            else f"torrent rejected={under_torrent.rejected}, NZB rejected={under_nzb.rejected}",
        )
    )

    deduction = create_profile(instance, profile("B3 quality deduction", both))
    rows = score(
        instance,
        deduction,
        [
            release("t320", "MP3 320kbps", "torrent", 300),
            release("t64", "MP3 64kbps", "torrent", 300),
            release("n320", "MP3 320kbps", "usenet", 300),
            release("n64", "MP3 64kbps", "usenet", 300),
        ],
    )
    show("the same release twice per protocol, at the top and the bottom of the ladder", rows)
    print()
    t320, t64, n320, n64 = rows
    held = n320.score == n64.score and t320.score != t64.score
    checks.append(
        Check(
            "B3c quality deduction",
            FINDING,
            "the quality deduction separates two torrents and ties the same two NZBs",
            held,
            f"torrents {t320.score} against {t64.score}, NZBs both {n320.score}"
            if held
            else f"torrents {t320.score} and {t64.score}, NZBs {n320.score} and {n64.score}",
        )
    )

    biased = min(n320.score, n64.score) > max(t320.score, t64.score)
    checks.append(
        Check(
            "B3c protocol bias",
            FINDING,
            "the worst NZB outranks the best torrent, so protocol decides before content does",
            biased,
            f"MP3 64kbps over Usenet scores {n64.score}, MP3 320kbps over torrent scores "
            f"{t320.score}"
            if biased
            else "the NZB rows no longer outrank every torrent row",
        )
    )

    veto = create_profile(
        instance,
        profile("B3 allowed veto", [("MP3 320kbps", True), ("MP3 128kbps", False)]),
    )
    rows = score(
        instance,
        veto,
        [
            release("t128", "MP3 128kbps", "torrent", 300),
            release("n128", "MP3 128kbps", "usenet", 300),
            release("t320", "MP3 320kbps", "torrent", 300),
            release("n320", "MP3 320kbps", "usenet", 300),
        ],
    )
    show("MP3 128kbps switched off in the profile, MP3 320kbps left on", rows)
    print()
    refused_torrent, refused_nzb = rows[0], rows[1]
    held = refused_torrent.rejected and not refused_nzb.rejected
    checks.append(
        Check(
            "B3d allowed veto",
            FINDING,
            "a quality the operator switched off is refused over torrent and downloads over Usenet",
            held,
            "torrent rejected with a reason, NZB accepted with none"
            if held
            else f"torrent rejected={refused_torrent.rejected}, "
            f"NZB rejected={refused_nzb.rejected}",
        )
    )

    print("B3 controls: one gate that should branch on protocol, one that should not.")
    print()
    seeders = create_profile(
        instance, profile("B3 seeders", [("MP3 320kbps", True)], minimum_seeders=5)
    )
    rows = score(
        instance,
        seeders,
        [
            release("t", "MP3 320kbps", "torrent", 300),
            release("n", "MP3 320kbps", "usenet", 300),
        ],
    )
    show("minimumSeeders 5 and neither release has any, a gate that IS protocol-specific", rows)
    print()
    seed_torrent, seed_nzb = rows
    held = seed_torrent.rejected and not seed_nzb.rejected
    checks.append(
        Check(
            "B3 seeders control",
            CONTROL,
            "the seeders gate fires for the torrent and not for the NZB",
            held,
            "the apparatus tells the two protocols apart and applies a gate to one of them"
            if held
            else "the seeders gate did not behave as a protocol-specific gate, so nothing here "
            "shows the apparatus can distinguish the protocols",
        )
    )

    word = "listenarrprobeforbidden"
    forbidden_profile = create_profile(
        instance, profile("B3 forbidden word", [("MP3 320kbps", True)], forbidden=[word])
    )
    rows = score(
        instance,
        forbidden_profile,
        [
            release("t", "MP3 320kbps", "torrent", 300, title=f"Some Book {word} torrent"),
            release("n", "MP3 320kbps", "usenet", 300, title=f"Some Book {word} usenet"),
        ],
    )
    show(f"mustNotContain {word!r}, a gate that is NOT protocol-conditional", rows)
    print()
    word_torrent, word_nzb = rows
    held = word_torrent.rejected and word_nzb.rejected
    checks.append(
        Check(
            "B3 forbidden word control",
            CONTROL,
            "a gate outside the protocol guard rejects both protocols",
            held,
            "both rejected, so an NZB does reach the scorer and the scorer can refuse one"
            if held
            else "the NZB was not rejected even by a gate outside the protocol guard, so the B3 "
            "findings may only show that NZB results never reach the scorer",
        )
    )
    return checks


def check_b4(instance: str) -> list[Check]:
    print("B4: does an operator preference survive to the score?")
    print()

    words = ["retail", "unabridged", "narrator", "chaptered", "m4b"]
    matches_all = " ".join(words)

    # FLAC is the top of the hardcoded ladder, so the quality deduction is zero and the release is
    # already at the cap before a single preference has been added to it.
    top = create_profile(
        instance,
        profile(
            "B4 top rung",
            [("FLAC", True), ("MP3 128kbps", True)],
            preferred_words=words,
        ),
    )
    top_rows = score(
        instance,
        top,
        [
            release(
                "top-preferred",
                "FLAC",
                "torrent",
                300,
                title=f"Some Book {matches_all} FLAC",
                seeders=10,
            ),
            release("top-plain", "FLAC", "torrent", 300, title="Some Book FLAC"),
        ],
    )
    show("FLAC, five preferred words and ten seeders against neither", top_rows)

    # The same preference on a rung 50 below the cap, where it was never in reach.
    low = create_profile(
        instance,
        profile(
            "B4 low rung",
            [("MP3 128kbps", True), ("FLAC", True)],
            preferred_words=words,
        ),
    )
    low_rows = score(
        instance,
        low,
        [
            release(
                "low-preferred",
                "MP3 128kbps",
                "torrent",
                300,
                title=f"Some Book {matches_all} MP3 128kbps",
                seeders=10,
            ),
            release("low-plain", "MP3 128kbps", "torrent", 300, title="Some Book MP3 128kbps"),
        ],
    )
    show(
        "control, the identical preference on MP3 128kbps, which starts 50 below the cap",
        low_rows,
    )
    print()

    top_preferred, top_plain = top_rows
    low_preferred, low_plain = low_rows
    tied = top_preferred.score == top_plain.score
    control_separates = low_preferred.score != low_plain.score

    return [
        Check(
            "B4 preference truncated",
            FINDING,
            "a preferred release and a plain one tie at the top of the ladder",
            tied,
            f"both scored {top_preferred.score} although only one matched the profile's words"
            if tied
            else f"preferred {top_preferred.score} against plain {top_plain.score}, so the "
            "preference now separates them",
        ),
        Check(
            "B4 preference control",
            CONTROL,
            "the same preference does separate two releases further down the ladder",
            control_separates,
            f"preferred {low_preferred.score} against plain {low_plain.score}, so preferred words "
            "and seeders do reach the score"
            if control_separates
            else "the preference changed nothing even where the cap was out of reach, so it never "
            "reaches the score and the tie above says nothing about the cap",
        ),
    ]


def report(checks: Sequence[Check]) -> int:
    width = max(len(check.key) for check in checks)
    print("=" * 96)
    print(f"{'check':<{width}}  {'kind':<8}  {'result':<15}  claim")
    print("-" * 96)
    for check in checks:
        if check.kind == FINDING:
            result = "REPRODUCED" if check.held else "NOT REPRODUCED"
        else:
            result = "DISCRIMINATES" if check.held else "INERT"
        print(f"{check.key:<{width}}  {check.kind:<8}  {result:<15}  {check.claim}")
    print("-" * 96)
    for check in checks:
        print(f"{check.key:<{width}}  {check.note}")
    print("=" * 96)
    print()

    inert = [check for check in checks if check.kind == CONTROL and not check.held]
    missing = [check for check in checks if check.kind == FINDING and not check.held]

    if inert:
        print("A control did not discriminate, so this run measured nothing. Findings are void:")
        for check in inert:
            print(f"  {check.key}: {check.note}")
        return EXIT_CONTROL_INERT
    if missing:
        print("Every control discriminated, and a finding did not reproduce:")
        for check in missing:
            print(f"  {check.key}: {check.note}")
        print()
        print("If a fix landed, that is the expected result and this run is the evidence for it.")
        return EXIT_FINDING_GONE

    print("Every finding reproduced and every control discriminated.")
    return EXIT_OK


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="profile_gate_probe.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--name",
        metavar="NAME",
        help="measure an instance that is already up under this probe name, and leave it up. "
        "Without this a stock throwaway is started and torn down again.",
    )
    parser.add_argument(
        "--image",
        default=None,
        metavar="REF",
        help="image to start, when starting one. Defaults to whatever probe.sh calls stock. "
        "A patched build is not a control for anything meant to go upstream.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        metavar="N",
        help="port to publish on, when starting one. probe.sh picks a free one otherwise, and "
        "refuses the production port either way.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)

    instance = args.name
    started = False
    if instance is None:
        instance = f"profile-gate-probe-{os.getpid()}"
        up = ["up", "--name", instance]
        if args.image:
            up += ["--image", args.image]
        if args.port:
            up += ["--port", str(args.port)]
        print(run_probe(up), end="")
        started = True
        print()

    try:
        checks = check_b2(instance) + check_b3(instance) + check_b4(instance)
        return report(checks)
    finally:
        if started:
            print()
            print(run_probe(["down", instance]), end="")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ApparatusError as error:
        print(f"apparatus: {error}", file=sys.stderr)
        sys.exit(EXIT_APPARATUS)
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        sys.exit(EXIT_APPARATUS)
