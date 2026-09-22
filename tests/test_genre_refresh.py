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
from http.server import BaseHTTPRequestHandler
from typing import Any

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

from genre_refresh import (
    COMPLETED,
    EXIT_NO_MATCH,
    EXIT_OK,
    EXIT_REFRESH_INCOMPLETE,
    GAVE_UP,
    MATCH_EXACT,
    MATCH_PHRASE,
    ApiError,
    ListenarrApi,
    RefreshDisabled,
    Response,
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
            name = path.split("name=")[1].split("&")[0].replace("+", " ").replace("%20", " ")
            author_id = self.monitored.get(name)
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


def _outcome_completed() -> Any:
    from genre_refresh import RunOutcome

    return RunOutcome(COMPLETED, "r1", processed=4, updated=3)


class TestProductionPortGuard:
    """Port 4545 is a live instance's, and this tool does not reach one by accident."""

    def args(self, base_url: str, *extra: str) -> Any:
        return parse_args(["--base-url", base_url, "--genre", CYBERPUNK, *extra])

    def test_it_refuses_the_production_port(self) -> None:
        with pytest.raises(SystemExit) as exit_info:
            self.args("http://somewhere:4545")
        assert exit_info.value.code == 2

    def test_it_refuses_the_production_port_in_list_only_mode_too(self) -> None:
        # Reading is still talking to it, and a tool that will read production is one flag away
        # from writing to it.
        with pytest.raises(SystemExit) as exit_info:
            self.args("http://somewhere:4545")
        assert exit_info.value.code == 2

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
