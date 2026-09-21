"""Contract tests for the release-selection probe's verdict.

The probe exists to say two things: the findings reproduce, and the controls discriminate. The
ways it could say that wrongly all matter more than the arithmetic:

* false PASS via an inert control  - the apparatus never reached the scorer, every gate came back
                                     quiet, and quiet reads exactly like "the gate did not fire",
                                     which is the finding itself
* false PASS via a dropped row     - the scorer returned fewer rows than were posted and the
                                     missing one was never compared
* false verdict via row order      - the endpoint sorts its answer, so comparing by position
                                     compares a torrent against an NZB the moment a score moves
* false PASS after a fix lands     - a finding stops reproducing and the run still exits zero, so
                                     the regression check never fires

The exit code is the assertion in each case, not the printed table, because the table is what a
human reads and the exit code is what CI reads.
"""

from __future__ import annotations

import pathlib
import sys
from typing import Any

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

from profile_gate_probe import (
    CONTROL,
    EXIT_CONTROL_INERT,
    EXIT_FINDING_GONE,
    EXIT_OK,
    FINDING,
    ApparatusError,
    Check,
    create_profile,
    profile,
    release,
    report,
    score,
)


def held(key: str, kind: str) -> Check:
    return Check(key, kind, "claim", True, "note")


def broken(key: str, kind: str) -> Check:
    return Check(key, kind, "claim", False, "note")


class TestVerdict:
    """Sub-contract 1 and 3: fails when it should, and never launders a void run into a pass."""

    @pytest.mark.contract
    def test_everything_holding_exits_zero(self) -> None:
        checks = [held("f", FINDING), held("c", CONTROL)]
        assert report(checks) == EXIT_OK

    @pytest.mark.contract
    def test_a_finding_that_stopped_reproducing_exits_non_zero(self) -> None:
        checks = [held("f1", FINDING), broken("f2", FINDING), held("c", CONTROL)]
        assert report(checks) == EXIT_FINDING_GONE

    @pytest.mark.contract
    def test_an_inert_control_voids_the_run(self) -> None:
        checks = [held("f", FINDING), broken("c", CONTROL)]
        assert report(checks) == EXIT_CONTROL_INERT

    @pytest.mark.contract
    def test_an_inert_control_outranks_a_missing_finding(self) -> None:
        """A finding cannot be reported as gone on the strength of a run that measured nothing."""
        checks = [broken("f", FINDING), broken("c", CONTROL)]
        assert report(checks) == EXIT_CONTROL_INERT


class TestRowMatching:
    """Sub-contract 3: a scorer that drops or reorders rows must not pass silently."""

    def posted(self) -> list[dict[str, object]]:
        return [
            release("t", "MP3 320kbps", "torrent", 300),
            release("n", "MP3 64kbps", "usenet", 300),
        ]

    def scored(self, *entries: tuple[str, str, str, int, bool]) -> list[dict[str, Any]]:
        return [
            {
                "searchResult": {
                    "id": rid,
                    "quality": quality,
                    "downloadType": protocol,
                    "size": 300 * 1024 * 1024,
                },
                "totalScore": total,
                "isRejected": rejected,
                "rejectionReasons": [],
            }
            for rid, quality, protocol, total, rejected in entries
        ]

    @pytest.mark.contract
    def test_rows_come_back_in_the_order_they_were_posted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The endpoint sorts its answer. Matching by position would compare across protocols."""
        answer = self.scored(
            ("n", "MP3 64kbps", "usenet", 100, False),
            ("t", "MP3 320kbps", "torrent", 80, False),
        )
        monkeypatch.setattr(
            "profile_gate_probe.api", lambda *args, **kwargs: answer, raising=True
        )
        rows = score("irrelevant", 1, self.posted())
        assert [row.rid for row in rows] == ["t", "n"]
        assert [row.protocol for row in rows] == ["torrent", "usenet"]
        assert [row.score for row in rows] == [80, 100]

    @pytest.mark.contract
    def test_a_dropped_row_raises_rather_than_shrinking_the_comparison(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        answer = self.scored(("t", "MP3 320kbps", "torrent", 80, False))
        monkeypatch.setattr(
            "profile_gate_probe.api", lambda *args, **kwargs: answer, raising=True
        )
        with pytest.raises(ApparatusError, match="no row for release 'n'"):
            score("irrelevant", 1, self.posted())

    @pytest.mark.contract
    def test_a_profile_that_was_not_created_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "profile_gate_probe.api", lambda *args, **kwargs: {"error": "nope"}, raising=True
        )
        with pytest.raises(ApparatusError, match="returned no id"):
            create_profile("irrelevant", profile("p", [("MP3 320kbps", True)]))


class TestProfileShape:
    """Sub-contract 2: the profiles have to be the ones the measurement claims to use."""

    def test_ordering_is_the_order_the_rungs_were_given(self) -> None:
        payload = profile("p", [("MP3 128kbps", True), ("MP3 320kbps", True)])
        rungs = payload["qualities"]
        assert isinstance(rungs, list)
        assert [(rung["quality"], rung["priority"]) for rung in rungs] == [
            ("MP3 128kbps", 0),
            ("MP3 320kbps", 1),
        ]

    def test_profiles_are_never_default(self) -> None:
        """A default profile has eleven required qualities injected, destroying the ordering."""
        assert profile("p", [("MP3 320kbps", True)])["isDefault"] is False

    def test_the_cutoff_is_a_quality_the_profile_allows(self) -> None:
        payload = profile("p", [("MP3 320kbps", False), ("MP3 128kbps", True)])
        assert payload["cutoffQuality"] == "MP3 128kbps"

    def test_nothing_else_can_separate_the_two_protocols(self) -> None:
        """Languages, formats, seeders and age all score a torrent and an NZB differently."""
        payload = profile("p", [("MP3 320kbps", True)])
        assert payload["preferredLanguages"] == []
        assert payload["preferredFormats"] == []
        assert payload["minimumSeeders"] == 0
        assert payload["maximumAge"] == 0
        assert release("r", "MP3 320kbps", "usenet", 300)["publishedDate"] == ""
