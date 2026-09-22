#!/usr/bin/env python3
"""Refresh provider metadata for the authors whose books carry chosen genres, one at a time.

Why this exists
---------------

The scheduled walk stamps every row on upgrade through a startup backfill, so nothing comes due
for a staleness window, and a forced library-wide run is a multi-day walk at the shipped request
budget. An operator who wants a couple of genres filled in first should not have to wait behind
the whole library. This picks the authors those genres point at and refreshes them in order,
forced, one run at a time.

What it does NOT do. It never changes a setting, never raises the request budget, and never runs
two refreshes at once. The provider behind this is a small community service and the server's
throttle is the whole reason a library-wide run takes days; going around it is not an
optimisation, it is someone else's outage.

Defaults
--------

LIST ONLY. Without --refresh it prints the authors it would refresh, in order, and stops. Nothing
about this tool should be discovered by running it.

The host is an argument. It refuses to talk to port 4545 at all unless --allow-production-port is
passed, because that is the port a live instance uses and this repository is public.

Genre matching
--------------

Provider genre strings are inconsistent, so matching is a judgement call and the choice is stated
here rather than buried.

Both sides are normalised the same way and split the same way. Normalising case-folds, folds
accents away, and turns every run of separators into a single space. Splitting breaks a genre on
the characters providers use to pack several genres into one string (comma, semicolon, slash,
pipe, ampersand, plus) and on the word "and", so "Science Fiction & Fantasy" is two genres rather
than one long one, whether it arrives from the provider or is typed as the request.

The default mode is `phrase`: a request part matches when its normalised words appear as a
consecutive run of words inside one of the book's normalised genres, and every part of the request
has to match something. So asking for "science fiction" catches "Military Science Fiction" and
"Science Fiction & Fantasy", while asking for "Science Fiction & Fantasy" needs the book to carry
both. `--match exact` requires each part to equal a whole stored genre.

What this MISSES, in both directions:

* Synonyms and abbreviations. "sci-fi" normalises to "sci fi" and does not match "science
  fiction". Neither does "SF". There is no synonym list and no stemming; a hyphen inside a single
  word is a space afterwards, so "cyber-punk" does not match "cyberpunk" either.
* A book tagged more broadly than the request. Asking for "military science fiction" does not
  select a book tagged only "Science Fiction". Ask for the broader phrase to catch the narrower
  tags, not the other way round.
* A generic head word over-matches. Asking for "fiction" selects "Historical Fiction",
  "Literary Fiction" and everything else built on the word. That is the cost of catching
  compounds, and the mitigation is to ask for a specific phrase.
* A compound request is an AND. Asking for "Science Fiction & Fantasy" will not select a book
  tagged only "Science Fiction". Ask for the parts separately, as two --genre arguments, to get
  either one.
* Anything the provider never wrote down. Selection can only see genres already stored, so a book
  whose metadata has never been populated has no genres and is invisible to this tool. That is a
  real gap for exactly the library this is meant to help, and there is no client-side fix for it:
  a book with no genre cannot be selected by genre.

Ordering
--------

Genres are given in priority order and authors come back in that order: an author is ranked by the
earliest-listed genre any of their books match, then by how many of their books match, then by
name. The ordering is total and deterministic, so two runs over an unchanged library agree.

Resumability
------------

It will be interrupted. A state file (--state, default genre-refresh-state.json in the working
directory) records the monitored author ids that finished, keyed by the target host and port so
one file can serve more than one instance. It is rewritten through a temporary file and renamed
after each author, so an interrupt loses at most the author in flight.

An author is recorded only when the run came back `Completed` AND left nothing unsettled.
`Completed` on its own means the run reached the end of the author's list, not that every book on
it came back with an answer: a book the provider pushed back on is counted as deferred, one that
threw is counted as failed, and neither is stamped. Recording those would mean the tool never
comes back for them. `Truncated` means the run spent its request window before reaching the end,
so the rest keep their unset timestamps too. `Failed` and `Cancelled` likewise stay pending.

The cost of that choice: an author with a book that never settles is offered again on every
invocation, spending the provider budget on the rest of that author each time. The run reports
how many books did not settle, so a repeat is visible rather than silent, and an author that
reports the same number twice is one to stop re-running.

Cost
----

The library read is the whole library in one response, because the list endpoint takes no paging
or filter parameters. It is re-read after each author so the after-count is current, which is one
full library serialisation per author refreshed. That is local work on the server and spends none
of the provider budget, but it is not free on a large library.

Exit codes
----------

0, everything asked for was done: listed, or every selected author finished.
1, no author in the library matched the requested genres.
2, usage, including the production-port refusal.
3, the API could not be used: unreachable, unexpected shape, or the feature is turned off.
4, at least one author did not finish. Its state was not recorded, so run it again.
5, interrupted.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Any, Protocol

PRODUCTION_PORT = 4545
STATE_VERSION = 1
DEFAULT_STATE_FILE = "genre-refresh-state.json"

EXIT_OK = 0
EXIT_NO_MATCH = 1
EXIT_USAGE = 2
EXIT_API = 3
EXIT_REFRESH_INCOMPLETE = 4
EXIT_INTERRUPTED = 5

# MetadataRefreshRunState.ToString(), Listenarr.Application/Metadata/Refresh/MetadataRefreshRun.cs.
RUNNING = "Running"
COMPLETED = "Completed"

# The one run state that means an author's whole catalogue was reached.
TERMINAL_GOOD = frozenset({COMPLETED})

# Providers pack several genres into one string with these. "and" is a word, so it is handled by
# the tokeniser rather than here.
GENRE_SEPARATORS = re.compile(r"[,;/|&+]")
NON_ALPHANUMERIC = re.compile(r"[\W_]+", re.UNICODE)

MATCH_PHRASE = "phrase"
MATCH_EXACT = "exact"


class ApiError(RuntimeError):
    """The instance could not be used well enough for the result to mean anything."""


class RefreshDisabled(ApiError):
    """The metadata refresh feature is turned off in settings, so no run can start."""


@dataclass(frozen=True)
class Response:
    """One HTTP answer, parsed far enough to branch on."""

    status: int
    headers: Mapping[str, str]
    body: Any

    def dict_body(self) -> dict[str, Any]:
        """The body as an object, or an empty one when it was not an object."""
        return self.body if isinstance(self.body, dict) else {}


class Transport(Protocol):
    """Whatever actually moves a request. Injected so tests can assert on the calls made."""

    def __call__(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> Response: ...


def normalize_genre(text: str) -> str:
    """Case-fold a genre, fold its accents away, and reduce separators to single spaces.

    The accent folding is what lets "Ciencia Ficcion" match "Ciencia Ficcion" written with the
    accent, which is the common case where one provider accents a genre and another does not.
    Letters outside the Latin script survive rather than being stripped to nothing, so a genre in
    another script can still be asked for by pasting it.

    Args:
        text: A genre string as a provider wrote it.

    Returns:
        The normalised form, which may be empty.
    """
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    unaccented = "".join(char for char in decomposed if not unicodedata.combining(char))
    return NON_ALPHANUMERIC.sub(" ", unaccented).strip()


def split_genre_field(text: str) -> list[str]:
    """Split one stored genre string into the genres it actually names.

    "Science Fiction & Fantasy" is one string holding two genres, and matching it whole would
    mean an operator has to know which compound a provider happened to use.

    Args:
        text: A genre string as a provider wrote it.

    Returns:
        The normalised genres it contains, without empties.
    """
    parts = GENRE_SEPARATORS.split(text)
    return [normalized for part in parts if (normalized := normalize_genre(part))]


def genre_tokens(text: str) -> tuple[str, ...]:
    """The words of a normalised genre, with the joining word "and" dropped.

    "and" is dropped because splitting already treats "&" as a separator, and a genre written
    "Science Fiction and Fantasy" should behave the same as one written with the ampersand.
    """
    return tuple(word for word in normalize_genre(text).split() if word != "and")


def genre_matches(wanted: Sequence[str], stored: Iterable[str], mode: str) -> list[str]:
    """Which of the requested genres this book's stored genres carry.

    Args:
        wanted: Requested genres, in priority order, as the operator typed them.
        stored: The book's genre strings, as the API returned them.
        mode: MATCH_PHRASE or MATCH_EXACT.

    Returns:
        The requested genres that matched, in the order they were requested.
    """
    book_genres = [genre for value in stored if value for genre in split_genre_field(value)]
    book_tokens = [genre_tokens(genre) for genre in book_genres]

    matched: list[str] = []
    for request in wanted:
        # The request is split exactly like a stored genre is. An operator copying a compound
        # out of the interface, "Science Fiction & Fantasy", is naming two genres, and without
        # this the request stays one long token run that no split stored genre can contain: it
        # would match nothing at all and report it as a library with none of that genre.
        parts = [tokens for part in split_genre_field(request) if (tokens := genre_tokens(part))]
        if not parts:
            continue
        # Every part has to be present. A compound names both genres, so requiring both is the
        # reading that does not quietly widen "Science Fiction & Fantasy" into all of fantasy.
        if mode == MATCH_EXACT:
            hit = all(any(tokens == part for tokens in book_tokens) for part in parts)
        else:
            hit = all(any(_contains_run(tokens, part) for tokens in book_tokens) for part in parts)
        if hit:
            matched.append(request)
    return matched


def _contains_run(haystack: tuple[str, ...], needle: tuple[str, ...]) -> bool:
    """Whether needle appears as a consecutive run of whole words inside haystack."""
    if not needle or len(needle) > len(haystack):
        return False
    return any(
        haystack[start : start + len(needle)] == needle
        for start in range(len(haystack) - len(needle) + 1)
    )


@dataclass
class AuthorSelection:
    """One author the requested genres point at, and what their books look like now."""

    name: str
    priority: int
    matched_genres: list[str] = field(default_factory=list)
    book_ids: list[int] = field(default_factory=list)
    matching_book_ids: list[int] = field(default_factory=list)

    def sort_key(self) -> tuple[int, int, str]:
        """Earliest matching genre first, then the most matching books, then the name."""
        return self.priority, -len(self.matching_book_ids), self.name.casefold()


def select_authors(
    items: Sequence[Mapping[str, Any]], wanted: Sequence[str], mode: str
) -> list[AuthorSelection]:
    """Pick the authors whose books carry any requested genre, in priority order.

    Args:
        items: The library as the list endpoint returned it.
        wanted: Requested genres, in priority order.
        mode: MATCH_PHRASE or MATCH_EXACT.

    Returns:
        One entry per selected author, ordered by priority.
    """
    selections: dict[str, AuthorSelection] = {}

    for item in items:
        book_id = item.get("id")
        if not isinstance(book_id, int):
            continue
        # Deduped per item, case-insensitively. An item naming the same author twice would
        # otherwise push its id in twice and inflate both the displayed counts and the ordering.
        authors: dict[str, str] = {}
        for name in _string_list(item.get("authors")):
            if name.strip():
                authors.setdefault(name.casefold(), name)
        if not authors:
            continue

        matched = genre_matches(wanted, _string_list(item.get("genres")), mode)
        for name in authors.values():
            key = name.casefold()
            selection = selections.get(key)
            if selection is None:
                selection = AuthorSelection(name=name, priority=len(wanted))
                selections[key] = selection
            selection.book_ids.append(book_id)
            if matched:
                selection.matching_book_ids.append(book_id)
                selection.priority = min(selection.priority, wanted.index(matched[0]))
                for genre in matched:
                    if genre not in selection.matched_genres:
                        selection.matched_genres.append(genre)

    chosen = [s for s in selections.values() if s.matching_book_ids]
    return sorted(chosen, key=lambda s: s.sort_key())


def _string_list(value: Any) -> list[str]:
    """A JSON array of strings, tolerating null and a bare string."""
    if isinstance(value, list):
        return [entry for entry in value if isinstance(entry, str)]
    if isinstance(value, str):
        return [value]
    return []


def count_series_asins(items: Sequence[Mapping[str, Any]], book_ids: Iterable[int]) -> int:
    """How many of these books carry a populated SeriesAsin on any series membership.

    SeriesAsin lives inside each entry of the audiobook's seriesMemberships array; there is no
    endpoint that aggregates it.
    """
    wanted = set(book_ids)
    populated = 0
    for item in items:
        if item.get("id") not in wanted:
            continue
        memberships = item.get("seriesMemberships")
        if not isinstance(memberships, list):
            continue
        if any(
            isinstance(m, dict) and isinstance(m.get("seriesAsin"), str) and m["seriesAsin"].strip()
            for m in memberships
        ):
            populated += 1
    return populated


class ListenarrApi:
    """The handful of calls this tool makes, over an injected transport."""

    def __init__(self, transport: Transport) -> None:
        self._transport = transport

    def list_library(self) -> list[dict[str, Any]]:
        """Every audiobook, with its authors, genres and series memberships.

        GET /library takes no parameters and answers with the whole library as one array; there
        is no page or offset to ask for, so there is nothing to walk. An envelope carrying the
        array under "items" is accepted as well, in case that ever changes, but no second page is
        ever requested: a tool that silently read only the first page of a paginated library
        would select from part of it and look like it had worked.
        """
        response = self._transport("GET", "/library")
        if response.status != 200:
            raise ApiError(f"GET /library answered {response.status}")
        body = response.body
        if isinstance(body, dict):
            body = body.get("items")
        if not isinstance(body, list):
            raise ApiError("GET /library did not answer with a list of audiobooks")
        return [item for item in body if isinstance(item, dict)]

    def monitored_author_id(self, name: str, region: str, language: str) -> int | None:
        """The MonitoredAuthors row id for this author name, or None if it is not monitored.

        The name is matched server side after the same normalisation the monitoring service
        applies, and there is no list-all endpoint to search instead.
        """
        query = urllib.parse.urlencode({"name": name, "region": region, "language": language})
        response = self._transport("GET", f"/authors/monitoring/status?{query}")
        if response.status == 400:
            # A 400 is a rejected request, not an answer about monitoring: the endpoint says
            # "not monitored" with a 200 and isMonitored false. Reporting it as unmonitored
            # would send the operator looking at the wrong thing.
            raise ApiError(f"the author monitoring endpoint rejected a lookup for {name!r}")
        if response.status != 200:
            raise ApiError(f"author monitoring status answered {response.status} for an author")
        body = response.dict_body()
        if not body.get("isMonitored"):
            return None
        author = body.get("monitoredAuthor")
        author_id = author.get("id") if isinstance(author, dict) else None
        return author_id if isinstance(author_id, int) else None

    def start_refresh(self, author_id: int, force: bool) -> Response:
        """Ask for a run. The caller branches on the status; this does not raise on 409 or 429."""
        return self._transport(
            "POST", "/library/refresh-metadata", {"authorId": author_id, "force": force}
        )

    def run_status(self, run_id: str) -> Response:
        """One run's progress."""
        return self._transport("GET", f"/library/refresh-metadata/{run_id}")


class SameOriginRedirect(urllib.request.HTTPRedirectHandler):
    """Refuses a redirect that would move the request to another host or port.

    The production-port refusal is checked once, against the base URL, and urllib follows
    redirects by default. Without this an instance on an allowed port could answer a read with a
    302 to port 4545 and the tool would follow it to a live instance, which is the one thing the
    guard exists to prevent. Any cross-origin redirect is refused rather than only that port,
    because a tool that talks to a host the operator did not name is wrong either way.
    """

    def __init__(self, origin: str) -> None:
        self._origin = origin

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        if target_of(newurl) != self._origin:
            raise ApiError(
                f"refusing a redirect away from {self._origin}; the instance answered "
                f"{code} pointing somewhere else"
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class UrllibTransport:
    """Real HTTP, with the cookie jar and CSRF token a write to this API needs.

    A POST is refused without an antiforgery token unless the caller authenticated with an API
    key, and the token is bound to a cookie, so both have to be carried. The token is fetched
    immediately before each POST rather than cached: there is one POST per author, so a stale
    token is a failure mode bought for nothing.
    """

    def __init__(self, base_url: str, api_key: str | None, timeout: float) -> None:
        self._base = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()),
            SameOriginRedirect(target_of(base_url)),
        )

    def __call__(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> Response:
        headers: dict[str, str] = {"Accept": "application/json"}
        if self._api_key:
            headers["X-Api-Key"] = self._api_key
        if method != "GET":
            token = self._antiforgery_token()
            if token:
                headers["X-XSRF-TOKEN"] = token
        return self._send(method, path, body, headers)

    def _antiforgery_token(self) -> str | None:
        headers = {"Accept": "application/json"}
        if self._api_key:
            headers["X-Api-Key"] = self._api_key
        response = self._send("GET", "/antiforgery/token", None, headers)
        token = response.dict_body().get("token")
        return token if isinstance(token, str) and token else None

    def _send(
        self, method: str, path: str, body: dict[str, Any] | None, headers: dict[str, str]
    ) -> Response:
        data: bytes | None = None
        if body is not None:
            data = json.dumps(body).encode()
            headers = {**headers, "Content-Type": "application/json"}

        request = urllib.request.Request(
            f"{self._base}/api/v1{path}", data=data, headers=headers, method=method
        )
        try:
            with self._opener.open(request, timeout=self._timeout) as raw:
                return _read(raw.status, raw.headers, raw.read())
        except urllib.error.HTTPError as error:
            return _read(error.code, error.headers, error.read())
        except urllib.error.URLError as error:
            raise ApiError(f"{method} {path}: {error.reason}") from error
        except TimeoutError as error:
            raise ApiError(f"{method} {path}: timed out") from error


def _read(status: int, headers: Any, payload: bytes) -> Response:
    try:
        parsed = json.loads(payload.decode()) if payload else None
    except (UnicodeDecodeError, json.JSONDecodeError):
        parsed = None
    return Response(status, {k.lower(): v for k, v in dict(headers).items()}, parsed)


@dataclass(frozen=True)
class RunOutcome:
    """How one author's refresh ended."""

    status: str
    run_id: str | None = None
    processed: int = 0
    updated: int = 0
    skipped: int = 0
    deferred: int = 0
    failed: int = 0
    requests_spent: int = 0
    detail: str = ""

    @property
    def unsettled(self) -> int:
        """Books the run reached but did not settle, so they kept their unset timestamps.

        A book the provider pushed back on, or one that threw, is counted and then left
        unstamped, exactly like the leftovers of a truncated run. The run still ends Completed.
        """
        return self.deferred + self.failed

    @property
    def finished(self) -> bool:
        """Whether every book in the author's catalogue settled, which is what lets it be recorded.

        Completed alone is not enough. It means the loop reached the end of the list, not that
        every book on the list came back with an answer, and an author recorded on Completed with
        unsettled books would never be offered again.
        """
        return self.status in TERMINAL_GOOD and self.unsettled == 0


# Outcome statuses this tool invents, for the cases where no run existed to report one.
NOT_MONITORED = "NotMonitored"
UNKNOWN_AUTHOR = "UnknownAuthor"
GAVE_UP = "GaveUp"

# A run the server no longer has. The registry is in memory and holds only the active run and
# the last one, so a run displaced by two later ones is genuinely gone. This is kept apart from
# GaveUp because the two mean opposite things about the gate: a forgotten run is not holding it
# and the author can be offered again, while a run that outlasted the deadline still is.
FORGOTTEN = "Forgotten"


class Sequencer:
    """Runs authors one at a time, waiting out whatever already holds the refresh gate.

    The server admits one refresh run at a time. A second POST while one is in flight comes back
    409 carrying that run's id and status in the same shape a success uses, so the answer to a
    collision is to wait on the run the server named rather than to poll and re-POST. There is
    also a fifteen second per-caller, per-scope cooldown in front of the coordinator, and a
    refused attempt spends it too, so the retry after a collision is itself liable to a 429. Both
    waits are the server's number, never a shorter one of ours.
    """

    def __init__(
        self,
        api: ListenarrApi,
        poll_interval: float,
        run_timeout: float,
        max_attempts: int,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], float] = time.monotonic,
        log: Callable[[str], None] = print,
    ) -> None:
        self._api = api
        self._poll_interval = poll_interval
        self._run_timeout = run_timeout
        self._max_attempts = max_attempts
        self._sleep = sleep
        self._now = now
        self._log = log

    def refresh(self, author_id: int, force: bool) -> RunOutcome:
        """Start one author's refresh and wait for it, retrying only when the server says to."""
        for attempt in range(1, self._max_attempts + 1):
            response = self._api.start_refresh(author_id, force)

            if response.status in (200, 202):
                run_id = _run_id(response)
                if run_id is None:
                    raise ApiError("the refresh was accepted without a run id")
                return self._await_run(run_id)

            if response.status == 409:
                body = response.dict_body()
                if body.get("code") == "metadata_refresh_disabled":
                    raise RefreshDisabled(
                        str(body.get("message") or "metadata refresh is turned off in settings")
                    )
                in_flight = _run_id(response)
                if in_flight is None:
                    raise ApiError(f"409 from the refresh endpoint with no run id: {body}")
                self._log(
                    f"    a {body.get('scope', 'refresh')} run is already in flight; waiting for it"
                )
                held = self._await_run(in_flight)
                if held.status == GAVE_UP:
                    # Whatever holds the gate outlasted the deadline and is therefore still
                    # holding it. Re-offering the author now collides again, and doing that
                    # max_attempts times is the difference between a long wait and a day of them.
                    return RunOutcome(
                        GAVE_UP,
                        in_flight,
                        detail=f"a run held the refresh gate: {held.detail}",
                    )
                # A floor under the retry even when the wait returned at once. The server's own
                # fifteen second cooldown makes this a 429 in practice, and a refused attempt
                # spends that cooldown too, but the client should not depend on the server to
                # keep it from spinning.
                self._sleep(self._poll_interval)
                continue

            if response.status == 429:
                wait = _retry_after(response)
                self._log(f"    rate limited, waiting {wait:.0f}s (attempt {attempt})")
                self._sleep(wait)
                continue

            if response.status == 404:
                return RunOutcome(UNKNOWN_AUTHOR, detail="the server does not know that author id")

            raise ApiError(f"the refresh endpoint answered {response.status}: {response.body}")

        return RunOutcome(GAVE_UP, detail=f"still not admitted after {self._max_attempts} attempts")

    def _await_run(self, run_id: str) -> RunOutcome:
        """Poll one run until it leaves Running, or until the run timeout if there is one."""
        deadline = self._now() + self._run_timeout if self._run_timeout > 0 else None
        while True:
            response = self._api.run_status(run_id)
            if response.status == 404:
                return RunOutcome(FORGOTTEN, run_id, detail="the server no longer has the run")
            if response.status != 200:
                raise ApiError(f"run status answered {response.status}")

            body = response.dict_body()
            status = str(body.get("status") or "")
            if status and status != RUNNING:
                return RunOutcome(
                    status,
                    run_id,
                    processed=_int(body.get("processed")),
                    updated=_int(body.get("updated")),
                    skipped=_int(body.get("skipped")),
                    deferred=_int(body.get("deferred")),
                    failed=_int(body.get("failed")),
                    requests_spent=_int(body.get("requestsSpent")),
                )

            if deadline is not None and self._now() >= deadline:
                return RunOutcome(GAVE_UP, run_id, detail="the run outlasted --run-timeout")
            self._sleep(self._poll_interval)


def _run_id(response: Response) -> str | None:
    value = response.dict_body().get("runId")
    return value if isinstance(value, str) and value else None


def _retry_after(response: Response) -> float:
    """The server's own wait, from the body or the header, never shorter than a second."""
    body_value = response.dict_body().get("retryAfterSeconds")
    if isinstance(body_value, int | float) and body_value > 0:
        return float(body_value)
    header = response.headers.get("retry-after")
    if header:
        try:
            return max(1.0, float(header))
        except ValueError:
            pass
    return 1.0


def _int(value: Any) -> int:
    return value if isinstance(value, int) else 0


class StateFile:
    """The record of which authors finished, so a second run does not redo them."""

    def __init__(self, path: Path, target: str) -> None:
        self._path = path
        self._target = target
        self._data = self._load()

    def _load(self) -> dict[str, Any]:
        """Read the file, refusing anything it cannot safely merge into.

        Every shape check here exists so a hand-edited or truncated file comes back as exit 3
        with a sentence, rather than as a traceback out of the middle of a refresh.
        """
        try:
            raw = json.loads(self._path.read_text())
        except FileNotFoundError:
            return {"version": STATE_VERSION, "targets": {}}
        except (OSError, json.JSONDecodeError) as error:
            raise ApiError(f"the state file could not be read: {error}") from error
        if not isinstance(raw, dict):
            raise ApiError("the state file does not hold an object")

        version = raw.get("version", STATE_VERSION)
        if not isinstance(version, int) or version > STATE_VERSION:
            raise ApiError(
                f"the state file is version {version!r}, which this tool does not understand. "
                "Point --state somewhere else rather than letting it be rewritten."
            )
        raw["version"] = STATE_VERSION
        if not isinstance(raw.get("targets"), dict):
            raw["targets"] = {}
        return raw

    def completed(self) -> set[int]:
        """The monitored author ids already finished against this target."""
        target = self._data["targets"].get(self._target)
        done = target.get("completed") if isinstance(target, dict) else None
        if not isinstance(done, dict):
            return set()
        recorded: set[int] = set()
        for key in done:
            try:
                recorded.add(int(key))
            except (TypeError, ValueError):
                # A key that is not an author id cannot match one, so it is ignored rather than
                # allowed to abort a run that is otherwise fine.
                continue
        return recorded

    def record(self, author_id: int, name: str, outcome: RunOutcome) -> None:
        """Write one finished author down, atomically, right after it finished."""
        targets = self._data["targets"]
        target = targets.setdefault(self._target, {})
        if not isinstance(target.get("completed"), dict):
            target["completed"] = {}
        target["completed"][str(author_id)] = {
            "name": name,
            "runId": outcome.run_id,
            "status": outcome.status,
            "updated": outcome.updated,
            "finishedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        self._write()

    def _write(self) -> None:
        """Write through a temporary file and rename, so a reader never sees a half-written one.

        The fsync is what makes the rename mean something after a power loss rather than only
        after an interrupt: without it the rename can land while the contents have not.
        """
        temporary = self._path.with_name(self._path.name + ".tmp")
        try:
            payload = json.dumps(self._data, indent=2, sort_keys=True) + "\n"
            with temporary.open("w") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self._path)
        except OSError as error:
            raise ApiError(f"the state file could not be written: {error}") from error


def target_of(base_url: str) -> str:
    """The host and port a state file is keyed by, and the origin a redirect may not leave.

    Built from the parsed host and port rather than the raw netloc, so that any credentials in
    the URL stay out of the state file on disk, and so that one instance reached with and
    without them is one key rather than two.
    """
    parsed = urllib.parse.urlsplit(base_url)
    host = (parsed.hostname or "").casefold()
    port = port_of(base_url)
    return f"{host}:{port}" if port is not None else host


def port_of(base_url: str) -> int | None:
    """The port a base URL names, filling in the scheme default when it does not name one."""
    parsed = urllib.parse.urlsplit(base_url)
    try:
        explicit = parsed.port
    except ValueError:
        return None
    if explicit is not None:
        return explicit
    return {"http": 80, "https": 443}.get(parsed.scheme.lower())


def run(
    api: ListenarrApi,
    wanted: Sequence[str],
    mode: str,
    do_refresh: bool,
    force: bool,
    region: str,
    language: str,
    state: StateFile | None,
    sequencer: Sequencer | None,
    log: Callable[[str], None] = print,
) -> int:
    """Select, report, and when asked, refresh. Returns the process exit code."""
    library = api.list_library()
    log(f"library: {len(library)} audiobooks")

    selected = select_authors(library, wanted, mode)
    if not selected:
        log(f"no author has a book matching {', '.join(wanted)} under {mode} matching")
        return EXIT_NO_MATCH

    log(f"{len(selected)} authors matched, in priority order:")
    for position, author in enumerate(selected, start=1):
        have = count_series_asins(library, author.book_ids)
        log(
            f"  {position:3d}. {author.name}  "
            f"{len(author.matching_book_ids)}/{len(author.book_ids)} books matching "
            f"[{', '.join(author.matched_genres)}]  seriesAsin {have}/{len(author.book_ids)}"
        )

    if not do_refresh:
        log("")
        log("list only. Pass --refresh to actually refresh these authors.")
        return EXIT_OK

    if state is None or sequencer is None:
        raise ValueError("refreshing needs a state file and a sequencer")
    already = state.completed()
    incomplete = 0

    log("")
    for position, author in enumerate(selected, start=1):
        head = f"[{position}/{len(selected)}] {author.name}"
        author_id = api.monitored_author_id(author.name, region, language)
        if author_id is None:
            log(f"{head}: {NOT_MONITORED}, no id to refresh, skipped")
            incomplete += 1
            continue
        if author_id in already:
            # Also catches two spellings of one author in the library, which the server resolves
            # to a single monitored id: the second spelling is the same author, already done.
            log(f"{head}: already completed, skipped")
            continue

        before = count_series_asins(library, author.book_ids)
        log(f"{head}: refreshing (author {author_id}), seriesAsin {before}/{len(author.book_ids)}")

        outcome = sequencer.refresh(author_id, force)
        library = api.list_library()
        after = count_series_asins(library, author.book_ids)

        detail = f" ({outcome.detail})" if outcome.detail else ""
        log(
            f"{head}: {outcome.status}{detail}, {outcome.processed} processed, "
            f"{outcome.updated} updated, {outcome.deferred} deferred, {outcome.failed} failed, "
            f"{outcome.requests_spent} provider requests; "
            f"seriesAsin {before} -> {after} of {len(author.book_ids)}"
        )

        if outcome.finished:
            state.record(author_id, author.name, outcome)
            already.add(author_id)
        else:
            incomplete += 1
            if outcome.unsettled:
                log(
                    f"{head}: the run reached the end of the list but {outcome.unsettled} books "
                    "did not settle, so they kept their unset timestamps"
                )
            log(f"{head}: not recorded as done, so a later run will pick it up again")

    log("")
    if incomplete:
        log(f"{incomplete} of {len(selected)} authors did not finish. Run again to continue.")
        return EXIT_REFRESH_INCOMPLETE
    log(f"all {len(selected)} authors completed.")
    return EXIT_OK


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="genre_refresh.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--base-url",
        required=True,
        metavar="URL",
        help="the instance to talk to, for example http://127.0.0.1:18901. Never defaulted.",
    )
    parser.add_argument(
        "--genre",
        action="append",
        dest="genres",
        required=True,
        metavar="GENRE",
        help="a genre to select on. Repeatable, and the order is the priority order.",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="actually refresh. Without it nothing is started and nothing is written.",
    )
    parser.add_argument(
        "--match",
        choices=[MATCH_PHRASE, MATCH_EXACT],
        default=MATCH_PHRASE,
        help="how a requested genre is compared to a stored one. See the module docstring for "
        "what each one misses. Default: phrase.",
    )
    parser.add_argument(
        "--no-force",
        dest="force",
        action="store_false",
        help="leave the staleness window in place. Every row is stamped by the upgrade backfill, "
        "so this will usually find nothing due, which is the problem this tool exists for.",
    )
    parser.add_argument("--api-key", metavar="KEY", help="sent as X-Api-Key when the instance "
                        "has one configured.")
    parser.add_argument(
        "--state",
        metavar="FILE",
        default=DEFAULT_STATE_FILE,
        help=f"where completed authors are recorded. Default: {DEFAULT_STATE_FILE}",
    )
    parser.add_argument("--region", default="us", metavar="R", help="monitored author region.")
    parser.add_argument("--language", default="all", metavar="L", help="monitored author language.")
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=5.0,
        metavar="SECONDS",
        help="how often a running refresh is asked for its progress. Default: 5.",
    )
    parser.add_argument(
        "--run-timeout",
        type=float,
        default=90000.0,
        metavar="SECONDS",
        help="how long one author's run may take before it is given up on, or 0 to follow it "
        "for as long as it runs. The default is just over the 24 hours the server's own run "
        "window allows, because a client deadline shorter than that abandons runs the server "
        "goes on to finish. Default: 90000.",
    )
    parser.add_argument(
        "--http-timeout", type=float, default=120.0, metavar="SECONDS", help="per request timeout."
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=20,
        metavar="N",
        help="how many times one author may be re-offered after a collision or a 429.",
    )
    parser.add_argument(
        "--allow-production-port",
        action="store_true",
        help=f"required before this will talk to port {PRODUCTION_PORT}, which is where a live "
        "instance listens. Nothing about this tool should reach one by accident.",
    )
    args = parser.parse_args(argv)

    port = port_of(args.base_url)
    if port is None:
        parser.error(f"--base-url {args.base_url!r} does not name a host and port")
    if port == PRODUCTION_PORT and not args.allow_production_port:
        parser.error(
            f"refusing to talk to port {PRODUCTION_PORT}: that is where a live instance listens. "
            "Pass --allow-production-port if that is genuinely what you mean."
        )
    if args.poll_interval <= 0:
        parser.error("--poll-interval must be positive; this tool does not busy-poll")
    if args.run_timeout < 0:
        parser.error("--run-timeout must be zero or positive; zero means no client deadline")
    if args.max_attempts < 1:
        parser.error("--max-attempts must be at least 1, or no author would ever be offered")
    if args.http_timeout <= 0:
        parser.error("--http-timeout must be positive")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)

    api = ListenarrApi(UrllibTransport(args.base_url, args.api_key, args.http_timeout))
    state: StateFile | None = None
    sequencer: Sequencer | None = None
    if args.refresh:
        state = StateFile(Path(args.state), target_of(args.base_url))
        sequencer = Sequencer(
            api,
            poll_interval=args.poll_interval,
            run_timeout=args.run_timeout,
            max_attempts=args.max_attempts,
        )

    return run(
        api,
        wanted=args.genres,
        mode=args.match,
        do_refresh=args.refresh,
        force=args.force,
        region=args.region,
        language=args.language,
        state=state,
        sequencer=sequencer,
    )


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RefreshDisabled as error:
        print(f"metadata refresh is turned off in settings: {error}", file=sys.stderr)
        sys.exit(EXIT_API)
    except ApiError as error:
        print(f"api: {error}", file=sys.stderr)
        sys.exit(EXIT_API)
    except KeyboardInterrupt:
        print("interrupted; completed authors are recorded and will be skipped", file=sys.stderr)
        sys.exit(EXIT_INTERRUPTED)
