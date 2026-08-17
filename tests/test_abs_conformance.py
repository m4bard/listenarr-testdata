"""Contract tests for the Audiobookshelf conformance verdict.

This check exists to make a documented folder shape verifiable. The ways it could quietly stop
checking anything matter more than the comparison itself:

* false PASS via --ignore    — a layout excuses itself from the field it was supposed to carry
* false PASS via absence     — no book directories, so nothing mismatched, so everything "passed"
* false PASS on the sidecar  — a file that ABS accepts while discarding its series reads as fine
* a mechanism that did not happen — reporting "left glued to the title" for a layout that never
                                    wrote a bracket at all

The last one is not a pass/fail bug, it is a truthfulness bug, and it is the one that would put a
wrong sentence in front of a maintainer. It gets a test for the same reason the others do.

The parsing itself is deliberately not tested here. It lives in Audiobookshelf's own modules,
invoked through the bridge, and reimplementing any of it to assert against would defeat the point.
"""
from __future__ import annotations

import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

from abs_conformance import (
    COMPARED,
    EXIT_FAIL,
    EXIT_INCONCLUSIVE,
    EXIT_PASS,
    BookResult,
    book_directories,
    main,
    render,
)

TRUTH = {
    "title": "The Valley of Fear",
    "asin": "B002UUFXKU",
    "series": "Sherlock Holmes",
    "sequence": "7",
    "author": "Arthur Conan Doyle",
}


def result(observed_overrides: dict | None = None, ignored: tuple = ()) -> BookResult:
    observed = dict(TRUTH)
    observed.update(observed_overrides or {})
    return BookResult(directory="d", expected=dict(TRUTH), observed=observed, ignored=ignored)


# --------------------------------------------------------------------------
# The comparison
# --------------------------------------------------------------------------

def test_a_clean_round_trip_survives():
    assert result().survived


def test_a_dropped_asin_is_a_mismatch():
    assert result({"asin": None}).mismatches == ["asin"]


def test_a_book_with_no_series_is_not_faulted_for_ABS_finding_none():
    r = BookResult(
        directory="d",
        expected=dict(TRUTH, series=None, sequence=None),
        observed=dict(TRUTH, series=None, sequence=None),
    )
    assert r.survived


def test_empty_string_and_none_are_treated_as_the_same_absence():
    r = BookResult(directory="d",
                   expected=dict(TRUTH, series=""),
                   observed=dict(TRUTH, series=None))
    assert r.survived


# --------------------------------------------------------------------------
# --ignore must narrow the check honestly, not hide failures
# --------------------------------------------------------------------------

def test_ignore_suppresses_only_the_named_field():
    r = result({"series": None, "asin": None}, ignored=("series",))
    assert r.mismatches == ["asin"], "ignoring series must not also excuse a dropped ASIN"


def test_ignore_cannot_excuse_the_asin(tmp_path):
    """The ASIN is the whole point of the layout. Nothing should be able to wave it through."""
    r = result({"asin": None}, ignored=("asin",))
    # It is suppressible in the data model, so the guard has to be that no shipped case does it.
    # This test documents the hazard and pins the behaviour rather than pretending it cannot happen.
    assert r.survived

    # The real guard is that no shipped case ignores it. Read the ignore argument of every
    # run_case line in the driver, which is the quoted 4th field, and assert none names asin
    # or title. Matching the raw line would trip over the layout being called
    # 'audiobookshelf-asin', which is a name and not an instruction.
    import re
    driver = (ROOT / "tools" / "validate_abs_layout.sh").read_text()
    invocations = re.findall(r"^run_case\s+\S+\s+\S+\s+\S+\s*(\"[^\"]*\")?",
                             driver, re.MULTILINE)
    assert invocations, "no run_case invocations found; the guard is not reading the driver"
    for raw in invocations:
        fields = {f.strip() for f in raw.strip('"').split(",") if f.strip()}
        assert "asin" not in fields, f"a shipped case ignores the ASIN: {raw}"
        assert "title" not in fields, f"a shipped case ignores the title: {raw}"


def test_unknown_ignore_field_is_refused(tmp_path, capsys):
    fx = write_fixture(tmp_path)
    rc = main(["--manifest", str(fx["manifest"]), "--abs-repo", str(fx["abs_repo"]),
               "--ignore", "narrator"])
    assert rc == EXIT_INCONCLUSIVE


# --------------------------------------------------------------------------
# Reporting must not assert a mechanism that did not occur
# --------------------------------------------------------------------------

def test_missing_field_without_title_damage_is_not_called_pollution():
    r = result({"asin": None})
    out = render([r], [], "t", [])
    assert "carries no asin" in out
    assert "glued" not in out and "still attached" not in out


def test_title_damage_is_reported_as_such():
    r = result({"title": "1a - The Valley of Fear [b002uufxku]", "asin": None, "sequence": None})
    out = render([r], [], "t", [])
    assert "the title did not survive" in out


# --------------------------------------------------------------------------
# Sidecar reporting
# --------------------------------------------------------------------------

def test_accepted_but_discarded_series_is_called_out():
    out = render([result()], [], "t", [
        {"label": "structured", "parsed": {"title": "x"}, "series": [],
         "offeredSeries": 1, "keptSeries": 0}])
    assert "ACCEPTED but all 1 series entries were discarded" in out


def test_a_kept_series_is_shown_with_its_sequence():
    out = render([result()], [], "t", [
        {"label": "string", "parsed": {"title": "x"},
         "series": [{"name": "Sherlock Holmes", "sequence": "1a"}],
         "offeredSeries": 1, "keptSeries": 1}])
    assert "'Sherlock Holmes' #'1a'" in out


def test_no_series_offered_is_not_confused_with_discarded():
    out = render([result()], [], "t", [
        {"label": "none", "parsed": {"title": "x"}, "series": [],
         "offeredSeries": 0, "keptSeries": 0}])
    assert "no series offered" in out
    assert "discarded" not in out


def test_a_rejected_sidecar_is_reported():
    out = render([result()], [], "t", [{"label": "junk", "parsed": None}])
    assert "REJECTED" in out


# --------------------------------------------------------------------------
# Manifest reading and end-to-end refusals
# --------------------------------------------------------------------------

def write_fixture(tmp_path: pathlib.Path, entries: list | None = None) -> dict:
    library = tmp_path / "library"
    library.mkdir()
    manifest = library / "manifest.json"
    manifest.write_text(json.dumps({"entries": entries if entries is not None else [{
        "path": "Arthur Conan Doyle/Sherlock Holmes/7 - The Valley of Fear [B002UUFXKU]/a.m4b",
        "kind": "book", "belongs_to_asin": "B002UUFXKU",
        "true_title": "The Valley of Fear", "true_authors": ["Arthur Conan Doyle"],
        "true_series": "Sherlock Holmes", "true_series_position": "7"}]}))
    abs_repo = tmp_path / "abs"
    (abs_repo / "server" / "utils").mkdir(parents=True)
    (abs_repo / "server" / "utils" / "scandir.js").write_text("// stand-in")
    return {"manifest": manifest, "abs_repo": abs_repo}


def test_one_directory_per_book_not_one_per_file(tmp_path):
    """A multi-file book is one directory for ABS, not three."""
    entries = [{
        "path": f"A/S/1 - T [B000000000]/part{n}.m4b", "kind": "book",
        "belongs_to_asin": "B000000000", "true_title": "T", "true_authors": ["A"],
        "true_series": "S", "true_series_position": "1"} for n in (1, 2, 3)]
    fx = write_fixture(tmp_path, entries)
    assert len(book_directories(fx["manifest"])) == 1


def test_loose_files_at_the_root_are_skipped(tmp_path):
    fx = write_fixture(tmp_path, [{
        "path": "loose.m4b", "kind": "book", "belongs_to_asin": "B000000000",
        "true_title": "T", "true_authors": ["A"]}])
    assert book_directories(fx["manifest"]) == []


def test_a_manifest_with_no_books_is_inconclusive_not_a_pass(tmp_path):
    fx = write_fixture(tmp_path, [])
    assert main(["--manifest", str(fx["manifest"]),
                 "--abs-repo", str(fx["abs_repo"])]) == EXIT_INCONCLUSIVE


def test_a_directory_that_is_not_audiobookshelf_is_refused(tmp_path):
    fx = write_fixture(tmp_path)
    assert main(["--manifest", str(fx["manifest"]),
                 "--abs-repo", str(tmp_path)]) == EXIT_INCONCLUSIVE


def test_a_missing_manifest_is_refused(tmp_path):
    fx = write_fixture(tmp_path)
    fx["manifest"].unlink()
    assert main(["--manifest", str(fx["manifest"]),
                 "--abs-repo", str(fx["abs_repo"])]) == EXIT_INCONCLUSIVE
