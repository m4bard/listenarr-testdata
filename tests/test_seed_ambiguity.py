"""Seed fragments must be able to tell one work from another.

`build_corpus` accepts a book when the seed's expected author and title appear *within* what the
API returned. That looseness is deliberate: the expected values are short fragments a human chose,
so they are independent of the API's own answer. Rewriting them to full titles would mean copying
those titles out of the API responses, turning a real check into one that can never disagree.

The cost is that a fragment can fail to distinguish two different works by the same author.
`Kipling` plus `Jungle Book` matches both `The Jungle Book` and `The Second Jungle Book`, so an
ASIN drifting from one to the other would still be accepted.

A fragment is not the only thing standing between a seed and the wrong book. A seed named in
`build_corpus.SPELLING_CRITICAL` must additionally match its credited author string exactly, and
that pin discriminates in cases no fragment can: `Peter Pan` is inside `Peter Pan in Kensington
Gardens` however you word it, but one record credits `J M Barrie` and the other `James M. Barrie`.
`accepted` below applies both halves, because judging on the fragment alone would report those
seeds as unprotected when they are the best protected in the corpus.

This is a ratchet, not a clean bill of health. The seeds that are ambiguous today are listed and
frozen; a new one cannot be added, and a listed one that gets fixed has to be removed from the
list. Editions of the *same* work sharing a fragment are fine and are not counted, since the drift
worth catching is an ASIN resolving to a different work. `same_work` below is what decides that,
and it is written to keep flagging two different novels by one author whose titles happen to
overlap.
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
# point of having it; growing it is a regression. Three rounds of fixes have been done: twenty
# series cases got a fragment naming the specific work, then sixteen of the empty-fragment
# language and edition seeds got one too, and then the last five seeds expecting nothing got
# their native title, which `build_corpus.check_fragment` now requires of every seed.
#
# The edition variants left the list by a different route: they were never really ambiguous,
# and `same_work` below now says so. `B00APWL9E4` (Faust I, inside Faust I + II) left on
# 2026-09-04 by a third route: it is spelling-critical, so acceptance now demands the exact
# string `Johann Wolfgang Goethe`, and every record it used to be confusable with credits
# `Johann Wolfgang von Goethe`. Nothing about its fragment changed.
#
# What survives is one kind of case, and no fragment can fix it. A title that is a strict
# substring of another's leaves nothing to match the shorter without also matching the
# longer: Pellucidar inside Tanar of Pellucidar, Faust inside Faust I. What CAN fix it is
# an exact author pin, where the two records credit different strings; the four below are
# the ones where they do not.
KNOWN_AMBIGUOUS_SEEDS: frozenset[str] = frozenset({
    "B002V1OVFQ",  # Pellucidar, inside Tanar of Pellucidar
    "B00769TAK4",  # Faust, inside Faust I and Faust I + II
    "B00EOO99WS",  # Faust
    "B00JQEQFL4",  # Faust
})

# Text a retailer hangs off a title to mark one edition of it: "(AmazonClassics Edition)",
# "(Russian Edition)", "[The Divine Comedy]". Both the label and the bracketing are the
# retailer's, which is exactly why writing one into a seed fragment is a bad idea.
EDITION_DECORATION = re.compile(r"[(\[][^)\]]*[)\]]")
LEADING_ARTICLE = re.compile(r"^(the|a|an)\s+")


def normalize(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip()).casefold()


def work_key(title: str) -> str:
    """A title with the retailer's edition decoration and a leading article taken off."""
    return LEADING_ARTICLE.sub("", normalize(EDITION_DECORATION.sub(" ", title or "")))


def author_key(name: str) -> str:
    """A credited name with punctuation and spacing removed: 'J. M. Barrie' -> 'jmbarrie'.

    The corpus deliberately credits one author under several punctuations of the same
    initials, because that is the drift a {Author} rename has to fold together. Comparing
    credited strings exactly therefore makes two editions of ONE work look like two works,
    which is the opposite of what same_work is for. Only punctuation and spacing come off:
    'James M. Barrie' stays distinct from 'J. M. Barrie', so an abbreviation is still a
    difference and Pellucidar-style containment is untouched.
    """
    return "".join(ch for ch in normalize(name) if ch.isalnum())


def same_work(one: dict, other: dict) -> bool:
    """Are these two books the same work, differing only in which edition of it they are?

    The docstring at the top of this module says editions of one work sharing a fragment are
    fine, and this is what decides that. Two conditions, both required: the books share an
    author, and their titles agree once an edition label and a leading article come off. So
    `War and Peace` and `War and Peace (Russian Edition)` are one work, and `Metamorphosis`
    and `The Metamorphosis` are one work.

    The author comparison ignores punctuation and spacing, so a work credited to
    `J. M. Barrie` on one ASIN and `J.M. Barrie` on another is still one work. Without that
    the corpus's own author-drift pairs would each read as two different books.

    The rule deliberately does NOT say "same author, and one title contains the other". That
    reading would swallow `Pellucidar` inside `Tanar of Pellucidar`, which is the same author
    and a real containment and two entirely different novels. What separates them is that the
    extra words in `Tanar of Pellucidar` are part of the title, whereas the extra words in
    `(Russian Edition)` are a bracketed edition label, and stripping only the latter keeps
    Burroughs flagged where he belongs.
    """
    return (work_key(one["title"]) == work_key(other["title"])
            and bool({author_key(a) for a in one["authors"]}
                     & {author_key(a) for a in other["authors"]}))


def corpus_books() -> list[dict]:
    data = json.loads((ROOT / "corpus" / "corpus.json").read_text())
    books: list[dict] = data["books"]
    return books


def fragment_matches(want_author: str, want_title: str, book: dict) -> bool:
    """The acceptance rule from build_corpus, applied to an arbitrary book."""
    joined = ", ".join(book["authors"])
    return (want_author.lower() in joined.lower()
            and want_title.lower() in (book["title"] or "").lower())


def accepted(asin: str, want_author: str, want_title: str, book: dict) -> bool:
    """Everything `build_corpus` requires before it takes a book, not just the fragment half.

    A seed named in SPELLING_CRITICAL must also match its credited author string exactly, and
    that check discriminates where a fragment cannot. `Peter Pan` sits inside `Peter Pan in
    Kensington Gardens` and no wording of the fragment separates them, but the two records
    credit `J M Barrie` and `James M. Barrie`, so the exact pin does. Judging ambiguity on the
    fragment alone would report those seeds as unprotected when they are not.
    """
    return (fragment_matches(want_author, want_title, book)
            and build_corpus.check_spelling(asin, book["authors"]) is None)


def ambiguous_seeds() -> dict[str, list[str]]:
    """Seeds that would also accept a different WORK, and which those are."""
    books = corpus_books()
    by_asin = {book["asin"]: book for book in books}
    found: dict[str, list[str]] = {}
    for asin, want_author, want_title, _tags, _librivox in build_corpus.SEEDS:
        own = by_asin.get(asin)
        if own is None:
            continue
        others = sorted({
            book["title"] for book in books
            if accepted(asin, want_author, want_title, book)
            and normalize(book["title"]) != normalize(own["title"])
            and not same_work(book, own)
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
            "these seeds would accept a differently-titled book by the same author, so a "
            "drifted or mistyped ASIN could resolve to the wrong one and still pass:\n"
            f"{json.dumps(detail, indent=2, ensure_ascii=False)}\n"
            "Fix it, in this order. Give the seed a fragment that names its own work, which "
            "works unless its title is a strict substring of the other's. Failing that, pin "
            "it in build_corpus.SPELLING_CRITICAL: acceptance then demands the exact credited "
            "author, which separates two records a fragment cannot. Only if neither applies "
            "does it belong in KNOWN_AMBIGUOUS_SEEDS, with the reason written beside it."
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
        """Why build_corpus refuses one: an empty expectation excludes nothing."""
        anything = {"asin": "B9", "title": "Whatever", "authors": ["Someone"]}
        assert fragment_matches("", "", anything)
        assert build_corpus.check_fragment("B9", "", "") is not None


@pytest.mark.contract
class TestSameWorkDoesNotLaunderARealCollision:
    """`same_work` suppresses flags, so its failure mode is hiding a genuine defect.

    Every case here is drawn from the committed corpus rather than invented, because the risk
    is not that the rule mishandles a hypothetical title but that it mishandles one we ship.
    """

    def test_an_edition_label_in_parentheses_does_not_make_a_new_work(self) -> None:
        plain = {"asin": "B01AGYIKG0", "title": "Around the World in Eighty Days",
                 "authors": ["Jules Verne"]}
        labelled = {"asin": "B076PQXBV7",
                    "title": "Around the World in Eighty Days (AmazonClassics Edition)",
                    "authors": ["Jules Verne", "George Makepeace Towle - translator"]}
        assert same_work(plain, labelled)

    def test_a_bracketed_translated_title_does_not_make_a_new_work(self) -> None:
        comedia = {"asin": "B07RGRBKS5", "title": "La Divina Comedia",
                   "authors": ["Dante Alighieri"]}
        bracketed = {"asin": "B00BYIJW6A", "title": "La Divina Comedia [The Divine Comedy]",
                     "authors": ["Dante Alighieri"]}
        assert same_work(comedia, bracketed)

    def test_a_leading_article_does_not_make_a_new_work(self) -> None:
        bare = {"asin": "B01MU7YH84", "title": "Metamorphosis", "authors": ["Franz Kafka"]}
        articled = {"asin": "B01LFD0GWM", "title": "The Metamorphosis", "authors": ["Franz Kafka"]}
        assert same_work(bare, articled)

    def test_a_containing_title_is_still_a_different_work(self) -> None:
        """The case the rule exists to get right: same author, real containment, two novels."""
        pellucidar = {"asin": "B002V1OVFQ", "title": "Pellucidar",
                      "authors": ["Edgar Rice Burroughs"]}
        tanar = {"asin": "B0C6B525PQ", "title": "Tanar of Pellucidar",
                 "authors": ["Edgar Rice Burroughs"]}
        assert not same_work(pellucidar, tanar)
        assert "B002V1OVFQ" in ambiguous_seeds()

    def test_a_volume_number_is_not_an_edition_label(self) -> None:
        """Faust I is not an edition of Faust, so the Goethe cluster must stay flagged."""
        faust = {"asin": "B00EOO99WS", "title": "Faust", "authors": ["Johann Wolfgang von Goethe"]}
        part_one = {"asin": "B00APWL9E4", "title": "Faust I",
                    "authors": ["Johann Wolfgang Goethe"]}
        assert not same_work(faust, part_one)

    def test_the_same_title_by_a_different_author_is_a_different_work(self) -> None:
        """Sharing an author is required, so an unrelated namesake title cannot be waved through."""
        one = {"asin": "B1", "title": "Marie", "authors": ["H. Rider Haggard"]}
        other = {"asin": "B2", "title": "Marie", "authors": ["Someone Else"]}
        assert not same_work(one, other)


@pytest.mark.contract
class TestTheExactAuthorPinIsWhatSeparatesTheBarrieSeeds:
    """Four Barrie records, two titles, and one title a strict substring of another's.

    No fragment can separate `Peter Pan` from `Peter Pan in Kensington Gardens`. What does is
    that acceptance for a spelling-critical seed demands the whole credited author string, and
    the two records credit different ones. These tests exist so that removing a pin cannot
    quietly turn the fragment check back into the only thing guarding these seeds.
    """

    BARRIE = ("B0C6FJ6L34", "B084J9S79P", "B078X1NX28", "B002V1M36U")

    def test_none_of_them_is_ambiguous(self) -> None:
        assert [asin for asin in self.BARRIE if asin in ambiguous_seeds()] == []

    def test_all_four_are_pinned(self) -> None:
        """If one loses its pin the test above stops meaning what it says."""
        assert [a for a in self.BARRIE if a not in build_corpus.SPELLING_CRITICAL] == []

    def test_the_fragment_alone_would_not_have_separated_them(self) -> None:
        """The gap the pin closes, asserted rather than described."""
        by_asin = {book["asin"]: book for book in corpus_books()}
        kensington = by_asin["B0C6FJ6L34"]
        # B002V1M36U's whole title is "Peter Pan", which is inside the Kensington title, and
        # both are credited to a name containing "Barrie".
        assert fragment_matches("Barrie", "Peter Pan", kensington)
        # Acceptance still refuses it, because the credited strings differ.
        assert not accepted("B002V1M36U", "Barrie", "Peter Pan", kensington)

    def test_removing_a_pin_makes_the_seed_ambiguous_again(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        without = {a: v for a, v in build_corpus.SPELLING_CRITICAL.items()
                   if a != "B002V1M36U"}
        monkeypatch.setattr(build_corpus, "SPELLING_CRITICAL", without)
        assert "B002V1M36U" in ambiguous_seeds()
