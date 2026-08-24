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

# Measured against the committed corpus, last narrowed 2026-08-24. Shrinking this list is the
# point of having it; growing it is a regression. Two rounds of fixes have been done: twenty
# series cases got a fragment naming the specific work, then sixteen of the empty-fragment
# language and edition seeds got one too, which is why several non-English seeds that used to
# expect nothing now expect a short native fragment (Vingt mille lieues, Verwandlung,
# Dornroeschen).
#
# What is left is three kinds of case, none of them fixable by picking a better fragment:
#
#   1. A title that is a strict substring of another's, so nothing matching the shorter one can
#      fail to match the longer: Pellucidar inside Tanar of Pellucidar, Faust inside Faust I,
#      War and Peace inside War and Peace (Russian Edition).
#   2. Two editions of one work whose titles differ only by an edition marker, where the pair
#      now collides with each other and with nothing else. Separating them is a decision about
#      whether an edition label belongs in a fragment at all, not a missing fragment.
#   3. The five seeds whose author and title are both non-Latin, where the only specific value
#      available is the native string and lifting it out of the API response would make the
#      expectation a copy of the answer.
KNOWN_AMBIGUOUS_SEEDS: frozenset[str] = frozenset({
    # 1. strict-substring titles
    "B002V0PVJC",
    "B002V1OVFQ",
    "B00769TAK4",
    "B00APWL9E4",
    "B00EOO99WS",
    "B00JQEQFL4",
    # 2. edition variants of one work, colliding only with their own sibling
    "B00BYIJW6A",
    "B006GDCIY6",
    "B01AGYIKG0",
    "B01MU7YH84",
    "B076PQXBV7",
    "B07RGRBKS5",
    # 3. no Latin fragment to give
    "B08BTM5TDG",
    "B08BTZVGS8",
    "B08BV2RNS9",
    "B0B5Z12CCM",
    "B0CTK91XJ6",
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
