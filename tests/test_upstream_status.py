"""The ledger's verdict contract.

`waiting_on` is the only judgement this tool makes, and a status board that cries wolf is one
nobody reads. The first version of the rule said "whoever spoke last is not the one being
waited on", which flagged three threads as ours on its first real run: one where a passing user
said the PR looked useful, and two where the maintainer was addressing the PR's own author.
None were addressed to us. These tests pin the corrected rule so it cannot regress to that.
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "tools"))

from upstream_status import (
    GhError,
    Overlap,
    Thread,
    collect,
    find_conflicts,
    last_comment,
    render,
    render_conflicts,
    run_gh,
    search_threads,
)

USER = "m4bard"


def thread(**kwargs: object) -> Thread:
    base: dict[str, object] = {
        "repo": "Listenarrs/Listenarr",
        "number": 1,
        "title": "t",
        "state": "open",
        "is_pr": False,
        "ours": False,
        "updated_at": "2026-08-02T00:00:00Z",
    }
    base.update(kwargs)
    return Thread(**base)  # type: ignore[arg-type]


class TestWaitingOn:
    @pytest.mark.contract
    def test_our_thread_someone_else_spoke_last_is_ours(self) -> None:
        t = thread(ours=True, last_comment_by="maintainer")
        assert t.waiting_on(USER) == "US"

    @pytest.mark.contract
    def test_our_thread_we_spoke_last_is_theirs(self) -> None:
        t = thread(ours=True, last_comment_by=USER)
        assert t.waiting_on(USER) == "them"

    @pytest.mark.contract
    def test_others_thread_not_addressed_to_us_is_not_ours(self) -> None:
        """The exact false positive that motivated the rewrite.

        A bystander commenting on the maintainer's PR, or the maintainer replying to a third
        party's PR, is not a request for anything from us.
        """
        t = thread(ours=False, last_comment_by="bystander", last_mentions_us=False)
        assert t.waiting_on(USER) == "-"

    @pytest.mark.contract
    def test_others_thread_that_mentions_us_is_ours(self) -> None:
        t = thread(ours=False, last_comment_by="maintainer", last_mentions_us=True)
        assert t.waiting_on(USER) == "US"

    @pytest.mark.contract
    def test_closed_thread_is_never_waiting(self) -> None:
        t = thread(ours=True, state="closed", last_comment_by="someone")
        assert t.waiting_on(USER) == "closed"

    def test_our_thread_with_no_replies_waits_on_them(self) -> None:
        t = thread(ours=True, last_comment_by=None)
        assert t.waiting_on(USER) == "them"

    def test_others_thread_with_no_replies_is_not_ours(self) -> None:
        t = thread(ours=False, last_comment_by=None)
        assert t.waiting_on(USER) == "-"


class TestRender:
    def test_banner_counts_only_real_obligations(self) -> None:
        threads = [
            thread(number=1, ours=True, last_comment_by="someone"),
            thread(number=2, ours=False, last_comment_by="bystander"),
            thread(number=3, ours=False, last_comment_by="dev", last_mentions_us=True),
        ]
        out = render(threads, USER)
        assert "** 2 WAITING ON US **" in out

    def test_no_banner_when_nothing_is_owed(self) -> None:
        threads = [thread(number=1, ours=True, last_comment_by=USER)]
        assert "WAITING ON US" not in render(threads, USER)

    def test_closed_threads_are_counted_but_not_listed(self) -> None:
        threads = [
            thread(number=1, ours=True, state="closed", last_comment_by=USER),
            thread(number=2, ours=True, last_comment_by=USER, title="still open"),
        ]
        out = render(threads, USER)
        assert "1 open, 2 total" in out
        assert "still open" in out
        assert "#1" not in out


REPO = "Owner/Repo"


def pr(number: int, login: str, title: str = "t") -> dict[str, object]:
    return {"number": number, "user": {"login": login}, "title": title}


def fake_gh(open_prs: list[dict[str, object]], files: dict[int, list[str]]) -> object:
    """Stand in for run_gh, dispatching on the request path.

    Anything unexpected raises rather than returning a default, so a test cannot pass because
    the fake quietly answered a call the real one would have made differently.
    """

    def _run(args: list[str]) -> object:
        path = args[1]
        if "/pulls?" in path:
            return open_prs
        if "/pulls/" in path and path.endswith("/files?per_page=100"):
            number = int(path.split("/pulls/")[1].split("/")[0])
            return [{"filename": name} for name in files[number]]
        raise AssertionError(f"unexpected gh call: {args}")

    return _run


class TestFindConflicts:
    """The check whose absence let a refactor invalidate one of our PRs unnoticed.

    A silent empty result here is the damaging outcome, because it reads as "nothing overlaps"
    and that is exactly the reassurance it was written to provide.
    """

    @pytest.mark.contract
    def test_a_shared_file_is_reported(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "upstream_status.run_gh",
            fake_gh(
                [pr(1, USER), pr(2, "someone", "their big refactor")],
                {1: ["src/a.cs", "src/b.cs"], 2: ["src/b.cs", "src/c.cs"]},
            ),
        )
        overlaps = find_conflicts(REPO, USER)
        assert len(overlaps) == 1
        assert overlaps[0].ours == 1
        assert overlaps[0].theirs == 2
        assert overlaps[0].shared == {"src/b.cs"}
        assert overlaps[0].theirs_title == "their big refactor"

    @pytest.mark.contract
    def test_disjoint_files_report_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "upstream_status.run_gh",
            fake_gh(
                [pr(1, USER), pr(2, "someone")],
                {1: ["src/a.cs"], 2: ["src/z.cs"]},
            ),
        )
        assert find_conflicts(REPO, USER) == []

    def test_our_own_prs_are_not_compared_against_each_other(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two of our PRs touching one file is our business, not a conflict to report."""
        monkeypatch.setattr(
            "upstream_status.run_gh",
            fake_gh([pr(1, USER), pr(2, USER)], {1: ["src/a.cs"], 2: ["src/a.cs"]}),
        )
        assert find_conflicts(REPO, USER) == []

    def test_no_prs_of_ours_short_circuits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """With nothing of ours open there is nothing to compare, and no file calls to make."""
        monkeypatch.setattr(
            "upstream_status.run_gh", fake_gh([pr(2, "someone"), pr(3, "another")], {})
        )
        assert find_conflicts(REPO, USER) == []

    @pytest.mark.contract
    def test_every_pairing_is_reported_not_just_the_first(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two of ours overlapping two of theirs is four findings, and missing any is a miss."""
        monkeypatch.setattr(
            "upstream_status.run_gh",
            fake_gh(
                [pr(1, USER), pr(2, USER), pr(10, "x"), pr(11, "y")],
                {
                    1: ["shared.cs"], 2: ["shared.cs"],
                    10: ["shared.cs"], 11: ["shared.cs"],
                },
            ),
        )
        overlaps = find_conflicts(REPO, USER)
        assert {(o.ours, o.theirs) for o in overlaps} == {(1, 10), (2, 10), (1, 11), (2, 11)}

    @pytest.mark.contract
    def test_a_malformed_pulls_response_raises_rather_than_reporting_calm(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("upstream_status.run_gh", lambda _args: {"message": "Not Found"})
        with pytest.raises(GhError):
            find_conflicts(REPO, USER)

    @pytest.mark.contract
    def test_a_malformed_files_response_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _run(args: list[str]) -> object:
            return [pr(1, USER)] if "/pulls?" in args[1] else {"message": "nope"}

        monkeypatch.setattr("upstream_status.run_gh", _run)
        with pytest.raises(GhError):
            find_conflicts(REPO, USER)


class TestRenderConflicts:
    def test_no_overlap_says_so_explicitly(self) -> None:
        out = render_conflicts([], REPO)
        assert "No file overlap" in out and REPO in out

    def test_each_shared_file_is_listed(self) -> None:
        out = render_conflicts(
            [Overlap(ours=1, theirs=2, theirs_title="theirs", shared={"a.cs", "b.cs"})], REPO
        )
        assert "our #1" in out and "#2" in out
        assert "a.cs" in out and "b.cs" in out


class TestRunGhFailsClosed:
    """A ledger that reports "nothing open" because a token expired is the worst outcome."""

    @pytest.mark.contract
    def test_a_nonzero_exit_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "upstream_status.subprocess.run",
            lambda *a, **k: subprocess.CompletedProcess(a, 1, "", "gh: not authenticated"),
        )
        with pytest.raises(GhError, match="not authenticated"):
            run_gh(["api", "whatever"])

    @pytest.mark.contract
    def test_empty_output_raises_rather_than_becoming_an_empty_list(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "upstream_status.subprocess.run",
            lambda *a, **k: subprocess.CompletedProcess(a, 0, "   ", ""),
        )
        with pytest.raises(GhError, match="returned nothing"):
            run_gh(["api", "whatever"])

    @pytest.mark.contract
    def test_unparseable_output_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "upstream_status.subprocess.run",
            lambda *a, **k: subprocess.CompletedProcess(a, 0, "<html>rate limited</html>", ""),
        )
        with pytest.raises(GhError, match="non-JSON"):
            run_gh(["api", "whatever"])

    def test_valid_json_comes_back_parsed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "upstream_status.subprocess.run",
            lambda *a, **k: subprocess.CompletedProcess(a, 0, '{"ok": true}', ""),
        )
        assert run_gh(["api", "whatever"]) == {"ok": True}


class TestLastComment:
    def test_an_empty_thread_reports_nobody(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("upstream_status.run_gh", lambda _args: [])
        assert last_comment(REPO, 1, USER) == (None, None, False, 0)

    def test_it_reports_the_last_commenter_not_the_first(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "upstream_status.run_gh",
            lambda _args: [
                {"user": {"login": USER}, "created_at": "2026-01-01T00:00:00Z", "body": "first"},
                {"user": {"login": "other"}, "created_at": "2026-01-02T00:00:00Z", "body": "last"},
            ],
        )
        who, when, mentions, count = last_comment(REPO, 1, USER)
        assert (who, when, mentions, count) == ("other", "2026-01-02T00:00:00Z", False, 2)

    @pytest.mark.contract
    def test_a_mention_is_detected_case_insensitively(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The mention is what promotes somebody else's thread to being our turn."""
        monkeypatch.setattr(
            "upstream_status.run_gh",
            lambda _args: [
                {"user": {"login": "other"}, "created_at": "t", "body": f"thanks @{USER.upper()}"}
            ],
        )
        assert last_comment(REPO, 1, USER)[2] is True

    def test_a_missing_body_is_not_a_mention(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "upstream_status.run_gh",
            lambda _args: [{"user": {"login": "other"}, "created_at": "t", "body": None}],
        )
        assert last_comment(REPO, 1, USER)[2] is False


class TestCollect:
    def test_a_thread_we_authored_and_commented_on_is_counted_once_and_marked_ours(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The dedup that stops one thread appearing twice, and losing its authored flag."""
        item = {"number": 5, "title": "t", "state": "closed", "updated_at": "2026-01-01T00:00:00Z"}
        monkeypatch.setattr("upstream_status.search_threads", lambda _r, _u, _q: [item])
        threads = collect((REPO,), USER)
        assert len(threads) == 1
        assert threads[0].ours is True

    def test_pull_requests_are_distinguished_from_issues(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        item = {"number": 5, "title": "t", "state": "closed", "updated_at": "2026-01-01T00:00:00Z",
                "pull_request": {"url": "..."}}
        monkeypatch.setattr("upstream_status.search_threads", lambda _r, _u, _q: [item])
        assert collect((REPO,), USER)[0].is_pr is True

    def test_open_threads_are_enriched_with_comment_state(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        item = {"number": 5, "title": "t", "state": "open", "updated_at": "2026-01-01T00:00:00Z"}
        monkeypatch.setattr("upstream_status.search_threads", lambda _r, _u, _q: [item])
        monkeypatch.setattr(
            "upstream_status.last_comment", lambda _r, _n, _u: ("other", "t", True, 3)
        )
        thread = collect((REPO,), USER)[0]
        assert thread.last_comment_by == "other" and thread.last_mentions_us is True

    def test_newest_activity_comes_first(self, monkeypatch: pytest.MonkeyPatch) -> None:
        items = [
            {"number": 1, "title": "old", "state": "closed", "updated_at": "2026-01-01T00:00:00Z"},
            {"number": 2, "title": "new", "state": "closed", "updated_at": "2026-06-01T00:00:00Z"},
        ]
        monkeypatch.setattr("upstream_status.search_threads", lambda _r, _u, _q: items)
        assert [t.number for t in collect((REPO,), USER)] == [2, 1]


class TestSearchThreads:
    def test_items_are_returned(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("upstream_status.run_gh", lambda _args: {"items": [{"number": 1}]})
        assert search_threads(REPO, USER, "author") == [{"number": 1}]

    @pytest.mark.contract
    def test_a_response_without_items_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """GitHub answers a rate limit with a body that has no items. Zero results and
        "we were not allowed to look" must not collapse into the same answer."""
        monkeypatch.setattr(
            "upstream_status.run_gh", lambda _args: {"message": "API rate limit exceeded"}
        )
        with pytest.raises(GhError, match="no items field"):
            search_threads(REPO, USER, "author")

    def test_the_qualifier_reaches_the_query(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: list[str] = []

        def _run(args: list[str]) -> object:
            seen.append(args[1])
            return {"items": []}

        monkeypatch.setattr("upstream_status.run_gh", _run)
        search_threads(REPO, USER, "commenter")
        assert f"commenter:{USER}" in seen[0] and f"repo:{REPO}" in seen[0]


@pytest.mark.contract
def test_a_thread_in_both_searches_stays_marked_ours(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A thread we authored and also commented on comes back from both searches.

    Losing the authored mark on the second sighting would drop it out of the "waiting on us"
    rule, which only applies the strong test to threads that are ours. The merge is an OR
    rather than an assignment so this holds whichever search runs first.
    """
    item = {"number": 5, "title": "t", "state": "closed", "updated_at": "2026-01-01T00:00:00Z"}
    monkeypatch.setattr("upstream_status.search_threads", lambda _r, _u, _q: [item])
    threads = collect((REPO,), USER)
    assert len(threads) == 1 and threads[0].ours is True
