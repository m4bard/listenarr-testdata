"""Contract tests for the genre-targeted metadata refresh tool.

Every test here pairs a claim with a control that has to come out differently, because most of
the ways this tool could be wrong look exactly like it working:

* a filter that selects everything  - picking the right author proves nothing unless a wrong one
                                      is also on offer and is left behind
* a stampede that looks like a wait  - re-POSTing into an in-flight 409 eventually succeeds, so
                                      the run still finishes and only the call log shows it
                                      spent its time colliding
* a 429 read as a failure, or as a  - all three answers to a POST end with the tool moving on,
  success                             so the outcome alone cannot tell them apart
* a list-only mode that refreshes    - the printed text says "would refresh" either way. Only the
                                      calls that left the process settle it.

So the assertions are on the recorded calls and the exit codes, not on the output.
"""

from __future__ import annotations

import json
import pathlib
import sys
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

from genre_refresh import (
    COMPLETED,
    EXIT_NO_MATCH,
    EXIT_OK,
    EXIT_REFRESH_INCOMPLETE,
    FORGOTTEN,
    GAVE_UP,
    MATCH_EXACT,
    MATCH_PHRASE,
    ApiError,
    ListenarrApi,
    RefreshDisabled,
    Response,
    RunOutcome,
    Sequencer,
    StateFile,
    count_series_asins,
    genre_matches,
    parse_args,
    port_of,
    run,
    select_authors,
    split_genre_field,
    target_of,
)

MILITARY_SF = "military science fiction"
CYBERPUNK = "cyberpunk"


def book(
    book_id: int,
    authors: list[str],
    genres: list[str],
    series_asin: str | None = None,
) -> dict[str, Any]:
    """One library list item, in the shape the list endpoint returns."""
    memberships = [{"seriesName": "A Series", "seriesAsin": series_asin}] if series_asin else []
    return {"id": book_id, "title": f"Book {book_id}", "authors": authors, "genres": genres,
            "seriesMemberships": memberships}


class RecordingTransport:
    """A stand-in for the API that records every call and answers from a script.

    Nothing here reaches a network. The recorded call list is what the sequencing and list-only
    tests assert on, because the printed output cannot distinguish a POST that happened from one
    that did not.
    """

    def __init__(self, library: list[dict[str, Any]] | None = None) -> None:
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []
        self.library: list[dict[str, Any]] = library or []
        self.monitored: dict[str, int] = {}
        # None means "answer regardless of the language asked for", the old behaviour.
        self.monitored_language: str | None = None
        self.start_responses: list[Response] = []
        self.status_responses: dict[str, list[Response]] = {}

    def __call__(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> Response:
        bare = path.split("?")[0]
        self.calls.append((method, bare, body))

        if bare == "/library" and method == "GET":
            return Response(200, {}, list(self.library))

        if bare == "/authors/monitoring/status":
            # Decoded the way a server decodes it, with strict parsing, so a client that stopped
            # encoding its query would break here instead of being quietly understood.
            query = parse_qs(urlsplit(path).query, strict_parsing=True)
            name = query["name"][0]
            author_id = self.monitored.get(name)
            # A real MonitoredAuthors row stores one language and the endpoint matches it as a
            # literal, so asking for a language no row holds answers 200 with isMonitored false.
            # This stub ignored the parameter entirely, which is why the whole suite passed while
            # the tool could not find a single monitored author on a real install. Opt-in, so the
            # tests that do not care about language are unaffected.
            if (
                self.monitored_language is not None
                and query.get("language", [None])[0] != self.monitored_language
            ):
                author_id = None
            if author_id is None:
                return Response(200, {}, {"isMonitored": False, "monitoredAuthor": None})
            return Response(
                200, {}, {"isMonitored": True, "monitoredAuthor": {"id": author_id,
                                                                   "authorName": name}}
            )

        if bare == "/library/refresh-metadata" and method == "POST":
            if not self.start_responses:
                raise AssertionError("an unscripted POST to the refresh endpoint")
            return self.start_responses.pop(0)

        if bare.startswith("/library/refresh-metadata/") and method == "GET":
            run_id = bare.rsplit("/", 1)[1]
            queue = self.status_responses.get(run_id)
            if not queue:
                raise AssertionError(f"an unscripted status poll for run {run_id}")
            return queue.pop(0) if len(queue) > 1 else queue[0]

        if method == "GET" and bare.split("/")[1:2] == ["configuration"]:
            # Answered rather than raised, and ONLY for a read of exactly that path, so the test
            # asserting this tool never touches a setting has something it could catch while the
            # catch-all below still refuses everything else. A write to it still raises.
            return Response(200, {}, {})

        raise AssertionError(f"an unexpected call: {method} {path}")

    def posts(self) -> list[tuple[str, str, dict[str, Any] | None]]:
        """Every call that was not a read."""
        return [call for call in self.calls if call[0] != "GET"]


def accepted(run_id: str, total: int = 4) -> Response:
    return Response(202, {}, {"runId": run_id, "scope": "Author", "totalBooks": total,
                              "status": "Running"})


def in_flight(run_id: str) -> Response:
    """The 409 a collision gives: the same body a success gives, naming the run that holds it."""
    return Response(409, {}, {"runId": run_id, "scope": "Library", "totalBooks": 900,
                              "status": "Running"})


def rate_limited(seconds: int = 15) -> Response:
    return Response(
        429,
        {"retry-after": str(seconds)},
        {"message": f"Metadata refresh cooldown active. Please wait {seconds} seconds "
                    "before starting another refresh.", "retryAfterSeconds": seconds},
    )


def disabled() -> Response:
    return Response(409, {}, {"message": "Metadata refresh is turned off in settings.",
                              "code": "metadata_refresh_disabled"})


def finished(run_id: str, status: str = COMPLETED, **counters: int) -> Response:
    body: dict[str, Any] = {"runId": run_id, "scope": "Author", "status": status,
                            "totalBooks": 4, "processed": 4, "updated": 3, "skipped": 1,
                            "deferred": 0, "failed": 0, "requestsSpent": 7}
    body.update(counters)
    return Response(200, {}, body)


def running(run_id: str) -> Response:
    return Response(200, {}, {"runId": run_id, "scope": "Library", "status": "Running",
                              "totalBooks": 900, "processed": 12, "updated": 9, "skipped": 3,
                              "deferred": 0, "failed": 0, "requestsSpent": 12})


class Clock:
    """A monotonic clock that only advances when something sleeps."""

    def __init__(self) -> None:
        self.slept: list[float] = []
        self.now = 0.0

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds

    def __call__(self) -> float:
        return self.now


def quiet(_: str) -> None:
    """Swallow the tool's progress output; these tests assert on calls, not text."""


class TestGenreSelection:
    """The filter picks what it should AND leaves behind what it should not."""

    def test_matching_author_selected_and_non_matching_author_not(self) -> None:
        library = [
            book(1, ["Ada Wren"], ["Military Science Fiction"]),
            book(2, ["Ada Wren"], ["Science Fiction"]),
            book(3, ["Bo Kell"], ["Cozy Mystery", "Romance"]),
            book(4, ["Cy Nolan"], ["Cyberpunk"]),
        ]

        selected = select_authors(library, [MILITARY_SF, CYBERPUNK], MATCH_PHRASE)
        names = [author.name for author in selected]

        assert "Ada Wren" in names
        assert "Cy Nolan" in names
        # The control. Bo Kell is in the same library, has books, and carries genres; the only
        # difference is which genres. If the filter were inert every author would come back.
        assert "Bo Kell" not in names

    def test_priority_order_follows_the_order_the_genres_were_given(self) -> None:
        library = [
            book(1, ["Cy Nolan"], ["Cyberpunk"]),
            book(2, ["Ada Wren"], ["Military Science Fiction"]),
        ]

        first = [a.name for a in select_authors(library, [MILITARY_SF, CYBERPUNK], MATCH_PHRASE)]
        reversed_request = [
            a.name for a in select_authors(library, [CYBERPUNK, MILITARY_SF], MATCH_PHRASE)
        ]

        assert first == ["Ada Wren", "Cy Nolan"]
        # The control on the ordering: the same library, the same authors, the other request
        # order. If priority were ignored these two lists would be identical.
        assert reversed_request == ["Cy Nolan", "Ada Wren"]

    def test_a_book_matches_only_the_author_it_names(self) -> None:
        library = [book(1, ["Ada Wren", "Cy Nolan"], ["Cyberpunk"]),
                   book(2, ["Bo Kell"], ["Cyberpunk"])]

        selected = {a.name: a for a in select_authors(library, [CYBERPUNK], MATCH_PHRASE)}

        assert sorted(selected) == ["Ada Wren", "Bo Kell", "Cy Nolan"]
        assert selected["Ada Wren"].matching_book_ids == [1]
        assert selected["Bo Kell"].matching_book_ids == [2]


class TestGenreMatching:
    """What the chosen matching rule does, and what it is documented to miss."""

    def test_phrase_matching_reaches_inside_a_compound_genre(self) -> None:
        assert genre_matches(["science fiction"], ["Military Science Fiction"], MATCH_PHRASE)
        assert genre_matches(["science fiction"], ["Science Fiction & Fantasy"], MATCH_PHRASE)

    def test_exact_matching_does_not(self) -> None:
        # The control for the mode switch: the same input under the stricter rule has to come out
        # differently, or --match is not reaching the comparison at all.
        assert not genre_matches(["science fiction"], ["Military Science Fiction"], MATCH_EXACT)
        assert genre_matches(["science fiction"], ["Science Fiction"], MATCH_EXACT)

    def test_case_and_punctuation_are_not_the_difference(self) -> None:
        assert genre_matches([CYBERPUNK], ["CYBERPUNK"], MATCH_EXACT)
        assert genre_matches(["science fiction"], ["science-fiction"], MATCH_EXACT)

    def test_the_documented_misses(self) -> None:
        # Abbreviations are not synonyms of the spelled-out genre.
        assert not genre_matches(["sci-fi"], ["Science Fiction"], MATCH_PHRASE)
        # A word split by a hyphen is not the same word joined.
        assert not genre_matches(["cyber-punk"], ["Cyberpunk"], MATCH_PHRASE)
        # A request narrower than the tag does not reach it.
        assert not genre_matches([MILITARY_SF], ["Science Fiction"], MATCH_PHRASE)
        # And the over-match that buys the compound handling.
        assert genre_matches(["fiction"], ["Historical Fiction"], MATCH_PHRASE)

    def test_compound_strings_split_into_their_genres(self) -> None:
        assert split_genre_field("Science Fiction & Fantasy") == ["science fiction", "fantasy"]
        assert split_genre_field("Sci Fi, Cyberpunk; Thriller") == ["sci fi", "cyberpunk",
                                                                     "thriller"]


class TestSeriesAsinCount:
    """The before-and-after number has to count books, not memberships."""

    def test_counts_books_carrying_a_populated_series_asin(self) -> None:
        library = [
            book(1, ["Ada Wren"], [], series_asin="B000000001"),
            book(2, ["Ada Wren"], [], series_asin=None),
            book(3, ["Ada Wren"], [], series_asin="   "),
        ]

        assert count_series_asins(library, [1, 2, 3]) == 1
        # The control: a book outside the id set must not be counted, or the number is the
        # library's rather than the author's.
        assert count_series_asins(library, [2, 3]) == 0


class TestSequencing:
    """One run at a time, waiting on the server's own number rather than a shorter one."""

    def build(self, transport: RecordingTransport, clock: Clock) -> Sequencer:
        return Sequencer(
            ListenarrApi(transport),
            poll_interval=5.0,
            run_timeout=600.0,
            max_attempts=5,
            sleep=clock.sleep,
            now=clock,
            log=quiet,
        )

    def test_it_waits_on_the_run_already_in_flight_instead_of_stampeding(self) -> None:
        transport = RecordingTransport()
        clock = Clock()
        transport.start_responses = [in_flight("held"), accepted("mine")]
        transport.status_responses = {
            "held": [running("held"), finished("held")],
            "mine": [finished("mine")],
        }

        outcome = self.build(transport, clock).refresh(author_id=7, force=True)

        assert outcome.status == COMPLETED
        # Two POSTs only: the refused one and the one that got in. A stampede would show as a
        # run of POSTs with no polls between them.
        assert len(transport.posts()) == 2
        polled_before_second_post = [
            call for call in transport.calls[: _index_of_second_post(transport)]
            if call[1] == "/library/refresh-metadata/held"
        ]
        assert len(polled_before_second_post) == 2
        assert clock.slept  # it waited rather than spinning

    def test_the_control_no_collision_means_no_waiting_and_one_post(self) -> None:
        # The control that must come out differently. Same code path, nothing holding the gate.
        # If this also showed two POSTs and a wait, the test above would prove nothing about
        # collisions.
        transport = RecordingTransport()
        clock = Clock()
        transport.start_responses = [accepted("mine")]
        transport.status_responses = {"mine": [finished("mine")]}

        outcome = self.build(transport, clock).refresh(author_id=7, force=True)

        assert outcome.status == COMPLETED
        assert len(transport.posts()) == 1
        assert clock.slept == []

    def test_a_429_waits_the_servers_number_and_is_not_a_collision(self) -> None:
        transport = RecordingTransport()
        clock = Clock()
        transport.start_responses = [rate_limited(15), accepted("mine")]
        transport.status_responses = {"mine": [finished("mine")]}

        outcome = self.build(transport, clock).refresh(author_id=7, force=True)

        assert outcome.status == COMPLETED
        assert clock.slept == [15.0]
        # And it is distinguishable from the 409: a rate-limited attempt polls no run, because
        # there is no run to poll. A collision would have polled one before retrying.
        assert [call for call in transport.calls if call[1].startswith(
            "/library/refresh-metadata/")] == [("GET", "/library/refresh-metadata/mine", None)]

    def test_the_disabled_409_is_fatal_and_not_retried(self) -> None:
        transport = RecordingTransport()
        clock = Clock()
        transport.start_responses = [disabled()]

        with pytest.raises(RefreshDisabled):
            self.build(transport, clock).refresh(author_id=7, force=True)

        # The control against the in-flight 409, which is the same status code: that one is
        # waited out and retried, this one stops immediately.
        assert len(transport.posts()) == 1
        assert clock.slept == []

    def test_an_unknown_author_id_is_reported_rather_than_retried(self) -> None:
        transport = RecordingTransport()
        clock = Clock()
        transport.start_responses = [
            Response(404, {}, {"message": "No monitored author with that id.",
                               "code": "monitored_author_not_found"})
        ]

        outcome = self.build(transport, clock).refresh(author_id=7, force=True)

        assert outcome.status == "UnknownAuthor"
        assert not outcome.finished
        assert len(transport.posts()) == 1

    def test_it_gives_up_rather_than_retrying_for_ever(self) -> None:
        transport = RecordingTransport()
        clock = Clock()
        transport.start_responses = [rate_limited(2)] * 5

        outcome = Sequencer(
            ListenarrApi(transport), poll_interval=5.0, run_timeout=600.0, max_attempts=5,
            sleep=clock.sleep, now=clock, log=quiet,
        ).refresh(author_id=7, force=True)

        assert outcome.status == GAVE_UP
        assert len(transport.posts()) == 5

    def test_a_run_that_outlasts_its_timeout_is_not_reported_as_finished(self) -> None:
        transport = RecordingTransport()
        clock = Clock()
        transport.start_responses = [accepted("mine")]
        transport.status_responses = {"mine": [running("mine")]}

        outcome = Sequencer(
            ListenarrApi(transport), poll_interval=5.0, run_timeout=20.0, max_attempts=5,
            sleep=clock.sleep, now=clock, log=quiet,
        ).refresh(author_id=7, force=True)

        assert outcome.status == GAVE_UP
        assert not outcome.finished

    def test_an_accepted_run_without_a_run_id_is_an_apparatus_failure(self) -> None:
        transport = RecordingTransport()
        clock = Clock()
        transport.start_responses = [Response(202, {}, {"scope": "Author", "status": "Running"})]

        with pytest.raises(ApiError):
            self.build(transport, clock).refresh(author_id=7, force=True)


def _index_of_second_post(transport: RecordingTransport) -> int:
    seen = 0
    for index, call in enumerate(transport.calls):
        if call[0] == "POST":
            seen += 1
            if seen == 2:
                return index
    raise AssertionError("there was no second POST")


class TestListOnlyMode:
    """The default does nothing, proven by the calls rather than by the wording."""

    def test_list_only_issues_no_post_at_all(self) -> None:
        transport = RecordingTransport([book(1, ["Ada Wren"], ["Cyberpunk"])])

        code = run(
            ListenarrApi(transport), wanted=[CYBERPUNK], mode=MATCH_PHRASE, do_refresh=False,
            force=True, region="us", language="all", state=None, sequencer=None, log=quiet,
        )

        assert code == EXIT_OK
        assert transport.posts() == []
        assert transport.calls == [("GET", "/library", None)]

    def test_the_control_refreshing_does_post(self, tmp_path: pathlib.Path) -> None:
        # The control that must come out differently: same library, same genres, --refresh on.
        # Without it, a tool that could never POST would pass the test above.
        transport = RecordingTransport([book(1, ["Ada Wren"], ["Cyberpunk"])])
        transport.monitored = {"Ada Wren": 42}
        transport.start_responses = [accepted("mine")]
        transport.status_responses = {"mine": [finished("mine")]}
        clock = Clock()

        code = run(
            ListenarrApi(transport), wanted=[CYBERPUNK], mode=MATCH_PHRASE, do_refresh=True,
            force=True, region="us", language="all",
            state=StateFile(tmp_path / "state.json", "host:18901"),
            sequencer=Sequencer(ListenarrApi(transport), 5.0, 600.0, 5, clock.sleep, clock, quiet),
            log=quiet,
        )

        assert code == EXIT_OK
        assert transport.posts() == [
            ("POST", "/library/refresh-metadata", {"authorId": 42, "force": True})
        ]

    def test_no_author_matching_is_its_own_exit_code(self) -> None:
        transport = RecordingTransport([book(1, ["Bo Kell"], ["Romance"])])

        code = run(
            ListenarrApi(transport), wanted=[CYBERPUNK], mode=MATCH_PHRASE, do_refresh=False,
            force=True, region="us", language="all", state=None, sequencer=None, log=quiet,
        )

        assert code == EXIT_NO_MATCH
        assert transport.posts() == []

    def test_nothing_it_sends_could_change_a_server_setting(self, tmp_path: pathlib.Path) -> None:
        # The request budget belongs to the server. The guarantee is structural: the only call
        # this tool makes that is not a read is the refresh trigger itself.
        transport = RecordingTransport([book(1, ["Ada Wren"], ["Cyberpunk"])])
        transport.monitored = {"Ada Wren": 42}
        transport.start_responses = [accepted("mine")]
        transport.status_responses = {"mine": [finished("mine")]}
        clock = Clock()

        run(
            ListenarrApi(transport), wanted=[CYBERPUNK], mode=MATCH_PHRASE, do_refresh=True,
            force=True, region="us", language="all",
            state=StateFile(tmp_path / "state.json", "host:18901"),
            sequencer=Sequencer(ListenarrApi(transport), 5.0, 600.0, 5, clock.sleep, clock, quiet),
            log=quiet,
        )

        assert {call[1] for call in transport.posts()} == {"/library/refresh-metadata"}
        assert not [call for call in transport.calls if "configuration" in call[1]]


class TestResumability:
    """A second run must not redo an author the first one finished."""

    def library(self) -> list[dict[str, Any]]:
        return [book(1, ["Ada Wren"], ["Cyberpunk"]), book(2, ["Cy Nolan"], ["Cyberpunk"])]

    def go(self, transport: RecordingTransport, state_path: pathlib.Path) -> int:
        clock = Clock()
        return run(
            ListenarrApi(transport), wanted=[CYBERPUNK], mode=MATCH_PHRASE, do_refresh=True,
            force=True, region="us", language="all",
            state=StateFile(state_path, "host:18901"),
            sequencer=Sequencer(ListenarrApi(transport), 5.0, 600.0, 5, clock.sleep, clock, quiet),
            log=quiet,
        )

    def test_a_completed_author_is_skipped_the_second_time(
        self, tmp_path: pathlib.Path
    ) -> None:
        state_path = tmp_path / "state.json"
        first = RecordingTransport(self.library())
        first.monitored = {"Ada Wren": 42, "Cy Nolan": 43}
        first.start_responses = [accepted("r1"), accepted("r2")]
        first.status_responses = {"r1": [finished("r1")], "r2": [finished("r2")]}

        assert self.go(first, state_path) == EXIT_OK
        assert len(first.posts()) == 2

        second = RecordingTransport(self.library())
        second.monitored = {"Ada Wren": 42, "Cy Nolan": 43}
        second.start_responses = []  # any POST at all raises

        assert self.go(second, state_path) == EXIT_OK
        assert second.posts() == []

    def test_a_truncated_run_leaves_the_author_pending(self, tmp_path: pathlib.Path) -> None:
        # The control against the test above. Truncated is a run that ended normally, but the
        # books it never reached keep their unset timestamps, so the author is not done. If it
        # were recorded like Completed, the second pass would skip an author still missing data.
        state_path = tmp_path / "state.json"
        first = RecordingTransport(self.library())
        first.monitored = {"Ada Wren": 42, "Cy Nolan": 43}
        first.start_responses = [accepted("r1"), accepted("r2")]
        first.status_responses = {
            "r1": [finished("r1", status="Truncated")],
            "r2": [finished("r2")],
        }

        assert self.go(first, state_path) == EXIT_REFRESH_INCOMPLETE

        second = RecordingTransport(self.library())
        second.monitored = {"Ada Wren": 42, "Cy Nolan": 43}
        second.start_responses = [accepted("r3")]
        second.status_responses = {"r3": [finished("r3")]}

        assert self.go(second, state_path) == EXIT_OK
        assert second.posts() == [
            ("POST", "/library/refresh-metadata", {"authorId": 42, "force": True})
        ]

    def test_state_is_written_after_each_author_not_at_the_end(
        self, tmp_path: pathlib.Path
    ) -> None:
        state_path = tmp_path / "state.json"
        transport = RecordingTransport(self.library())
        transport.monitored = {"Ada Wren": 42, "Cy Nolan": 43}
        transport.start_responses = [accepted("r1")]
        transport.status_responses = {"r1": [finished("r1")]}

        # The second author has no scripted POST, so the transport raises partway through, the
        # way an interrupt would. The first author's record must already be on disk.
        with pytest.raises(AssertionError):
            self.go(transport, state_path)

        written = json.loads(state_path.read_text())
        assert list(written["targets"]["host:18901"]["completed"]) == ["42"]

    def test_the_state_file_is_keyed_by_target(self, tmp_path: pathlib.Path) -> None:
        state_path = tmp_path / "state.json"
        StateFile(state_path, "host-a:18901").record(
            42, "Ada Wren", _outcome_completed()
        )
        # The control: the same file, a different instance. One file may serve more than one
        # target, and a completed author on one must not silently count as done on the other.
        assert StateFile(state_path, "host-a:18901").completed() == {42}
        assert StateFile(state_path, "host-b:18902").completed() == set()

    def test_an_unmonitored_author_is_skipped_and_counted_incomplete(
        self, tmp_path: pathlib.Path
    ) -> None:
        transport = RecordingTransport([book(1, ["Ada Wren"], ["Cyberpunk"])])
        transport.monitored = {}  # not monitored, so there is no id to refresh

        assert self.go(transport, tmp_path / "state.json") == EXIT_REFRESH_INCOMPLETE
        assert transport.posts() == []

    def test_a_language_no_row_holds_finds_nobody(self, tmp_path: pathlib.Path) -> None:
        # The defect this tool shipped with. "all" reads like a wildcard and is not one: the
        # endpoint matches it against the stored column, so every author on a real install came
        # back unmonitored and nothing was refreshed. Measured on the install before this test
        # existed: language=all answered {"isMonitored":false} for an author that language=english
        # answered with an id.
        transport = RecordingTransport([book(1, ["Ada Wren"], ["Cyberpunk"])])
        transport.monitored = {"Ada Wren": 42}
        transport.monitored_language = "english"

        code = run(
            ListenarrApi(transport), wanted=[CYBERPUNK], mode=MATCH_PHRASE, do_refresh=True,
            force=True, region="us", language="all",
            state=StateFile(tmp_path / "state.json", "host:18901"),
            sequencer=Sequencer(
                ListenarrApi(transport), 5.0, 600.0, 5, Clock().sleep, Clock(), quiet
            ),
            log=quiet,
        )

        assert code == EXIT_REFRESH_INCOMPLETE
        assert transport.posts() == []

    def test_the_control_the_stored_language_finds_the_author(
        self, tmp_path: pathlib.Path
    ) -> None:
        # The control that has to come out differently. Same library, same monitored author, same
        # stub: only the language asked for changes. Without this, a tool that could never find
        # anybody would pass the test above.
        transport = RecordingTransport([book(1, ["Ada Wren"], ["Cyberpunk"])])
        transport.monitored = {"Ada Wren": 42}
        transport.monitored_language = "english"
        transport.start_responses = [accepted("mine")]
        transport.status_responses = {"mine": [finished("mine")]}
        clock = Clock()

        code = run(
            ListenarrApi(transport), wanted=[CYBERPUNK], mode=MATCH_PHRASE, do_refresh=True,
            force=True, region="us", language="english",
            state=StateFile(tmp_path / "state.json", "host:18901"),
            sequencer=Sequencer(ListenarrApi(transport), 5.0, 600.0, 5, clock.sleep, clock, quiet),
            log=quiet,
        )

        assert code == EXIT_OK
        assert transport.posts() != []


def _outcome_completed() -> Any:
    from genre_refresh import RunOutcome

    return RunOutcome(COMPLETED, "r1", processed=4, updated=3)


class TestProductionPortGuard:
    """Port 4545 is a live instance's, and this tool does not reach one by accident."""

    def args(self, base_url: str, *extra: str) -> Any:
        return parse_args(["--base-url", base_url, "--genre", CYBERPUNK, *extra])

    def test_it_refuses_the_production_port_in_both_modes(self) -> None:
        # Reading is still talking to it, and a tool that will read production is one flag away
        # from writing to it. Both modes are checked in one test because the guard runs before
        # the mode is looked at, and a pair of identical tests would only look like a control.
        with pytest.raises(SystemExit) as list_only:
            self.args("http://somewhere:4545")
        with pytest.raises(SystemExit) as refreshing:
            self.args("http://somewhere:4545", "--refresh")
        assert list_only.value.code == 2
        assert refreshing.value.code == 2

    def test_the_control_any_other_port_is_accepted(self) -> None:
        # Without this the refusal could be a parser that rejects every URL.
        parsed = self.args("http://somewhere:18901")
        assert parsed.base_url == "http://somewhere:18901"
        assert parsed.refresh is False

    def test_the_explicit_flag_is_what_unlocks_it(self) -> None:
        parsed = self.args("http://somewhere:4545", "--allow-production-port")
        assert parsed.base_url == "http://somewhere:4545"

    def test_a_url_with_no_usable_port_is_refused(self) -> None:
        with pytest.raises(SystemExit) as exit_info:
            self.args("somewhere:4545")
        assert exit_info.value.code == 2

    def test_port_is_read_from_the_scheme_when_it_is_not_written_down(self) -> None:
        assert port_of("http://somewhere") == 80
        assert port_of("https://somewhere") == 443
        assert port_of("http://somewhere:4545") == 4545

    def test_the_state_key_is_the_host_and_port(self) -> None:
        assert target_of("http://Somewhere:18901/") == "somewhere:18901"


class TestRealTransport:
    """The one piece the fake transport cannot stand in for.

    Everything above this class asserts on a recorded call list, which proves the sequencing but
    says nothing about whether a 409 can be read at all. urllib raises on any 4xx, and the whole
    in-flight design rests on reading the run id out of the body of a refused POST. A stub on an
    ephemeral loopback port settles it. Port 4545 is a live instance's and is never bound here;
    the stub takes whatever port the kernel hands out.
    """

    def serve(self, handler_factory: Any) -> Any:
        from http.server import HTTPServer

        server = HTTPServer(("127.0.0.1", 0), handler_factory)
        assert server.server_address[1] != 4545
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server

    def test_a_409_comes_back_as_a_response_rather_than_an_exception(self) -> None:
        from genre_refresh import UrllibTransport

        seen: list[tuple[str, str, str | None]] = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_: Any) -> None:
                pass

            # do_GET and do_POST are the names BaseHTTPRequestHandler dispatches to.
            def do_GET(self) -> None:
                seen.append(("GET", self.path, None))
                self._reply(200, {"token": "csrf-token-value"})

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                seen.append(("POST", self.path, self.headers.get("X-XSRF-TOKEN")))
                self._reply(409, {"runId": "held", "scope": "Library", "totalBooks": 900,
                                  "status": "Running"})

            def _reply(self, status: int, body: dict[str, Any]) -> None:
                payload = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        server = self.serve(Handler)
        try:
            host, port = server.server_address[0], server.server_address[1]
            transport = UrllibTransport(f"http://{host}:{port}", None, timeout=10.0)
            response = transport("POST", "/library/refresh-metadata", {"authorId": 42})
        finally:
            server.shutdown()
            server.server_close()

        # The claim: a refused POST is a Response, not a raised HTTPError, so the run id in its
        # body is reachable.
        assert response.status == 409
        assert response.dict_body()["runId"] == "held"
        # And the control on the CSRF dance: the POST carried the token the GET issued. Without
        # this, a transport that never fetched a token would pass the assertion above against a
        # stub that does not check one.
        assert ("GET", "/api/v1/antiforgery/token", None) in seen
        assert [call for call in seen if call[0] == "POST"] == [
            ("POST", "/api/v1/library/refresh-metadata", "csrf-token-value")
        ]


class TestCompoundRequests:
    """A genre copied out of the interface as one compound string has to be expressible."""

    def test_a_compound_request_matches_the_compound_tag(self) -> None:
        # Before the request was split the same way the stored side is, this matched nothing:
        # the request stayed one token run that no split stored genre could contain, and the
        # tool reported a library with none of that genre.
        assert genre_matches(["Science Fiction & Fantasy"], ["Science Fiction & Fantasy"],
                             MATCH_PHRASE)
        assert genre_matches(["Mystery, Thriller & Suspense"],
                             ["Mystery", "Thriller", "Suspense"], MATCH_PHRASE)

    def test_a_compound_request_is_an_and(self) -> None:
        # The control that fixes the meaning: if the parts were ORed, this would select every
        # fantasy book from a request that named science fiction first.
        assert not genre_matches(["Science Fiction & Fantasy"], ["Fantasy"], MATCH_PHRASE)
        assert not genre_matches(["Science Fiction & Fantasy"], ["Science Fiction"], MATCH_PHRASE)

    def test_the_parts_asked_for_separately_are_an_or(self) -> None:
        matched = genre_matches(["science fiction", "fantasy"], ["Fantasy"], MATCH_PHRASE)
        assert matched == ["fantasy"]

    def test_accents_fold_so_one_providers_spelling_matches_anothers(self) -> None:
        assert genre_matches(["ciencia ficcion"], ["Ciencia Ficción"], MATCH_EXACT)
        # The control: folding accents must not fold two different genres together.
        assert not genre_matches(["ciencia ficcion"], ["Novela Negra"], MATCH_EXACT)

    def test_a_non_latin_genre_survives_normalisation(self) -> None:
        # It used to normalise to the empty string, which made the book invisible with no
        # warning. The control is that it still does not match something else.
        assert genre_matches(["Научная фантастика"],
                             ["Научная фантастика"], MATCH_EXACT)
        assert not genre_matches(["Научная фантастика"],
                                 ["Romance"], MATCH_EXACT)

    def test_one_author_named_twice_on_a_book_counts_once(self) -> None:
        library = [{"id": 1, "authors": ["Ada Wren", "ada wren"], "genres": ["Cyberpunk"],
                    "seriesMemberships": []}]

        selected = select_authors(library, [CYBERPUNK], MATCH_PHRASE)

        assert len(selected) == 1
        assert selected[0].book_ids == [1]
        assert selected[0].matching_book_ids == [1]


class TestUnsettledBooks:
    """Completed means the run reached the end of the list, not that every book answered."""

    def build(self, transport: RecordingTransport, clock: Clock) -> Sequencer:
        return Sequencer(ListenarrApi(transport), 5.0, 600.0, 5, clock.sleep, clock, quiet)

    def test_a_completed_run_with_deferred_books_is_not_finished(self) -> None:
        transport = RecordingTransport()
        clock = Clock()
        transport.start_responses = [accepted("mine")]
        transport.status_responses = {"mine": [finished("mine", deferred=2)]}

        outcome = self.build(transport, clock).refresh(author_id=7, force=True)

        assert outcome.status == COMPLETED
        assert outcome.unsettled == 2
        assert not outcome.finished

    def test_a_completed_run_with_failed_books_is_not_finished(self) -> None:
        transport = RecordingTransport()
        clock = Clock()
        transport.start_responses = [accepted("mine")]
        transport.status_responses = {"mine": [finished("mine", failed=1)]}

        assert not self.build(transport, clock).refresh(author_id=7, force=True).finished

    def test_the_control_a_clean_completed_run_is_finished(self) -> None:
        # Without this the two above would pass for a tool that never records anything.
        transport = RecordingTransport()
        clock = Clock()
        transport.start_responses = [accepted("mine")]
        transport.status_responses = {"mine": [finished("mine")]}

        outcome = self.build(transport, clock).refresh(author_id=7, force=True)

        assert outcome.unsettled == 0
        assert outcome.finished

    def test_an_author_with_unsettled_books_is_offered_again(
        self, tmp_path: pathlib.Path
    ) -> None:
        state_path = tmp_path / "state.json"
        first = RecordingTransport([book(1, ["Ada Wren"], ["Cyberpunk"])])
        first.monitored = {"Ada Wren": 42}
        first.start_responses = [accepted("r1")]
        first.status_responses = {"r1": [finished("r1", deferred=1)]}
        clock = Clock()

        code = run(
            ListenarrApi(first), wanted=[CYBERPUNK], mode=MATCH_PHRASE, do_refresh=True,
            force=True, region="us", language="all",
            state=StateFile(state_path, "host:18901"),
            sequencer=Sequencer(ListenarrApi(first), 5.0, 600.0, 5, clock.sleep, clock, quiet),
            log=quiet,
        )

        assert code == EXIT_REFRESH_INCOMPLETE
        assert StateFile(state_path, "host:18901").completed() == set()


class TestCollisionBudget:
    """A run holding the gate must not cost max_attempts whole run timeouts."""

    def test_giving_up_on_the_held_run_gives_up_on_the_author(self) -> None:
        transport = RecordingTransport()
        clock = Clock()
        transport.start_responses = [in_flight("held")] * 5
        transport.status_responses = {"held": [running("held")]}

        outcome = Sequencer(
            ListenarrApi(transport), poll_interval=5.0, run_timeout=20.0, max_attempts=5,
            sleep=clock.sleep, now=clock, log=quiet,
        ).refresh(author_id=7, force=True)

        assert outcome.status == GAVE_UP
        # One POST, not five. Waiting out the same stuck run five times is the difference
        # between a long wait and a day of them.
        assert len(transport.posts()) == 1

    def test_the_control_a_gate_that_clears_lets_the_author_through(self) -> None:
        transport = RecordingTransport()
        clock = Clock()
        transport.start_responses = [in_flight("held"), accepted("mine")]
        transport.status_responses = {"held": [running("held"), finished("held")],
                                      "mine": [finished("mine")]}

        outcome = Sequencer(
            ListenarrApi(transport), poll_interval=5.0, run_timeout=600.0, max_attempts=5,
            sleep=clock.sleep, now=clock, log=quiet,
        ).refresh(author_id=7, force=True)

        assert outcome.status == COMPLETED
        assert len(transport.posts()) == 2

    def test_a_forgotten_run_does_not_become_a_hot_loop(self) -> None:
        # A 404 on the status poll returns without sleeping, so the retry after a collision has
        # to carry its own floor or the client spins as fast as the network allows.
        transport = RecordingTransport()
        clock = Clock()
        transport.start_responses = [in_flight("gone"), accepted("mine")]
        transport.status_responses = {"mine": [finished("mine")]}
        transport.status_responses["gone"] = [Response(404, {}, {"message": "no such run"})]

        outcome = Sequencer(
            ListenarrApi(transport), poll_interval=5.0, run_timeout=600.0, max_attempts=5,
            sleep=clock.sleep, now=clock, log=quiet,
        ).refresh(author_id=7, force=True)

        # A forgotten run means the gate is free, so the author IS re-offered, unlike a run that
        # outlasted its deadline and is therefore still holding it.
        assert outcome.status == COMPLETED
        assert len(transport.posts()) == 2
        assert clock.slept == [5.0]
        assert not RunOutcome(FORGOTTEN).finished


class TestTargetKey:
    """The state key is the host and port, and nothing else."""

    def test_credentials_in_the_url_stay_out_of_the_key(self) -> None:
        # Which is what keeps them out of the state file on disk.
        assert target_of("http://operator:hunter2@host:18901") == "host:18901"

    def test_the_same_instance_with_and_without_credentials_is_one_key(self) -> None:
        assert target_of("http://host:18901") == target_of("http://operator@host:18901")

    def test_an_implied_port_and_a_written_one_agree(self) -> None:
        assert target_of("http://host") == target_of("http://host:80")

    def test_the_control_two_instances_are_two_keys(self) -> None:
        assert target_of("http://host:18901") != target_of("http://host:18902")


class TestArgumentValidation:
    """Values that would silently turn the tool into a no-op are refused."""

    def args(self, *extra: str) -> Any:
        return parse_args(["--base-url", "http://h:18901", "--genre", CYBERPUNK, *extra])

    def test_the_default_language_is_one_a_row_can_hold(self) -> None:
        # The bug this tool shipped with, pinned at the level it actually occurred. The default
        # was "all", which reads like a wildcard and is not one: the monitoring endpoint matches
        # the value against the stored column, so the default found nobody on a real install and
        # every author was skipped. The tests above pass a language explicitly, so none of them
        # could have caught a wrong default.
        assert self.args().language != "all"
        assert self.args().language == "english"

    def test_an_explicit_language_still_wins(self) -> None:
        # The control. If the parser ignored the flag, the assertion above would pass for the
        # wrong reason and this one would fail.
        assert self.args("--language", "german").language == "german"

    def test_zero_attempts_is_refused(self) -> None:
        # With zero the loop body never runs, no POST is issued, and every author is reported
        # incomplete as though the server had refused it.
        with pytest.raises(SystemExit) as exit_info:
            self.args("--max-attempts", "0")
        assert exit_info.value.code == 2

    def test_a_negative_run_timeout_is_refused(self) -> None:
        with pytest.raises(SystemExit):
            self.args("--run-timeout", "-1")

    def test_zero_run_timeout_is_allowed_and_means_no_deadline(self) -> None:
        # The control: zero is a real setting, not a mistake, so it must not be refused with
        # the negative values.
        assert self.args("--run-timeout", "0").run_timeout == 0

    def test_the_default_run_timeout_outlasts_the_servers_own_window(self) -> None:
        # The server windows every run at MetadataRefreshIntervalHours, which defaults to 24.
        # A client deadline shorter than that abandons runs the server goes on to finish.
        assert self.args().run_timeout > 24 * 60 * 60


class TestRedirectGuard:
    """The port refusal is checked once, and urllib follows redirects."""

    def test_a_redirect_to_another_origin_is_refused(self) -> None:
        from genre_refresh import SameOriginRedirect

        handler = SameOriginRedirect("host:18901")
        request = urllib.request.Request("http://host:18901/api/v1/library")

        with pytest.raises(ApiError):
            handler.redirect_request(
                request, None, 302, "Found", {}, "http://host:4545/api/v1/library"
            )

    def test_the_control_a_redirect_within_the_same_origin_is_allowed(self) -> None:
        # Without this the guard could be a handler that refuses every redirect, which would
        # break an instance behind a URL base that redirects to add a trailing slash.
        from genre_refresh import SameOriginRedirect

        handler = SameOriginRedirect("host:18901")
        request = urllib.request.Request("http://host:18901/api/v1/library")

        assert handler.redirect_request(
            request, None, 302, "Found", {}, "http://host:18901/api/v1/library/"
        ) is not None


class TestRedirectGuardOnTheRequestPath:
    """The hand-called guard tests above prove the comparison, not that it is ever consulted.

    Both of those would pass if the handler were never passed to build_opener at all, or if
    urllib installed its own default alongside it and ran that one first. The guard is a safety
    property about not reaching a live instance, so it gets measured through a socket.
    """

    def serve(self, handler_factory: Any) -> Any:
        from http.server import HTTPServer

        server = HTTPServer(("127.0.0.1", 0), handler_factory)
        assert server.server_address[1] != 4545
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server

    def stub(self, seen: list[str], location: dict[str, str | None]) -> Any:
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_: Any) -> None:
                pass

            def do_GET(self) -> None:  # the name BaseHTTPRequestHandler dispatches to
                seen.append(self.path)
                target = location["value"]
                if target and self.path.endswith("/redirect"):
                    self.send_response(302)
                    self.send_header("Location", target)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                payload = json.dumps({"ok": True}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        return Handler

    def test_a_redirect_to_another_origin_is_refused_and_never_reaches_it(self) -> None:
        from genre_refresh import UrllibTransport

        here_seen: list[str] = []
        there_seen: list[str] = []
        location: dict[str, str | None] = {"value": None}

        here = self.serve(self.stub(here_seen, location))
        there = self.serve(self.stub(there_seen, {"value": None}))
        try:
            there_url = f"http://127.0.0.1:{there.server_address[1]}"
            here_url = f"http://127.0.0.1:{here.server_address[1]}"
            transport = UrllibTransport(here_url, None, timeout=10.0)

            # The apparatus control first: the stub really does redirect, and a same-origin
            # redirect is followed. Without this the refusal below could be a stub that never
            # sent a Location at all, which is how this test failed the first time it was run.
            location["value"] = f"{here_url}/api/v1/library"
            assert transport("GET", "/redirect").status == 200
            assert here_seen == ["/api/v1/redirect", "/api/v1/library"]

            # The finding: a redirect that leaves the origin is refused.
            here_seen.clear()
            location["value"] = f"{there_url}/api/v1/library"
            with pytest.raises(ApiError):
                transport("GET", "/redirect")

            # And it never arrived. A guard that raised after following would be no guard.
            assert there_seen == []
        finally:
            for server in (here, there):
                server.shutdown()
                server.server_close()

    def test_a_relative_location_is_still_followed(self) -> None:
        # A real instance behind a URL base issues these, and urllib resolves them against the
        # request before the guard sees them. If the guard refused these it would break a
        # perfectly ordinary deployment.
        from genre_refresh import UrllibTransport

        seen: list[str] = []
        location: dict[str, str | None] = {"value": "/api/v1/library"}
        server = self.serve(self.stub(seen, location))
        try:
            base = f"http://127.0.0.1:{server.server_address[1]}"
            assert UrllibTransport(base, None, timeout=10.0)("GET", "/redirect").status == 200
            assert seen == ["/api/v1/redirect", "/api/v1/library"]
        finally:
            server.shutdown()
            server.server_close()


class TestPollingAndStateHardening:
    """The cases that used to end in a long spin or a traceback."""

    def test_a_200_with_no_status_is_an_apparatus_failure(self) -> None:
        # It used to fall through to the sleep and poll for the whole run timeout, which at the
        # defaults is thousands of requests against an instance plainly not answering.
        transport = RecordingTransport()
        clock = Clock()
        transport.start_responses = [accepted("mine")]
        transport.status_responses = {"mine": [Response(200, {}, {"runId": "mine"})]}

        with pytest.raises(ApiError):
            Sequencer(ListenarrApi(transport), 5.0, 600.0, 5, clock.sleep, clock, quiet).refresh(
                author_id=7, force=True
            )
        assert clock.slept == []

    def test_the_control_a_200_with_a_status_still_polls(self) -> None:
        transport = RecordingTransport()
        clock = Clock()
        transport.start_responses = [accepted("mine")]
        transport.status_responses = {"mine": [running("mine"), finished("mine")]}

        outcome = Sequencer(
            ListenarrApi(transport), 5.0, 600.0, 5, clock.sleep, clock, quiet
        ).refresh(author_id=7, force=True)

        assert outcome.status == COMPLETED
        assert clock.slept == [5.0]

    def test_a_state_file_whose_targets_is_not_an_object_is_refused(
        self, tmp_path: pathlib.Path
    ) -> None:
        path = tmp_path / "state.json"
        path.write_text(json.dumps({"version": 1, "targets": ["not", "an", "object"]}))

        with pytest.raises(ApiError):
            StateFile(path, "host:18901")

    def test_a_state_file_from_a_future_version_is_refused(
        self, tmp_path: pathlib.Path
    ) -> None:
        path = tmp_path / "state.json"
        path.write_text(json.dumps({"version": 99, "targets": {}}))

        with pytest.raises(ApiError):
            StateFile(path, "host:18901")

    def test_the_control_a_current_state_file_loads(self, tmp_path: pathlib.Path) -> None:
        path = tmp_path / "state.json"
        path.write_text(json.dumps({"version": 1, "targets": {"host:18901":
                                    {"completed": {"42": {"name": "Ada Wren"}}}}}))

        assert StateFile(path, "host:18901").completed() == {42}

    def test_no_temporary_file_is_left_behind(self, tmp_path: pathlib.Path) -> None:
        path = tmp_path / "state.json"
        StateFile(path, "host:18901").record(42, "Ada Wren", _outcome_completed())

        assert path.exists()
        assert list(tmp_path.glob("*.tmp")) == []


class TestPendingRecord:
    """An author that did not finish is written down, so a repeat can be reported."""

    def test_an_unfinished_author_is_recorded_as_pending_with_its_count(
        self, tmp_path: pathlib.Path
    ) -> None:
        path = tmp_path / "state.json"
        state = StateFile(path, "host:18901")
        state.record_pending(42, "Ada Wren", RunOutcome(COMPLETED, "r1", deferred=3))

        assert StateFile(path, "host:18901").previous_unsettled(42) == 3
        # It is NOT recorded as completed, which is what makes it get re-offered.
        assert StateFile(path, "host:18901").completed() == set()

    def test_the_control_an_author_never_seen_has_no_previous_count(
        self, tmp_path: pathlib.Path
    ) -> None:
        assert StateFile(tmp_path / "state.json", "host:18901").previous_unsettled(42) is None

    def test_finishing_clears_the_pending_record(self, tmp_path: pathlib.Path) -> None:
        path = tmp_path / "state.json"
        state = StateFile(path, "host:18901")
        state.record_pending(42, "Ada Wren", RunOutcome(COMPLETED, "r1", deferred=3))
        state.record(42, "Ada Wren", _outcome_completed())

        reloaded = StateFile(path, "host:18901")
        assert reloaded.completed() == {42}
        assert reloaded.previous_unsettled(42) is None


class TestEmptyGenreGuard:
    """A genre that normalises away is the one argument that could still silently no-op."""

    def args(self, *genres: str) -> Any:
        flags: list[str] = []
        for genre in genres:
            flags += ["--genre", genre]
        return parse_args(["--base-url", "http://h:18901", *flags])

    def test_a_request_that_names_nothing_is_a_usage_error_not_an_empty_library(self) -> None:
        # Exit 1 is published as "no author in the library matched", which is a claim about the
        # library. A request naming nothing is a claim about the request.
        for genre in ("", "   ", "&", "---"):
            with pytest.raises(SystemExit) as exit_info:
                self.args(genre)
            assert exit_info.value.code == 2

    def test_the_control_one_usable_genre_among_empties_is_accepted(self) -> None:
        parsed = self.args("&", CYBERPUNK)
        assert parsed.genres == ["&", CYBERPUNK]

    def test_the_control_an_ordinary_request_is_accepted(self) -> None:
        assert self.args(CYBERPUNK).genres == [CYBERPUNK]


class TestAndSpelling:
    """The word "and" joins within a genre; it does not separate two."""

    def test_phrase_mode_does_not_care_which_spelling(self) -> None:
        assert genre_matches(["fantasy"], ["Science Fiction and Fantasy"], MATCH_PHRASE)
        assert genre_matches(["fantasy"], ["Science Fiction & Fantasy"], MATCH_PHRASE)

    def test_exact_mode_does_and_that_is_documented(self) -> None:
        # The ampersand separates, so "fantasy" is a whole stored genre.
        assert genre_matches(["fantasy"], ["Science Fiction & Fantasy"], MATCH_EXACT)
        # The word does not, so the stored genre is three words and "fantasy" is not equal to it.
        assert not genre_matches(["fantasy"], ["Science Fiction and Fantasy"], MATCH_EXACT)

    def test_a_joining_and_inside_one_genre_is_ignored(self) -> None:
        assert genre_matches(["rock and roll"], ["Rock Roll"], MATCH_EXACT)
        assert genre_matches(["rock roll"], ["Rock and Roll"], MATCH_EXACT)


class TestExitCodeContract:
    """A crash must never be readable as an answer about the library."""

    def test_an_unanticipated_error_is_an_api_failure_not_no_match(self) -> None:
        from genre_refresh import EXIT_API, report_failure

        # Python's default for an uncaught exception is 1, which this tool publishes as
        # EXIT_NO_MATCH, "no author in the library matched". A script branching on the code
        # would read a crash as a fact about the library.
        assert report_failure(TypeError("something unforeseen")) == EXIT_API
        assert report_failure(TypeError("x")) != EXIT_NO_MATCH

    def test_the_controls_each_anticipated_error_keeps_its_own_code(self) -> None:
        from genre_refresh import EXIT_API, EXIT_INTERRUPTED, report_failure

        assert report_failure(ApiError("unreachable")) == EXIT_API
        assert report_failure(RefreshDisabled("off")) == EXIT_API
        # This one has to come out differently, or the mapping could be "everything is 3".
        assert report_failure(KeyboardInterrupt()) == EXIT_INTERRUPTED
