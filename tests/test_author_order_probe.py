"""Contract tests for the author-credit-order probe's verdict.

The probe answers one question: can a role-suffixed contributor occupy index 0, which is the
position Readarr's "keep the first contributor" rule keeps. The ways it could answer that
wrongly are what these tests pin:

* false SAFE from a blind detector - a role detector that matched nothing would report zero
                                     role-suffixed names at index 0, which reads exactly like
                                     "the catalogue always credits the author first"
* false UNSAFE from a greedy one   - a detector that matched every name would report the
                                     opposite, so the run must also prove it found books whose
                                     first credit is a plain author name
* a verdict with no control at all - a capture holding only role-suffixed-first products
                                     cannot distinguish those two failures, so it must refuse
                                     to answer rather than report the finding
* a clean answer off a tiny sample  - three multi-contributor records that happen not to
                                     contain the case look exactly like a catalogue that never
                                     produces it, and the public corpus is exactly that size

The exit code carries the verdict, because that is what a caller checks. 0 is "no role suffix
was ever first in this capture", 1 is "one was", and 2 is "this capture cannot tell you".
"""

from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

from author_order_probe import MIN_MULTI, carries_role, classify, is_role_tail, report

# Verbatim from Audible's catalogue, captured 2026-09-21. Order is the catalogue's own.
AUTHOR_FIRST = ["Fyodor Dostoevsky", "Constance Garnett - translator"]   # B002V9ZF3K
ROLE_FIRST = ["Constance Garnett - translator", "Fyodor Dostoevsky"]     # B00EZAXAF8
NO_ROLE = ["O. Henry", "William Sydney Porter"]                          # B007ZEANIS
ALL_ROLE = ["Lisa Morton - editor", "Leslie S. Klinger - editor"]        # 1094179574


def test_role_suffix_is_recognised_where_it_appears() -> None:
    assert carries_role("Constance Garnett - translator")
    assert carries_role("Guy Newland - editor and translator")
    assert carries_role("Marty Ross - adapted by")


def test_a_dash_is_not_by_itself_a_role() -> None:
    """The detector must not treat every dashed tail as a credit.

    Audible credits names that carry a transliteration or a slashed double credit after a
    dash. A rule that fired on the dash alone would maim those names and, worse, would make
    the safety verdict meaningless by matching almost everything.
    """
    assert not carries_role("Yang Jing - Yang Jing")
    assert not carries_role("Jonathan Maberry - editor/author")
    assert not carries_role("Fyodor Dostoevsky")
    assert not is_role_tail("")


def test_classification_separates_the_three_positions() -> None:
    buckets = classify({
        "role_first": ROLE_FIRST,
        "author_first": AUTHOR_FIRST,
        "clean": NO_ROLE,
    })
    assert [asin for asin, _ in buckets["role_first"]] == ["role_first"]
    assert [asin for asin, _ in buckets["role_later"]] == ["author_first"]
    assert [asin for asin, _ in buckets["no_role"]] == ["clean"]


def test_a_capture_with_a_role_first_reports_unsafe() -> None:
    assert report({"a": ROLE_FIRST, "b": AUTHOR_FIRST, "c": NO_ROLE}) == 1


def test_a_capture_with_none_first_reports_safe() -> None:
    capture = {f"ok{i}": AUTHOR_FIRST for i in range(MIN_MULTI)}
    capture["clean"] = NO_ROLE
    assert report(capture) == 0


def test_a_small_capture_refuses_rather_than_reporting_clean() -> None:
    """The failure the public corpus actually produces.

    Three multi-contributor records with no role-suffixed first credit is what the committed
    corpus gives, and reading that as "the catalogue always credits the author first" is
    wrong: the wider sample finds the case without difficulty. Too little data and no data
    are different failures, and both have to refuse.
    """
    assert report({"b": AUTHOR_FIRST, "c": NO_ROLE}) == 2


def test_a_small_capture_still_reports_a_finding_it_did_see() -> None:
    """Size gates the all-clear, never the finding.

    One product crediting a role-suffixed name first is proof the case exists, however small
    the capture. Refusing to report it because the sample is thin would lose the one thing a
    small sample can establish.
    """
    assert report({"a": ROLE_FIRST, "b": AUTHOR_FIRST, "c": NO_ROLE}) == 1


def test_a_capture_with_no_control_refuses_to_answer() -> None:
    """The apparatus-failure case: every product role-suffixed first proves nothing.

    A detector matching every string produces exactly this capture, and so does a catalogue
    that really does credit contributors first. Without a single product whose first credit
    is a plain author name there is nothing that had to come out differently, so the probe
    must report that rather than the finding.
    """
    assert report({"a": ROLE_FIRST, "b": ROLE_FIRST[:]}) == 2


def test_books_whose_every_credit_is_role_suffixed_are_counted() -> None:
    """The failure mode of the other candidate rule, which must not be silently invisible.

    An anthology credited only to its editors lands in two buckets on purpose. Keeping the
    first credit keeps an editor, so it counts against the positional rule; dropping every
    role-suffixed credit leaves the book with nobody, so it counts against the other one.
    The single-credit form belongs only to the second, because there is no first-versus-rest
    question to answer when there is one name.
    """
    buckets = classify({"anthology": ALL_ROLE, "solo": ["Alice Wong - editor"]})
    assert sorted(asin for asin, _ in buckets["all_role"]) == ["anthology", "solo"]
    assert [asin for asin, _ in buckets["role_first"]] == ["anthology"]
    assert buckets["role_later"] == []
