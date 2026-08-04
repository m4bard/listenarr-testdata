"""The corpus builder's verdict contract.

This module decides whether an identifier is allowed into the corpus, which makes it the guard
on the rule the whole repository rests on: nothing here is taken on trust. A bug that lets a
wrong book through does not announce itself, because the resulting corpus still looks plausible
and every downstream fixture inherits the mistake.

So these tests are mostly about refusal. The interesting question is not "does a good ASIN get
accepted" but "does a bad one get rejected, and does a run containing one refuse to write".

Only ``fetch`` touches the network, so it is replaced throughout. ``time.sleep`` is replaced too,
because the real one costs a quarter second per seed per region.
"""
from __future__ import annotations

import email.message
import json
import pathlib
import sys
import time
import urllib.error
import urllib.request
from typing import Any

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "tools"))

import build_corpus

# A response shaped like Audnex's, with only the fields the builder reads.
AUDNEX_OK: dict[str, Any] = {
    "title": "The Valley of Fear",
    "subtitle": None,
    "authors": [{"name": "Arthur Conan Doyle"}],
    "narrators": [{"name": "A Narrator"}],
    "seriesPrimary": {"name": "Sherlock Holmes", "asin": "B0SERIES1", "position": "7"},
    "releaseDate": "2009-03-11T00:00:00.000Z",
    "language": "english",
}

SEED = ("B002UUFXKU", "Arthur Conan Doyle", "The Valley of Fear", ["canonical"])


@pytest.fixture(autouse=True)
def no_sleeping(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)


def fake_fetch(responses: dict[str, Any]) -> Any:
    """A fetch that answers from a dict. A missing ASIN is an unresolvable one."""

    def _fetch(
        asin: str, region: str = build_corpus.DEFAULT_REGION
    ) -> tuple[dict | None, str | None]:
        key = f"{asin}@{region}"
        if key in responses:
            value = responses[key]
            return (None, value) if isinstance(value, str) else (value, None)
        if asin in responses:
            value = responses[asin]
            return (None, value) if isinstance(value, str) else (value, None)
        return None, "HTTP 404"

    return _fetch


class TestBuildAccepts:
    def test_a_matching_book_enters_the_corpus(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(build_corpus, "SEEDS", [SEED])
        monkeypatch.setattr(build_corpus, "fetch", fake_fetch({SEED[0]: AUDNEX_OK}))
        books, problems = build_corpus.build()
        assert problems == []
        assert [b["asin"] for b in books] == [SEED[0]]

    def test_it_records_the_fields_downstream_relies_on(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(build_corpus, "SEEDS", [SEED])
        monkeypatch.setattr(build_corpus, "fetch", fake_fetch({SEED[0]: AUDNEX_OK}))
        book = build_corpus.build()[0][0]
        assert book["title"] == "The Valley of Fear"
        assert book["authors"] == ["Arthur Conan Doyle"]
        assert book["narrators"] == ["A Narrator"]
        assert book["series"] == "Sherlock Holmes"
        assert book["series_asin"] == "B0SERIES1"
        assert book["series_position"] == "7"
        assert book["tags"] == ["canonical"]

    def test_the_release_date_is_truncated_to_a_plain_day(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Generated filenames use the year, so a full timestamp here leaks into paths."""
        monkeypatch.setattr(build_corpus, "SEEDS", [SEED])
        monkeypatch.setattr(build_corpus, "fetch", fake_fetch({SEED[0]: AUDNEX_OK}))
        assert build_corpus.build()[0][0]["release_date"] == "2009-03-11"

    def test_a_book_with_no_series_is_still_accepted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        standalone = {**AUDNEX_OK, "seriesPrimary": None}
        monkeypatch.setattr(build_corpus, "SEEDS", [SEED])
        monkeypatch.setattr(build_corpus, "fetch", fake_fetch({SEED[0]: standalone}))
        books, problems = build_corpus.build()
        assert problems == []
        assert books[0]["series"] is None and books[0]["series_position"] is None


@pytest.mark.contract
class TestBuildRefuses:
    """The half that matters. Every one of these must keep the book OUT."""

    def test_an_unresolvable_asin_is_excluded_and_reported(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(build_corpus, "SEEDS", [SEED])
        monkeypatch.setattr(build_corpus, "fetch", fake_fetch({SEED[0]: "HTTP 404"}))
        books, problems = build_corpus.build()
        assert books == []
        assert len(problems) == 1 and "unresolvable" in problems[0]

    def test_a_different_title_is_excluded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        wrong = {**AUDNEX_OK, "title": "A Study in Scarlet"}
        monkeypatch.setattr(build_corpus, "SEEDS", [SEED])
        monkeypatch.setattr(build_corpus, "fetch", fake_fetch({SEED[0]: wrong}))
        books, problems = build_corpus.build()
        assert books == []
        assert "A Study in Scarlet" in problems[0]

    def test_a_different_author_is_excluded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        wrong = {**AUDNEX_OK, "authors": [{"name": "Agatha Christie"}]}
        monkeypatch.setattr(build_corpus, "SEEDS", [SEED])
        monkeypatch.setattr(build_corpus, "fetch", fake_fetch({SEED[0]: wrong}))
        books, problems = build_corpus.build()
        assert books == []
        assert "Agatha Christie" in problems[0]

    def test_an_empty_response_is_excluded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A 200 carrying nothing must not be read as agreement."""
        monkeypatch.setattr(build_corpus, "SEEDS", [SEED])
        monkeypatch.setattr(build_corpus, "fetch", fake_fetch({SEED[0]: {}}))
        books, problems = build_corpus.build()
        assert books == []
        assert problems

    def test_one_bad_seed_does_not_take_the_good_ones_with_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        other = ("B002UUON10", "Rudyard Kipling", "Stalky", ["canonical"])
        other_ok = {**AUDNEX_OK, "title": "Stalky and Co.",
                    "authors": [{"name": "Rudyard Kipling"}], "seriesPrimary": None}
        monkeypatch.setattr(build_corpus, "SEEDS", [SEED, other])
        monkeypatch.setattr(
            build_corpus, "fetch", fake_fetch({SEED[0]: "HTTP 404", other[0]: other_ok})
        )
        books, problems = build_corpus.build()
        assert [b["asin"] for b in books] == [other[0]]
        assert len(problems) == 1


@pytest.mark.contract
class TestMatchingIsSubstringBased:
    """Characterisation, not endorsement.

    The acceptance check asks whether the expected title and author appear *within* what came
    back. These tests pin that as it stands so a future tightening is a deliberate, visible
    change rather than a silent one. The last case shows the looseness plainly: a seed whose
    expected title is a substring of a different real book is accepted.
    """

    def test_a_seed_title_that_is_a_prefix_of_another_book_is_accepted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seed = ("B0000000AA", "H. Rider Haggard", "She", ["canonical"])
        other_book = {**AUDNEX_OK, "title": "She and Allan",
                      "authors": [{"name": "H. Rider Haggard"}], "seriesPrimary": None}
        monkeypatch.setattr(build_corpus, "SEEDS", [seed])
        monkeypatch.setattr(build_corpus, "fetch", fake_fetch({seed[0]: other_book}))
        books, problems = build_corpus.build()
        assert problems == []
        assert books[0]["title"] == "She and Allan"

    def test_matching_ignores_case(self, monkeypatch: pytest.MonkeyPatch) -> None:
        shouty = {**AUDNEX_OK, "title": "THE VALLEY OF FEAR"}
        monkeypatch.setattr(build_corpus, "SEEDS", [SEED])
        monkeypatch.setattr(build_corpus, "fetch", fake_fetch({SEED[0]: shouty}))
        assert build_corpus.build()[1] == []

    def test_one_author_among_several_is_enough(self, monkeypatch: pytest.MonkeyPatch) -> None:
        collaboration = {**AUDNEX_OK,
                         "authors": [{"name": "Someone Else"}, {"name": "Arthur Conan Doyle"}]}
        monkeypatch.setattr(build_corpus, "SEEDS", [SEED])
        monkeypatch.setattr(build_corpus, "fetch", fake_fetch({SEED[0]: collaboration}))
        assert build_corpus.build()[1] == []


@pytest.mark.contract
class TestRegionLockIsAnAssertion:
    """The regional claim is that an ASIN is invisible outside its own marketplace.

    That is asserted upstream, so a silent failure here would mean publishing something untrue.
    """

    def test_visible_only_at_home_is_the_passing_case(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seed = ("B00REGION1", "de", "Grimm", "Kinder", ["region-lock"])
        home_ok = {**AUDNEX_OK, "title": "Kinder- und Hausmärchen",
                   "authors": [{"name": "Grimm"}], "seriesPrimary": None}
        monkeypatch.setattr(build_corpus, "REGIONAL_SEEDS", [seed])
        monkeypatch.setattr(build_corpus, "fetch", fake_fetch({f"{seed[0]}@de": home_ok}))
        proofs, problems = build_corpus.check_region_lock()
        assert problems == []
        assert proofs[0]["visibility"]["de"] == "ok"

    def test_visible_outside_home_breaks_the_claim(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seed = ("B00REGION1", "de", "Grimm", "Kinder", ["region-lock"])
        book = {**AUDNEX_OK, "title": "Kinder- und Hausmärchen",
                "authors": [{"name": "Grimm"}], "seriesPrimary": None}
        other = ("B00REGION2", "us", "Someone", "Something", ["region-lock"])
        monkeypatch.setattr(build_corpus, "REGIONAL_SEEDS", [seed, other])
        # Visible in both regions: exactly what must be caught.
        monkeypatch.setattr(
            build_corpus, "fetch",
            fake_fetch({f"{seed[0]}@de": book, f"{seed[0]}@us": book}),
        )
        _, problems = build_corpus.check_region_lock()
        assert any("region-lock claim is broken" in p for p in problems)

    def test_not_resolving_at_home_is_a_problem(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seed = ("B00REGION1", "de", "Grimm", "Kinder", ["region-lock"])
        monkeypatch.setattr(build_corpus, "REGIONAL_SEEDS", [seed])
        monkeypatch.setattr(build_corpus, "fetch", fake_fetch({}))
        _, problems = build_corpus.check_region_lock()
        assert any("does NOT resolve in its own region" in p for p in problems)


@pytest.mark.contract
class TestTheRunRefusesToWrite:
    """The outcome that would actually damage the repository is a corpus written anyway."""

    @pytest.fixture(autouse=True)
    def _quiet_and_isolated(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        monkeypatch.setattr(build_corpus, "OUT", tmp_path / "corpus.json")
        monkeypatch.setattr(build_corpus, "ROOT", tmp_path)
        monkeypatch.setattr(build_corpus, "REGIONAL_SEEDS", [])
        monkeypatch.setattr(sys, "argv", ["build_corpus.py"])

    def test_a_clean_run_writes_the_corpus_and_exits_zero(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(build_corpus, "SEEDS", [SEED])
        monkeypatch.setattr(build_corpus, "fetch", fake_fetch({SEED[0]: AUDNEX_OK}))
        assert build_corpus.main() == 0
        written = json.loads(build_corpus.OUT.read_text())
        assert [b["asin"] for b in written["books"]] == [SEED[0]]

    def test_a_mismatch_refuses_to_write_anything(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        wrong = {**AUDNEX_OK, "title": "A Study in Scarlet"}
        monkeypatch.setattr(build_corpus, "SEEDS", [SEED])
        monkeypatch.setattr(build_corpus, "fetch", fake_fetch({SEED[0]: wrong}))
        assert build_corpus.main() == 1
        assert not build_corpus.OUT.exists()

    def test_it_does_not_write_a_corpus_with_the_bad_seed_merely_dropped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Partial success is the tempting failure: one good book is not a corpus."""
        other = ("B002UUON10", "Rudyard Kipling", "Stalky", ["canonical"])
        other_ok = {**AUDNEX_OK, "title": "Stalky and Co.",
                    "authors": [{"name": "Rudyard Kipling"}], "seriesPrimary": None}
        monkeypatch.setattr(build_corpus, "SEEDS", [SEED, other])
        monkeypatch.setattr(
            build_corpus, "fetch", fake_fetch({SEED[0]: "HTTP 404", other[0]: other_ok})
        )
        assert build_corpus.main() == 1
        assert not build_corpus.OUT.exists()

    def test_check_mode_reports_drift_without_writing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        wrong = {**AUDNEX_OK, "title": "A Study in Scarlet"}
        monkeypatch.setattr(build_corpus, "SEEDS", [SEED])
        monkeypatch.setattr(build_corpus, "fetch", fake_fetch({SEED[0]: wrong}))
        monkeypatch.setattr(sys, "argv", ["build_corpus.py", "--check"])
        assert build_corpus.main() == 1
        assert not build_corpus.OUT.exists()

    def test_check_mode_on_a_clean_corpus_exits_zero_and_writes_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(build_corpus, "SEEDS", [SEED])
        monkeypatch.setattr(build_corpus, "fetch", fake_fetch({SEED[0]: AUDNEX_OK}))
        monkeypatch.setattr(sys, "argv", ["build_corpus.py", "--check"])
        assert build_corpus.main() == 0
        assert not build_corpus.OUT.exists()


@pytest.mark.contract
class TestFetchClassifiesFailures:
    """`fetch` decides what counts as unresolvable, so a bug here makes bad ASINs look fine."""

    def test_a_good_response_is_parsed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class Response:
            def read(self) -> bytes:
                return json.dumps(AUDNEX_OK).encode()

            def __enter__(self) -> Response:
                return self

            def __exit__(self, *_: object) -> None:
                return None

        monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: Response())
        data, err = build_corpus.fetch("B002UUFXKU")
        assert err is None and data is not None and data["title"] == "The Valley of Fear"

    def test_an_http_error_becomes_a_status_not_a_book(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def raise_404(*_a: object, **_k: object) -> None:
            raise urllib.error.HTTPError(
                url="u", code=404, msg="Not Found", hdrs=email.message.Message(), fp=None
            )

        monkeypatch.setattr(urllib.request, "urlopen", raise_404)
        data, err = build_corpus.fetch("B000000000")
        assert data is None and err == "HTTP 404"

    def test_any_other_failure_is_also_a_refusal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A timeout must not be mistaken for a book that does not exist, nor for one that does."""

        def raise_timeout(*_a: object, **_k: object) -> None:
            raise TimeoutError("too slow")

        monkeypatch.setattr(urllib.request, "urlopen", raise_timeout)
        data, err = build_corpus.fetch("B002UUFXKU")
        assert data is None and err == "TimeoutError"


@pytest.mark.contract
def test_a_regional_asin_resolving_to_the_wrong_book_at_home_is_caught(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same guard as the main path, on the regional one, where it was previously untested."""
    seed = ("B00REGION1", "de", "Grimm", "Kinder", ["region-lock"])
    wrong_book = {**AUDNEX_OK, "title": "Something Else Entirely",
                  "authors": [{"name": "Not Grimm"}], "seriesPrimary": None}
    monkeypatch.setattr(build_corpus, "REGIONAL_SEEDS", [seed])
    monkeypatch.setattr(build_corpus, "fetch", fake_fetch({f"{seed[0]}@de": wrong_book}))
    proofs, problems = build_corpus.check_region_lock()
    assert any("Something Else Entirely" in p for p in problems)
    assert "title" not in proofs[0]
