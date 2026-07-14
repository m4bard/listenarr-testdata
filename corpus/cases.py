#!/usr/bin/env python3
"""The case matrix: what a generated test library is actually FOR.

A test library is the cross product of two independent axes:

  LAYOUT    — how the folders are named on disk
  TAG_STATE — what the embedded ID3/MP4 tags say relative to the folder

A scanner can fail on either axis independently, and the interesting bugs live in
the corners where both are adverse at once. Each case declares its EXPECTED
outcome, which is what turns generated data into a conformance suite rather than
just a pile of files.

Expected outcomes are deliberately written as what a CORRECT scanner should do,
not as what Listenarr does today. A case whose expectation Listenarr fails is a
bug report, not a broken fixture.
"""
from __future__ import annotations

from dataclasses import dataclass, field


# --------------------------------------------------------------------------
# Axis 1: on-disk layouts
# --------------------------------------------------------------------------
# `pattern` uses {author} {series} {series_position} {title} {year} placeholders.
# A layout is a folder pattern plus a filename pattern.

@dataclass(frozen=True)
class Layout:
    key: str
    folder: str
    filename: str
    note: str


LAYOUTS: list[Layout] = [
    Layout(
        key="listenarr-native",
        folder="{author}/{year} - {title}",
        filename="{title}",
        note="The layout PathMetadataParser actually requires: '{Year} - {Title}'. "
             "The only one that currently parses.",
    ),
    Layout(
        key="listenarr-native-series",
        folder="{author}/{series}/{year} - {title} [{series} {series_position}]",
        filename="{title}",
        note="Native layout with the series bracket the parser also understands.",
    ),
    Layout(
        key="audnex-plex",
        folder="{author}/{author} - {series} - {title}",
        filename="{author} - {series} - {title}",
        note="The common Audnex/Plex convention. Carries no year, so the native "
             "parser rejects it outright — this is the layout that parses at 0%.",
    ),
    Layout(
        key="author-series-title",
        folder="{author}/{series}/{title}",
        filename="{title}",
        note="Plain author/series/title. No year anywhere.",
    ),
    Layout(
        key="audiobookshelf",
        folder="{author}/{series}/{series_position} - {title}",
        filename="{series} - {series_position} - {title}",
        note="AudioBookShelf-style: series-creator folder, numbered episode files. "
             "The layout PR #688 was written to rescue.",
    ),
    Layout(
        key="flat",
        folder="{author} - {title}",
        filename="{author} - {title}",
        note="Single flat folder per book, no author directory level.",
    ),
    Layout(
        key="loose",
        folder="",
        filename="{author} - {title}",
        note="Loose files at the library root, no containing folder at all.",
    ),
    Layout(
        key="title-only",
        folder="{title}",
        filename="{title}",
        note="Folder carries the title and nothing else — no author anywhere on the "
             "path, so any author-in-path heuristic has nothing to bite on.",
    ),
]


# --------------------------------------------------------------------------
# Axis 2: embedded tag states
# --------------------------------------------------------------------------
# Each state is a transform applied to the book's true metadata before it is
# written into the file's tags. This is where "the tags lie" gets modelled.

@dataclass(frozen=True)
class TagState:
    key: str
    note: str
    # what a correct scanner should do with a file in this state, given that the
    # FOLDER identifies the book correctly:
    expect: str


TAG_STATES: list[TagState] = [
    TagState(
        key="correct-with-asin",
        note="Tags agree with the folder and carry the book's real ASIN.",
        expect="link — ASIN is definitive",
    ),
    TagState(
        key="correct-no-asin",
        note="Tags agree with the folder but carry NO ASIN. This is the common real-world "
             "case: an entire library can contain zero ASINs.",
        expect="link — title+author agree",
    ),
    TagState(
        key="no-tags",
        note="File carries no usable tags at all.",
        expect="link — fall back to the folder; absence of tags is not evidence against",
    ),
    TagState(
        key="wrong-title-same-author",
        note="Tag names a DIFFERENT book by the SAME author. The single most common "
             "disagreement in a real library, and the one where the author check "
             "provides no discrimination whatsoever.",
        expect="do NOT link on tags — conflict; prefer folder, surface for review",
    ),
    TagState(
        key="wrong-author",
        note="Tag names a different author entirely.",
        expect="do NOT link on tags — conflict; prefer folder, surface for review",
    ),
    TagState(
        key="wrong-asin",
        note="Tag carries a real ASIN, but of a DIFFERENT book. A regional or "
             "re-release ASIN lands here too.",
        expect="do NOT link — a definitive signal pointing at the wrong book must not win",
    ),
    TagState(
        key="colliding-title",
        note="Tag names a different book whose title CONTAINS (or is contained by) the "
             "folder's title, e.g. folder 'She' vs tag 'She and Allan'. Defeats naive "
             "containment matching. See the title-collision entries in corpus.json.",
        expect="do NOT link on tags — distinct works",
    ),
    TagState(
        key="subtitled-title",
        note="Tag carries the same work's FULL title with subtitle, e.g. folder 'She' vs "
             "tag 'She: A History of Adventure'. A TRUE match that must survive any fix "
             "aimed at colliding-title.",
        expect="link — same work",
    ),
    TagState(
        key="author-variant",
        note="Same author, different credited spelling: 'H.G. Wells' vs 'H. G. Wells', "
             "'Émile Zola' vs 'Emile Zola', 'Dostoyevsky' vs 'Dostoevsky', "
             "'Saki' vs 'Hector Hugh Munro'.",
        expect="link — same author; strict equality here is a false negative",
    ),
    TagState(
        key="translator-as-author",
        note="Tag credits the translator instead of the author "
             "(e.g. Constance Garnett rather than Dostoevsky).",
        expect="link — do not reject on an author the record also lists",
    ),
    TagState(
        key="numeral-variant",
        note="Same work, numeral style differs: 'Twenty Thousand Leagues' vs "
             "'20,000 Leagues'; 'Eighty Days' vs '80 Days'. Both are real, live titles.",
        expect="link — same work",
    ),
]


# --------------------------------------------------------------------------
# Scenarios: the library-level shapes worth generating whole
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Scenario:
    key: str
    note: str
    layouts: list[str]
    tag_states: list[str]
    expect: str = ""
    extras: dict = field(default_factory=dict)


SCENARIOS: list[Scenario] = [
    Scenario(
        key="happy-path",
        note="The layout Listenarr was built for, tags correct. Everything should link. "
             "If this fails, nothing else is worth reading.",
        layouts=["listenarr-native", "listenarr-native-series"],
        tag_states=["correct-with-asin", "correct-no-asin"],
        expect="100% of files linked",
    ),
    Scenario(
        key="existing-library-adoption",
        note="THE headline bug. A pre-existing library in the common Audnex/Plex layout, "
             "correctly tagged. Listenarr synthesizes BasePath from metadata and dead-ends "
             "when that folder does not exist, so nothing is discovered.",
        layouts=["audnex-plex", "author-series-title"],
        tag_states=["correct-no-asin"],
        expect="100% linked. Listenarr today: 0%.",
    ),
    Scenario(
        key="tag-fallback-rescue",
        note="The case PR #688 targets: layout carries neither title nor author in a form "
             "the path heuristics accept, but the tags are good.",
        layouts=["audiobookshelf", "title-only"],
        tag_states=["correct-with-asin", "correct-no-asin"],
        expect="linked via embedded tags",
    ),
    Scenario(
        key="lying-tags",
        note="The case PR #688 does NOT handle. Tags actively disagree with the folder. "
             "A mis-link is worse than a miss: it silently attaches the wrong book and the "
             "user has no reason to look.",
        layouts=["audnex-plex", "listenarr-native"],
        tag_states=["wrong-title-same-author", "wrong-author", "wrong-asin", "colliding-title"],
        expect="0 files linked via tags; every one surfaced as a conflict",
    ),
    Scenario(
        key="title-collision",
        note="The Haggard trap. Three distinct novels — She / She and Allan / "
             "Ayesha: The Return of She — under one author. A bidirectional Contains "
             "attributes all three to 'She'. The subtitled form must still match.",
        layouts=["audnex-plex", "listenarr-native"],
        tag_states=["colliding-title", "subtitled-title"],
        expect="subtitled-title links; colliding-title does not",
    ),
    Scenario(
        key="author-variance",
        note="Same author, spelled differently across sources. Initials, diacritics, "
             "transliterations, pseudonyms, translators. All false-NEGATIVE risks.",
        layouts=["audnex-plex"],
        tag_states=["author-variant", "translator-as-author", "numeral-variant"],
        expect="all link — rejecting these is a false negative",
    ),
    Scenario(
        key="series-work-key",
        note="Two distinct ASINs for the same work, sharing one series ASIN and position "
             "(A Princess of Mars, 20,000 Leagues, Oz, The Three Musketeers). Demonstrates "
             "that the book ASIN is a manifestation id and (series_asin, position) is the "
             "stable work key. Also covers the Sherlock canon, whose positions really do "
             "run 1,2,3,5,7 because collections occupy the gaps.",
        layouts=["listenarr-native-series"],
        tag_states=["correct-with-asin"],
        expect="the pairs dedupe to one work; series positions survive round-trip",
    ),
    Scenario(
        key="mixed-reality",
        note="What an actual library looks like: several layouts side by side, a realistic "
             "spread of tag states, some loose files, some untagged. Nothing is uniform. "
             "This is the integration test.",
        layouts=[layout.key for layout in LAYOUTS],
        tag_states=[state.key for state in TAG_STATES],
        expect="see per-case expectations; report a pass/fail table",
        extras={"tag_state_weights": {
            # roughly matches what a real library looks like: most tags are fine,
            # a meaningful minority actively lie, a few files have nothing at all.
            "correct-no-asin": 0.62,
            "correct-with-asin": 0.05,
            "wrong-title-same-author": 0.11,
            "wrong-author": 0.06,
            "colliding-title": 0.04,
            "subtitled-title": 0.04,
            "author-variant": 0.04,
            "no-tags": 0.03,
            "wrong-asin": 0.01,
        }},
    ),
]


LAYOUTS_BY_KEY = {layout.key: layout for layout in LAYOUTS}
TAG_STATES_BY_KEY = {state.key: state for state in TAG_STATES}
SCENARIOS_BY_KEY = {scenario.key: scenario for scenario in SCENARIOS}


if __name__ == "__main__":
    print(f"{len(LAYOUTS)} layouts x {len(TAG_STATES)} tag states")
    print(f"{len(SCENARIOS)} scenarios\n")
    for scenario in SCENARIOS:
        print(f"  {scenario.key:28} {scenario.expect}")
