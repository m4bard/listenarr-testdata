"""Seed fragments must be able to tell one work from another.

`build_corpus` accepts a book when the seed's expected author and title appear *within* what the
API returned. That looseness is deliberate: the expected values are short fragments a human chose,
so they are independent of the API's own answer. Rewriting them to full titles would mean copying
those titles out of the API responses, turning a real check into one that can never disagree.

The cost is that a fragment can fail to distinguish two different works by the same author.
`Kipling` plus `Jungle Book` matches both `The Jungle Book` and `The Second Jungle Book`, so an
ASIN drifting from one to the other would still be accepted.

This is a ratchet, not a clean bill of health. The seeds that are ambiguous today are listed and
frozen; a new one cannot be added, and a listed one that gets fixed has to be removed from the
list. Editions of the *same* work sharing a fragment are fine and are not counted, since the drift
worth catching is an ASIN resolving to a different work.
"""
from __future__ import annotations

import json
import pathlib
import re
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "tools"))

import build_corpus

ROOT = pathlib.Path(__file__).resolve().parents[1]

# Ambiguous as of 2026-08-04, measured against the committed corpus. Shrinking this list is the
# point of having it; growing it is a regression.
KNOWN_AMBIGUOUS_SEEDS: frozenset[str] = frozenset({
    "B002V0PVJC",
    "B002V1OVFQ",
    "B002V5B7TK",
    "B002V5CJM4",
    "B002V8OEG0",
    "B002V9Z9WW",
    "B003F6JXC2",
    "B004V5UU0A",
    "B004YWTD30",
    "B006C692NM",
    "B006GDCIY6",
    "B00769TAK4",
    "B007BR5KZA",
    "B008Q3A6JI",
    "B008WB1L70",
    "B00APWL9E4",
    "B00B4FPVR2",
    "B00BYIJW6A",
    "B00EOO99WS",
    "B00JQEQFL4",
    "B00OQQTXE8",
    "B00T9V0BU0",
    "B00TDZQG3I",
    "B00TPKF9QQ",
    "B00TPW1FLM",
    "B00UXEBBIS",
    "B01AGYIKG0",
    "B01FKWL15A",
    "B01GIO3GFW",
    "B01IDLCAMI",
    "B01JWOHBEC",
    "B01LFD0GWM",
    "B01MU7YH84",
    "B076HSP1FT",
    "B076PQXBV7",
    "B07B7MCLB3",
    "B07RGRBKS5",
    "B07TKCFMD1",
    "B084J9S79P",
    "B0899BQL13",
    "B08BTM5TDG",
    "B08BTZVGS8",
    "B08BV2RNS9",
    "B08ML2HVVW",
    "B08SQ3S34B",
    "B0B1QKNWH3",
    "B0B5Z12CCM",
    "B0C6B525PQ",
    "B0CTK91XJ6",
    "B0DKK1PKN7",
    "B0DY31J772",
    "B0DZXWPQNW",
    "B0F48KS3BX",
})


def normalize(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip()).casefold()


def corpus_books() -> list[dict]:
    data = json.loads((ROOT / "corpus" / "corpus.json").read_text())
    books: list[dict] = data["books"]
    return books


def fragment_matches(want_author: str, want_title: str, book: dict) -> bool:
    """The acceptance rule from build_corpus, applied to an arbitrary book."""
    joined = ", ".join(book["authors"])
    return (want_author.lower() in joined.lower()
            and want_title.lower() in (book["title"] or "").lower())


def ambiguous_seeds() -> dict[str, list[str]]:
    """Seeds whose fragment also matches a differently-titled book, and which those are."""
    books = corpus_books()
    by_asin = {book["asin"]: book for book in books}
    found: dict[str, list[str]] = {}
    for asin, want_author, want_title, _tags in build_corpus.SEEDS:
        own = by_asin.get(asin)
        if own is None:
            continue
        others = sorted({
            book["title"] for book in books
            if fragment_matches(want_author, want_title, book)
            and normalize(book["title"]) != normalize(own["title"])
        })
        if others:
            found[asin] = others
    return found


@pytest.mark.contract
class TestSeedFragmentsAreUnambiguous:
    def test_no_new_ambiguous_seed_is_introduced(self) -> None:
        """A seed added with a fragment too vague to identify its own work is a regression."""
        new = sorted(set(ambiguous_seeds()) - KNOWN_AMBIGUOUS_SEEDS)
        detail = {asin: ambiguous_seeds()[asin][:3] for asin in new}
        assert not new, (
            "these seeds cannot tell their own book from a differently-titled one:\n"
            f"{json.dumps(detail, indent=2, ensure_ascii=False)}"
        )

    def test_the_exemption_list_has_no_stale_entries(self) -> None:
        """A seed that got fixed must leave the list, or the list stops meaning anything."""
        stale = sorted(KNOWN_AMBIGUOUS_SEEDS - set(ambiguous_seeds()))
        assert not stale, f"no longer ambiguous, remove from the list: {stale}"

    def test_every_exempted_asin_is_still_a_seed(self) -> None:
        """A dropped seed leaves a dead entry that would mask a later re-introduction."""
        seeds = {asin for asin, *_rest in build_corpus.SEEDS}
        assert not KNOWN_AMBIGUOUS_SEEDS - seeds


class TestTheCheckItself:
    def test_a_fragment_matching_only_its_own_work_is_not_flagged(self) -> None:
        book = {"asin": "B1", "title": "The Valley of Fear", "authors": ["Arthur Conan Doyle"]}
        other = {"asin": "B2", "title": "Moby Dick", "authors": ["Herman Melville"]}
        assert fragment_matches("Doyle", "Valley of Fear", book)
        assert not fragment_matches("Doyle", "Valley of Fear", other)

    def test_editions_of_one_work_are_not_treated_as_ambiguity(self) -> None:
        """Two ASINs for the same title is the duplicate-editions axis, not a vague fragment."""
        assert normalize("A Princess of Mars") == normalize("  a  princess of mars ")

    def test_an_empty_fragment_matches_everything(self) -> None:
        """Why the non-Latin seeds are on the list: an empty expectation excludes nothing."""
        anything = {"asin": "B9", "title": "Whatever", "authors": ["Someone"]}
        assert fragment_matches("", "", anything)
