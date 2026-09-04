"""The corpus builder's verdict contract.

This module decides whether an identifier is allowed into the corpus, which makes it the guard
on the rule the whole repository rests on: nothing here is taken on trust. A bug that lets a
wrong book through does not announce itself, because the resulting corpus still looks plausible
and every downstream fixture inherits the mistake.

So these tests are mostly about refusal. The interesting question is not "does a good ASIN get
accepted" but "does a bad one get rejected, and does a run containing one refuse to write".

Only ``fetch`` and ``fetch_librivox`` touch the network, so they are replaced throughout.
``time.sleep`` is replaced too, because the real one costs a quarter second per seed per region
and a full second per LibriVox project.
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

# Captured before the autouse fixture replaces it, for the few tests about the fetch itself.
REAL_FETCH_LIBRIVOX = build_corpus.fetch_librivox

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

# A LibriVox project record, reduced to the fields the builder reads, and the verified map
# a build is handed. Every seed below pins this one recording.
LIBRIVOX_RAW: dict[str, Any] = {
    "id": "3664",
    "title": "Valley of Fear",
    "authors": [{"first_name": "Sir Arthur Conan", "last_name": "Doyle"}],
    "language": "English",
    "url_librivox": "https://librivox.org/the-valley-of-fear-by-sir-arthur-conan-doyle",
}

LIBRIVOX_OK: dict[str, dict] = {
    "3664": {
        "id": "3664",
        "title": "Valley of Fear",
        "authors": ["Sir Arthur Conan Doyle"],
        "language": "English",
        "url": "https://librivox.org/the-valley-of-fear-by-sir-arthur-conan-doyle",
    }
}

SEED = ("B002UUFXKU", "Arthur Conan Doyle", "The Valley of Fear", ["canonical"], "3664")


@pytest.fixture(autouse=True)
def no_sleeping(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)


@pytest.fixture(autouse=True)
def no_librivox_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing in this file may reach librivox.org, including through ``main``."""
    monkeypatch.setattr(build_corpus, "fetch_librivox", fake_fetch_librivox({"3664": LIBRIVOX_RAW}))


def fake_fetch_librivox(responses: dict[str, Any]) -> Any:
    """A LibriVox fetch that answers from a dict. A missing id is an absent project."""

    def _fetch(book_id: str) -> tuple[dict | None, str | None]:
        if book_id in responses:
            value = responses[book_id]
            return (None, value) if isinstance(value, str) else (value, None)
        return None, "HTTP 404"

    return _fetch


ONE_RECORDING: dict[str, tuple[str, str]] = {"3664": ("Doyle", "Valley of Fear")}


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
        books, problems = build_corpus.build(LIBRIVOX_OK)
        assert problems == []
        assert [b["asin"] for b in books] == [SEED[0]]

    def test_it_records_the_fields_downstream_relies_on(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(build_corpus, "SEEDS", [SEED])
        monkeypatch.setattr(build_corpus, "fetch", fake_fetch({SEED[0]: AUDNEX_OK}))
        book = build_corpus.build(LIBRIVOX_OK)[0][0]
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
        assert build_corpus.build(LIBRIVOX_OK)[0][0]["release_date"] == "2009-03-11"

    def test_a_book_with_no_series_is_still_accepted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        standalone = {**AUDNEX_OK, "seriesPrimary": None}
        monkeypatch.setattr(build_corpus, "SEEDS", [SEED])
        monkeypatch.setattr(build_corpus, "fetch", fake_fetch({SEED[0]: standalone}))
        books, problems = build_corpus.build(LIBRIVOX_OK)
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
        books, problems = build_corpus.build(LIBRIVOX_OK)
        assert books == []
        assert len(problems) == 1 and "unresolvable" in problems[0]

    def test_a_different_title_is_excluded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        wrong = {**AUDNEX_OK, "title": "A Study in Scarlet"}
        monkeypatch.setattr(build_corpus, "SEEDS", [SEED])
        monkeypatch.setattr(build_corpus, "fetch", fake_fetch({SEED[0]: wrong}))
        books, problems = build_corpus.build(LIBRIVOX_OK)
        assert books == []
        assert "A Study in Scarlet" in problems[0]

    def test_a_different_author_is_excluded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        wrong = {**AUDNEX_OK, "authors": [{"name": "Agatha Christie"}]}
        monkeypatch.setattr(build_corpus, "SEEDS", [SEED])
        monkeypatch.setattr(build_corpus, "fetch", fake_fetch({SEED[0]: wrong}))
        books, problems = build_corpus.build(LIBRIVOX_OK)
        assert books == []
        assert "Agatha Christie" in problems[0]

    def test_an_empty_response_is_excluded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A 200 carrying nothing must not be read as agreement."""
        monkeypatch.setattr(build_corpus, "SEEDS", [SEED])
        monkeypatch.setattr(build_corpus, "fetch", fake_fetch({SEED[0]: {}}))
        books, problems = build_corpus.build(LIBRIVOX_OK)
        assert books == []
        assert problems

    def test_one_bad_seed_does_not_take_the_good_ones_with_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        other = ("B002UUON10", "Rudyard Kipling", "Stalky", ["canonical"], "3664")
        other_ok = {**AUDNEX_OK, "title": "Stalky and Co.",
                    "authors": [{"name": "Rudyard Kipling"}], "seriesPrimary": None}
        monkeypatch.setattr(build_corpus, "SEEDS", [SEED, other])
        monkeypatch.setattr(
            build_corpus, "fetch", fake_fetch({SEED[0]: "HTTP 404", other[0]: other_ok})
        )
        books, problems = build_corpus.build(LIBRIVOX_OK)
        assert [b["asin"] for b in books] == [other[0]]
        assert len(problems) == 1


@pytest.mark.contract
class TestAnEmptyExpectationIsRefused:
    """A substring test against "" passes against anything, so an empty seed verifies nothing.

    This is the hole underneath the substring-versus-exact argument. Whichever way that goes,
    a seed expecting nothing is not a loose check, it is the absence of one, and it reports
    itself as ok.
    """

    def test_an_empty_expected_title_keeps_the_book_out(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seed = ("B0000000AA", "Arthur Conan Doyle", "", ["canonical"], "3664")
        monkeypatch.setattr(build_corpus, "SEEDS", [seed])
        monkeypatch.setattr(build_corpus, "fetch", fake_fetch({seed[0]: AUDNEX_OK}))
        books, problems = build_corpus.build(LIBRIVOX_OK)
        assert books == []
        assert len(problems) == 1 and "expected title is empty" in problems[0]

    def test_an_empty_expected_author_keeps_the_book_out(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seed = ("B0000000AA", "", "The Valley of Fear", ["canonical"], "3664")
        monkeypatch.setattr(build_corpus, "SEEDS", [seed])
        monkeypatch.setattr(build_corpus, "fetch", fake_fetch({seed[0]: AUDNEX_OK}))
        books, problems = build_corpus.build(LIBRIVOX_OK)
        assert books == []
        assert "expected author is empty" in problems[0]

    def test_whitespace_is_not_a_fragment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A single space is a substring of nearly every title, so it is the same hole."""
        seed = ("B0000000AA", "Arthur Conan Doyle", " ", ["canonical"], "3664")
        monkeypatch.setattr(build_corpus, "SEEDS", [seed])
        monkeypatch.setattr(build_corpus, "fetch", fake_fetch({seed[0]: AUDNEX_OK}))
        books, problems = build_corpus.build(LIBRIVOX_OK)
        assert books == []
        assert "expected title is empty" in problems[0]

    def test_the_refusal_happens_without_fetching_the_asin(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Nothing the API says can rescue the seed, so asking it wastes a request."""
        asked: list[str] = []

        def recording_fetch(
            asin: str, region: str = build_corpus.DEFAULT_REGION
        ) -> tuple[dict | None, str | None]:
            asked.append(asin)
            return AUDNEX_OK, None

        monkeypatch.setattr(build_corpus, "SEEDS", [("B0000000AA", "", "", ["canonical"], "3664")])
        monkeypatch.setattr(build_corpus, "fetch", recording_fetch)
        build_corpus.build(LIBRIVOX_OK)
        assert asked == []

    def test_a_regional_seed_expecting_nothing_is_refused_too(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The region-lock loop runs the same substring check and had the same hole."""
        seed = ("B00REGION1", "de", "Grimm", "", ["region-lock"], "3664")
        monkeypatch.setattr(build_corpus, "REGIONAL_SEEDS", [seed])
        monkeypatch.setattr(build_corpus, "fetch", fake_fetch({f"{seed[0]}@de": AUDNEX_OK}))
        proofs, problems = build_corpus.check_region_lock(LIBRIVOX_OK)
        assert proofs == []
        assert any("expected title is empty" in p for p in problems)

    def test_a_run_containing_one_refuses_to_write(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        monkeypatch.setattr(build_corpus, "OUT", tmp_path / "corpus.json")
        monkeypatch.setattr(build_corpus, "ROOT", tmp_path)
        monkeypatch.setattr(build_corpus, "REGIONAL_SEEDS", [])
        monkeypatch.setattr(build_corpus, "LIBRIVOX_RECORDINGS", ONE_RECORDING)
        # SPELLING_CRITICAL is keyed to SEEDS the same way LIBRIVOX_RECORDINGS is, and
        # these tests replace SEEDS with one fixture seed. Leaving the committed table in
        # place would fail every run here on entries for seeds this fixture removed.
        monkeypatch.setattr(build_corpus, "SPELLING_CRITICAL", {})
        monkeypatch.setattr(sys, "argv", ["build_corpus.py"])
        monkeypatch.setattr(
            build_corpus, "SEEDS", [("B0000000AA", "Doyle", "", ["canonical"], "3664")]
        )
        monkeypatch.setattr(build_corpus, "fetch", fake_fetch({"B0000000AA": AUDNEX_OK}))
        assert build_corpus.main() == 1
        assert not build_corpus.OUT.exists()

    def test_every_committed_seed_carries_a_real_fragment(self) -> None:
        """The seed table itself, not a fixture: no shipped seed may expect nothing."""
        unusable = [
            build_corpus.check_fragment(asin, author, title)
            for asin, author, title, _tags, _librivox in build_corpus.SEEDS
        ] + [
            build_corpus.check_fragment(asin, author, title)
            for asin, _region, author, title, _tags, _librivox in build_corpus.REGIONAL_SEEDS
        ]
        assert [problem for problem in unusable if problem is not None] == []


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
        seed = ("B0000000AA", "H. Rider Haggard", "She", ["canonical"], "3664")
        other_book = {**AUDNEX_OK, "title": "She and Allan",
                      "authors": [{"name": "H. Rider Haggard"}], "seriesPrimary": None}
        monkeypatch.setattr(build_corpus, "SEEDS", [seed])
        monkeypatch.setattr(build_corpus, "fetch", fake_fetch({seed[0]: other_book}))
        books, problems = build_corpus.build(LIBRIVOX_OK)
        assert problems == []
        assert books[0]["title"] == "She and Allan"

    def test_matching_ignores_case(self, monkeypatch: pytest.MonkeyPatch) -> None:
        shouty = {**AUDNEX_OK, "title": "THE VALLEY OF FEAR"}
        monkeypatch.setattr(build_corpus, "SEEDS", [SEED])
        monkeypatch.setattr(build_corpus, "fetch", fake_fetch({SEED[0]: shouty}))
        assert build_corpus.build(LIBRIVOX_OK)[1] == []

    def test_one_author_among_several_is_enough(self, monkeypatch: pytest.MonkeyPatch) -> None:
        collaboration = {**AUDNEX_OK,
                         "authors": [{"name": "Someone Else"}, {"name": "Arthur Conan Doyle"}]}
        monkeypatch.setattr(build_corpus, "SEEDS", [SEED])
        monkeypatch.setattr(build_corpus, "fetch", fake_fetch({SEED[0]: collaboration}))
        assert build_corpus.build(LIBRIVOX_OK)[1] == []


@pytest.mark.contract
class TestRegionLockIsAnAssertion:
    """The regional claim is that an ASIN is invisible outside its own marketplace.

    That is asserted upstream, so a silent failure here would mean publishing something untrue.
    """

    def test_visible_only_at_home_is_the_passing_case(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seed = ("B00REGION1", "de", "Grimm", "Kinder", ["region-lock"], "3664")
        home_ok = {**AUDNEX_OK, "title": "Kinder- und Hausmärchen",
                   "authors": [{"name": "Grimm"}], "seriesPrimary": None}
        monkeypatch.setattr(build_corpus, "REGIONAL_SEEDS", [seed])
        monkeypatch.setattr(build_corpus, "fetch", fake_fetch({f"{seed[0]}@de": home_ok}))
        proofs, problems = build_corpus.check_region_lock(LIBRIVOX_OK)
        assert problems == []
        assert proofs[0]["visibility"]["de"] == "ok"

    def test_visible_outside_home_breaks_the_claim(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seed = ("B00REGION1", "de", "Grimm", "Kinder", ["region-lock"], "3664")
        book = {**AUDNEX_OK, "title": "Kinder- und Hausmärchen",
                "authors": [{"name": "Grimm"}], "seriesPrimary": None}
        other = ("B00REGION2", "us", "Someone", "Something", ["region-lock"], "3664")
        monkeypatch.setattr(build_corpus, "REGIONAL_SEEDS", [seed, other])
        # Visible in both regions: exactly what must be caught.
        monkeypatch.setattr(
            build_corpus, "fetch",
            fake_fetch({f"{seed[0]}@de": book, f"{seed[0]}@us": book}),
        )
        _, problems = build_corpus.check_region_lock(LIBRIVOX_OK)
        assert any("region-lock claim is broken" in p for p in problems)

    def test_not_resolving_at_home_is_a_problem(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seed = ("B00REGION1", "de", "Grimm", "Kinder", ["region-lock"], "3664")
        monkeypatch.setattr(build_corpus, "REGIONAL_SEEDS", [seed])
        monkeypatch.setattr(build_corpus, "fetch", fake_fetch({}))
        _, problems = build_corpus.check_region_lock(LIBRIVOX_OK)
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
        monkeypatch.setattr(build_corpus, "LIBRIVOX_RECORDINGS", ONE_RECORDING)
        # SPELLING_CRITICAL is keyed to SEEDS the same way LIBRIVOX_RECORDINGS is, and
        # these tests replace SEEDS with one fixture seed. Leaving the committed table in
        # place would fail every run here on entries for seeds this fixture removed.
        monkeypatch.setattr(build_corpus, "SPELLING_CRITICAL", {})
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
        other = ("B002UUON10", "Rudyard Kipling", "Stalky", ["canonical"], "3664")
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
    seed = ("B00REGION1", "de", "Grimm", "Kinder", ["region-lock"], "3664")
    wrong_book = {**AUDNEX_OK, "title": "Something Else Entirely",
                  "authors": [{"name": "Not Grimm"}], "seriesPrimary": None}
    monkeypatch.setattr(build_corpus, "REGIONAL_SEEDS", [seed])
    monkeypatch.setattr(build_corpus, "fetch", fake_fetch({f"{seed[0]}@de": wrong_book}))
    proofs, problems = build_corpus.check_region_lock(LIBRIVOX_OK)
    assert any("Something Else Entirely" in p for p in problems)
    assert "title" not in proofs[0]


@pytest.mark.contract
class TestThePublicDomainClaimIsChecked:
    """The README says every book is public domain. This is what makes that a check.

    LibriVox settles the rights question before it records anything, so a work it has recorded
    is one whose analysis somebody with a reason to get it right has already published. The
    seed names a project id; if that project does not resolve, or resolves to a different book,
    the entry has no evidence behind it and must not be written.
    """

    def test_a_seed_whose_recording_did_not_verify_is_excluded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(build_corpus, "SEEDS", [SEED])
        monkeypatch.setattr(build_corpus, "fetch", fake_fetch({SEED[0]: AUDNEX_OK}))
        books, problems = build_corpus.build({})
        assert books == []
        assert len(problems) == 1 and "public-domain status is unproven" in problems[0]

    def test_the_asin_is_not_even_fetched_without_a_verified_recording(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No Audnex answer can supply the missing evidence, so asking wastes a request."""
        asked: list[str] = []

        def recording_fetch(
            asin: str, region: str = build_corpus.DEFAULT_REGION
        ) -> tuple[dict | None, str | None]:
            asked.append(asin)
            return AUDNEX_OK, None

        monkeypatch.setattr(build_corpus, "SEEDS", [SEED])
        monkeypatch.setattr(build_corpus, "fetch", recording_fetch)
        build_corpus.build({})
        assert asked == []

    def test_an_accepted_book_carries_the_recording_it_rests_on(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The evidence is written down, so a reader can check it without rerunning anything."""
        monkeypatch.setattr(build_corpus, "SEEDS", [SEED])
        monkeypatch.setattr(build_corpus, "fetch", fake_fetch({SEED[0]: AUDNEX_OK}))
        book = build_corpus.build(LIBRIVOX_OK)[0][0]
        assert book["librivox_id"] == "3664"
        assert book["librivox_title"] == "Valley of Fear"
        assert book["librivox_url"].startswith("https://librivox.org/")
        assert book["librivox_language"] == "English"
        assert book["librivox_same_language"] is True

    def test_a_recording_in_another_language_is_recorded_as_such(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The honest half of the claim: the WORK is public domain, this edition's audio is not
        shown to exist. LibriVox has no Russian War and Peace, and the corpus says so rather
        than implying a Russian recording is free to download."""
        russian = {**AUDNEX_OK, "language": "russian"}
        monkeypatch.setattr(build_corpus, "SEEDS", [SEED])
        monkeypatch.setattr(build_corpus, "fetch", fake_fetch({SEED[0]: russian}))
        books, problems = build_corpus.build(LIBRIVOX_OK)
        assert problems == []
        assert books[0]["librivox_same_language"] is False

    def test_a_regional_proof_carries_its_recording_too(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seed = ("B00REGION1", "de", "Grimm", "Kinder", ["region-lock"], "3664")
        home_ok = {**AUDNEX_OK, "title": "Kinder- und Hausmärchen",
                   "authors": [{"name": "Grimm"}], "seriesPrimary": None}
        monkeypatch.setattr(build_corpus, "REGIONAL_SEEDS", [seed])
        monkeypatch.setattr(build_corpus, "fetch", fake_fetch({f"{seed[0]}@de": home_ok}))
        proofs, problems = build_corpus.check_region_lock(LIBRIVOX_OK)
        assert problems == []
        assert proofs[0]["librivox_id"] == "3664"

    def test_a_regional_seed_whose_recording_failed_is_excluded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seed = ("B00REGION1", "de", "Grimm", "Kinder", ["region-lock"], "3664")
        monkeypatch.setattr(build_corpus, "REGIONAL_SEEDS", [seed])
        monkeypatch.setattr(build_corpus, "fetch", fake_fetch({}))
        proofs, problems = build_corpus.check_region_lock({})
        assert proofs == []
        assert any("public-domain status is unproven" in p for p in problems)


@pytest.mark.contract
class TestVerifyLibrivoxRefuses:
    """Same shape as the ASIN check, on the other API. A pin taken on trust proves nothing."""

    def test_a_resolving_project_by_the_expected_author_is_verified(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(build_corpus, "LIBRIVOX_RECORDINGS", ONE_RECORDING)
        verified, problems = build_corpus.verify_librivox()
        assert problems == []
        assert verified["3664"]["authors"] == ["Sir Arthur Conan Doyle"]

    def test_an_absent_project_is_not_verified(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(build_corpus, "LIBRIVOX_RECORDINGS",
                            {"99999999": ("Doyle", "Valley of Fear")})
        verified, problems = build_corpus.verify_librivox()
        assert verified == {}
        assert len(problems) == 1 and "unresolvable" in problems[0]

    def test_a_project_by_a_different_author_is_not_verified(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A mistyped id landing on a real project is the failure a bare id cannot catch."""
        monkeypatch.setattr(build_corpus, "LIBRIVOX_RECORDINGS", {"3664": ("Melville", "Moby")})
        verified, problems = build_corpus.verify_librivox()
        assert verified == {}
        assert "Valley of Fear" in problems[0]

    def test_an_empty_expectation_is_refused_without_fetching(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        asked: list[str] = []

        def recording_fetch(book_id: str) -> tuple[dict | None, str | None]:
            asked.append(book_id)
            return LIBRIVOX_RAW, None

        monkeypatch.setattr(build_corpus, "LIBRIVOX_RECORDINGS", {"3664": ("Doyle", "")})
        monkeypatch.setattr(build_corpus, "fetch_librivox", recording_fetch)
        verified, problems = build_corpus.verify_librivox()
        assert verified == {} and asked == []
        assert "expected title is empty" in problems[0]

    def test_a_200_carrying_no_project_is_not_agreement(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """LibriVox answers some misses with an error body rather than a status."""

        class Response:
            def read(self) -> bytes:
                return json.dumps({"error": "Audiobooks could not be found"}).encode()

            def __enter__(self) -> Response:
                return self

            def __exit__(self, *_: object) -> None:
                return None

        monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: Response())
        data, err = REAL_FETCH_LIBRIVOX("99999999")
        assert data is None and err == "no project in response"


@pytest.mark.contract
class TestARetryDoesNotSoftenTheCheck:
    """LibriVox is a small volunteer site and drops out for seconds at a time.

    Retrying a gateway error is not the same as retrying a refusal. A 404 is the site
    answering, and answering that the project is not there; retrying it would only mean asking
    the same question until the network agreed with us.
    """

    def test_a_404_is_an_answer_and_is_not_retried(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[int] = []

        def raise_404(*_a: object, **_k: object) -> None:
            calls.append(1)
            raise urllib.error.HTTPError(
                url="u", code=404, msg="Not Found", hdrs=email.message.Message(), fp=None
            )

        monkeypatch.setattr(urllib.request, "urlopen", raise_404)
        data, err = REAL_FETCH_LIBRIVOX("99999999")
        assert data is None and err == "HTTP 404"
        assert len(calls) == 1

    def test_a_gateway_failure_is_retried_before_being_believed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[int] = []

        def raise_522(*_a: object, **_k: object) -> None:
            calls.append(1)
            raise urllib.error.HTTPError(
                url="u", code=522, msg="Origin timeout", hdrs=email.message.Message(), fp=None
            )

        monkeypatch.setattr(urllib.request, "urlopen", raise_522)
        data, err = REAL_FETCH_LIBRIVOX("3664", attempts=3)
        assert data is None and err == "HTTP 522"
        assert len(calls) == 3

    def test_a_blip_followed_by_an_answer_is_the_answer(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[int] = []

        class Response:
            def read(self) -> bytes:
                return json.dumps({"books": [LIBRIVOX_RAW]}).encode()

            def __enter__(self) -> Response:
                return self

            def __exit__(self, *_: object) -> None:
                return None

        def flaky(*_a: object, **_k: object) -> Response:
            calls.append(1)
            if len(calls) == 1:
                raise TimeoutError("too slow")
            return Response()

        monkeypatch.setattr(urllib.request, "urlopen", flaky)
        data, err = REAL_FETCH_LIBRIVOX("3664")
        assert err is None and data is not None and data["title"] == "Valley of Fear"


@pytest.mark.contract
class TestTheTwoTablesCannotDrift:
    """A seed and its recording live in different tables, so each must name the other."""

    def test_a_seed_pinning_an_unknown_recording_is_caught(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(build_corpus, "SEEDS", [(*SEED[:4], "404404")])
        monkeypatch.setattr(build_corpus, "REGIONAL_SEEDS", [])
        monkeypatch.setattr(build_corpus, "LIBRIVOX_RECORDINGS", ONE_RECORDING)
        assert any("not in LIBRIVOX_RECORDINGS" in p for p in build_corpus.check_librivox_table())

    def test_a_recording_no_seed_names_is_caught(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A stale row keeps passing its own check long after its book has gone."""
        monkeypatch.setattr(build_corpus, "SEEDS", [SEED])
        monkeypatch.setattr(build_corpus, "REGIONAL_SEEDS", [])
        monkeypatch.setattr(build_corpus, "LIBRIVOX_RECORDINGS",
                            {**ONE_RECORDING, "1": ("Nobody", "Nothing")})
        assert any("no seed names it" in p for p in build_corpus.check_librivox_table())

    def test_the_committed_tables_agree(self) -> None:
        """The shipped tables, not a fixture."""
        assert build_corpus.check_librivox_table() == []

    def test_every_committed_recording_expects_something(self) -> None:
        unusable = [
            build_corpus.check_fragment(f"librivox {book_id}", author, title)
            for book_id, (author, title) in build_corpus.LIBRIVOX_RECORDINGS.items()
        ]
        assert [problem for problem in unusable if problem is not None] == []


@pytest.mark.contract
class TestASpellingCriticalSeedRefusesADriftedCredit:
    """The substring check asks which WORK an ASIN is. For a handful of seeds the exact
    credited spelling is the entire reason they are in the corpus, and a publisher editing
    'J. M. Barrie' to 'J.M. Barrie' would pass the substring check while destroying the only
    punctuation-only drift pair the corpus has.
    """

    def test_the_chosen_spelling_is_accepted(self) -> None:
        assert build_corpus.check_spelling("B078X1NX28", ["J. M. Barrie"]) is None

    def test_a_seed_that_is_not_spelling_critical_is_not_judged(self) -> None:
        assert build_corpus.check_spelling("B071S17YLK", ["Someone Else Entirely"]) is None

    def test_punctuation_drift_is_refused(self) -> None:
        problem = build_corpus.check_spelling("B078X1NX28", ["J.M. Barrie"])
        assert problem is not None
        assert "requires exactly 'J. M. Barrie'" in problem

    def test_the_substring_check_alone_would_have_missed_it(self) -> None:
        """The gap this exists to close, asserted rather than described."""
        drifted = "J.M. Barrie"
        assert "barrie" in drifted.lower()          # the seed's substring check still passes
        assert build_corpus.check_spelling("B078X1NX28", [drifted]) is not None

    def test_the_refusal_says_why_this_seed_cares(self) -> None:
        """So that whoever hits it does not simply relax the assertion."""
        problem = build_corpus.check_spelling("B0C6FJ6L34", ["J. M. Barrie"])
        assert problem is not None
        assert "spelling-critical because" in problem
        assert "Do NOT relax this check" in problem

    def test_one_credit_among_several_is_enough(self) -> None:
        assert build_corpus.check_spelling(
            "B078X1NX28", ["J. M. Barrie", "Someone - translator"]
        ) is None

    def test_a_build_refuses_the_whole_corpus_over_one_drifted_spelling(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seed = ("B0SPELLING", "Barrie", "Peter and Wendy", ["author-punctuation"], "3664")
        monkeypatch.setattr(build_corpus, "SEEDS", [seed])
        monkeypatch.setattr(build_corpus, "SPELLING_CRITICAL",
                            {"B0SPELLING": ("J. M. Barrie", "the reason it was chosen")})
        monkeypatch.setattr(build_corpus, "fetch", fake_fetch({
            "B0SPELLING": {**AUDNEX_OK, "title": "Peter and Wendy",
                           "authors": [{"name": "J.M. Barrie"}]},
        }))
        books, problems = build_corpus.build(LIBRIVOX_OK)
        assert books == []
        assert any("requires exactly 'J. M. Barrie'" in p for p in problems)

    def test_an_entry_naming_no_seed_is_caught(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Otherwise it sits there looking like protection while asserting nothing."""
        monkeypatch.setattr(build_corpus, "SEEDS", [SEED])
        monkeypatch.setattr(build_corpus, "SPELLING_CRITICAL",
                            {"B0GONE": ("Someone", "a seed that was removed")})
        assert any("no seed uses it" in p for p in build_corpus.check_spelling_table())

    def test_the_committed_table_names_only_real_seeds(self) -> None:
        assert build_corpus.check_spelling_table() == []

    def test_every_committed_entry_matches_what_the_corpus_holds(self) -> None:
        """The table records what each seed's credited author IS, so it must agree with the
        corpus that was actually built. Disagreement means one of them was edited alone.
        """
        corpus = json.loads(
            (pathlib.Path(__file__).resolve().parents[1] / "corpus" / "corpus.json")
            .read_text()
        )
        by_asin = {book["asin"]: book for book in corpus["books"]}
        for asin, (spelling, _why) in build_corpus.SPELLING_CRITICAL.items():
            if asin in by_asin:
                assert spelling in by_asin[asin]["authors"], (
                    f"{asin}: SPELLING_CRITICAL expects {spelling!r} but corpus.json holds "
                    f"{by_asin[asin]['authors']!r}"
                )

    def test_every_pinned_spelling_is_the_sole_carrier_of_it(self) -> None:
        """The rule the table follows. A spelling several records carry survives an edit to
        any one of them, so pinning one would assert more than is true.
        """
        corpus = json.loads(
            (pathlib.Path(__file__).resolve().parents[1] / "corpus" / "corpus.json")
            .read_text()
        )
        carriers: dict[str, list[str]] = {}
        for book in corpus["books"]:
            for author in book["authors"]:
                carriers.setdefault(author, []).append(book["asin"])
        for asin, (spelling, _why) in build_corpus.SPELLING_CRITICAL.items():
            if asin in {b["asin"] for b in corpus["books"]}:
                assert carriers.get(spelling) == [asin], (
                    f"{spelling!r} is carried by {carriers.get(spelling)}, not by {asin} alone"
                )
