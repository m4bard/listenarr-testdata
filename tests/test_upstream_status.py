"""The ledger's verdict contract.

`waiting_on` is the only judgement this tool makes, and a status board that cries wolf is one
nobody reads. The first version of the rule said "whoever spoke last is not the one being
waited on", which flagged three threads as ours on its first real run: one where a passing user
said the PR looked useful, and two where the maintainer was addressing the PR's own author.
None were addressed to us. These tests pin the corrected rule so it cannot regress to that.
"""
from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "tools"))

from upstream_status import Thread, render

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
