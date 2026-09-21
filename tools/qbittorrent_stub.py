#!/usr/bin/env python3
"""A stub qBittorrent WebUI API, enough of it for Listenarr to register and poll.

The harness has no download-client mock otherwise: every other tool here drives the
library/import side, where the input is files on disk that we generate. A queue-poll bug is
triggered by a RESPONSE, so the thing that has to be generated is the client, not the library.

Only the routes Listenarr's qBittorrent adapter actually calls are implemented:

    POST /api/v2/auth/login        cookie auth; any credentials are accepted
    GET  /api/v2/app/version       version string, used by the connection test
    GET  /api/v2/app/preferences   global seed limits, read by the item-fetch workflow
    GET  /api/v2/torrents/info     the queue itself
    GET  /api/v2/torrents/files    per-torrent file list, polled once per torrent
    POST /api/v2/torrents/add      accepts a torrent; with --conflict-on-duplicate it answers
                                   409 the second time the same info-hash is submitted, which
                                   is what qBittorrent 5.2 does for a torrent it already holds
    POST /api/v2/torrents/delete   removes torrents by info-hash, honouring `deleteFiles`
    POST /api/v2/torrents/pause    (and /stop, /resume, /start) accepted and recorded

`--journal PATH` is what makes a NEGATIVE result readable. Every request the stub receives is
appended there as one JSON object per line, flushed immediately, so afterwards you can assert
that a particular call never arrived rather than inferring it from the absence of a log line.
An absence is only evidence when something in the same journal proves the channel was open at
the time, so the add that put the torrent there is the control for the delete that did not.

A deleted info-hash stops being served from /torrents/info, which gives a second and
independent observable: the queue a client reports actually shrinks.

The point of the whole thing is `--malformed-index`. qBittorrent's `downloaded` field is
documented as an integer and normally is one, so a client that reads it with a throwing
accessor looks correct until something upstream emits it in another JSON token form. This
serves exactly one torrent that way, at a chosen position, so the effect on the torrents
AFTER it in the response can be measured.

`--malformed-kind` picks which wrong form, because they do not fail identically:

    float   a JSON number that is not an integer -> System.Text.Json raises FormatException
            ("One of the identified items was in an invalid format"). This is the shape
            reported in the field.
    string  a quoted number -> InvalidOperationException ("requires an element of type
            Number"). This is the shape Listenarrs/Listenarr#618 and #619 hit on NZBGet,
            with the accessor and the token type the other way round.

Both are unhandled by a bare JsonElement.GetInt64(), so both are worth being able to serve;
they are separated so a fix can be shown to cover both rather than just the one that was
reported.

Torrents are emitted completed-and-seeding by default so they reach the Activity queue:
Listenarr hides unmatched ACTIVE items from an external client, and only surfaces unmatched
COMPLETED ones (and only when ShowCompletedExternalDownloads is on). --state overrides this.

Writes nothing and reads nothing. Serves from memory and exits on SIGTERM.

    ./tools/qbittorrent_stub.py --port 8111 --count 6
    ./tools/qbittorrent_stub.py --port 8111 --count 6 --malformed-index 3
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote_to_bytes, urlparse

LOG = logging.getLogger("qbstub")

# qBittorrent hashes are 40 hex characters. Listenarr lowercases them when it builds a
# `hashes=` filter, so anything derived here has to survive that untouched.
HASH_LENGTH = 40


def build_torrent(index: int, state: str, progress: float) -> dict[str, Any]:
    """One well-formed torrent, with every field Listenarr asks for in its `fields=` list."""
    digest = hashlib.sha1(f"listenarr-testdata-stub-{index}".encode()).hexdigest()
    return {
        "hash": digest[:HASH_LENGTH],
        "name": f"Stub Torrent {index:03d}",
        "progress": progress,
        "size": 100_000_000 + index,
        "downloaded": 100_000_000 + index,
        "dlspeed": 0,
        "eta": 8640000,
        "state": state,
        "added_on": 1700000000 + index,
        "num_seeds": 3,
        "num_leechs": 1,
        "ratio": 1.5,
        "save_path": "/downloads/stub",
    }


def corrupt(torrent: dict[str, Any], field: str, kind: str) -> dict[str, Any]:
    """Re-emit one field in a JSON token form a throwing typed accessor cannot read.

    The value stays numerically identical in every case. Only the token type changes, so a
    difference in behaviour cannot be blamed on the magnitude of the number.
    """
    value = torrent[field]
    if kind == "float":
        torrent[field] = float(value) + 0.5
    elif kind == "string":
        torrent[field] = str(value)
    else:
        raise ValueError(f"unknown malformed kind: {kind}")
    return torrent


MAGNET_BTIH = re.compile(rb"xt=urn:btih:([0-9a-fA-F]{40}|[2-7A-Za-z]{32})")


def extract_info_hash(body: bytes) -> str | None:
    """Pull the info-hash out of an add request.

    Listenarr submits either a magnet in the `urls` field or a .torrent file part. The
    magnet carries the hash in the URI. For a file part we hash the raw bytes instead:
    the stub does not need the true BitTorrent info-hash, only a stable identifier that
    is the same for the same submission and different for a different one.
    """
    # Percent-decode first. A form-encoded magnet arrives as xt%3Durn%3Abtih%3A..., and
    # the surrounding fields differ per submission (savepath, category), so falling back
    # to hashing the whole body would make two grabs of the SAME release look different.
    # That is precisely the case this stub exists to catch.
    decoded = unquote_to_bytes(body.replace(b"+", b" "))
    match = MAGNET_BTIH.search(decoded) or MAGNET_BTIH.search(body)
    if match:
        return match.group(1).decode().lower()

    marker = b'name="torrents"'
    index = body.find(marker)
    if index != -1:
        payload = body[index + len(marker):]
        return "file:" + hashlib.sha1(payload).hexdigest()

    if not body:
        return None
    return "body:" + hashlib.sha1(body).hexdigest()


class StubState:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.lock = threading.Lock()
        self.info_requests = 0
        # Info-hashes this client has been told to hold. A real qBittorrent keeps them
        # across the session, which is exactly why a second submission of the same
        # release is rejected rather than silently re-added.
        self.added_hashes: set[str] = set()
        # Info-hashes a caller has asked the client to drop, and the calls that asked.
        # Kept separately from added_hashes because the question being measured is
        # whether the call ever arrives, which is not the same as what it removed.
        self.deleted_hashes: set[str] = set()
        self.delete_calls: list[dict[str, Any]] = []
        self.journal_path: str | None = getattr(args, "journal", None)

    def record(self, entry: dict[str, Any]) -> None:
        """Append one request to the journal, flushed, so a crash still leaves the record.

        Buffering here would be a quiet way to lose the last few requests, which are
        exactly the ones a test is about to assert on.
        """
        if not self.journal_path:
            return
        line = json.dumps(entry, sort_keys=True)
        with self.lock, open(self.journal_path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()

    def torrents_json(self) -> str:
        with self.lock:
            dropped = set(self.deleted_hashes)
        torrents = [
            build_torrent(i, self.args.state, self.args.progress)
            for i in range(1, self.args.count + 1)
        ]
        # Corrupt before filtering, so --malformed-index keeps meaning the position in the
        # generated set rather than a position that shifts as torrents are deleted.
        if self.args.malformed_index:
            position = self.args.malformed_index - 1
            corrupt(torrents[position], self.args.malformed_field, self.args.malformed_kind)
            LOG.info(
                "generated %d torrents; #%d has a %s %r",
                len(torrents),
                self.args.malformed_index,
                self.args.malformed_kind,
                self.args.malformed_field,
            )

        generated = len(torrents)
        if dropped:
            torrents = [t for t in torrents if t["hash"] not in dropped]

        if generated == len(torrents):
            LOG.info("serving %d torrents, none deleted", generated)
        else:
            LOG.info(
                "serving %d of %d torrents; %d removed by a delete call",
                len(torrents),
                generated,
                generated - len(torrents),
            )
        return json.dumps(torrents)


class Handler(BaseHTTPRequestHandler):
    state: StubState

    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        LOG.debug("%s - %s", self.address_string(), fmt % args)

    def _send(
        self, body: str, content_type: str = "application/json", cookie: bool = False
    ) -> None:
        payload = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        if cookie:
            self.send_header("Set-Cookie", "SID=stub; HttpOnly; Path=/")
        self.end_headers()
        self.wfile.write(payload)

    def _send_status(self, code: int, body: str, content_type: str = "text/plain") -> None:
        payload = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    # Removal has gone by several names across qBittorrent's API versions, and a client
    # that pauses before deleting would otherwise show up as a 404 that reads like a
    # network fault. Each is accepted and recorded so the journal shows the attempt.
    LIFECYCLE_PATHS = (
        "/api/v2/torrents/pause",
        "/api/v2/torrents/stop",
        "/api/v2/torrents/resume",
        "/api/v2/torrents/start",
    )

    def _journal(self, method: str, path: str, body: bytes) -> None:
        parsed = urlparse(self.path)
        self.state.record({
            "method": method,
            "path": path,
            "query": parsed.query,
            "body": body.decode("utf-8", "replace")[:2000],
        })

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        self._journal("POST", path, body)
        if path == "/api/v2/auth/login":
            LOG.info("login accepted")
            self._send("Ok.", "text/plain", cookie=True)
            return
        if path == "/api/v2/torrents/add":
            self._handle_add(body)
            return
        if path == "/api/v2/torrents/delete":
            self._handle_delete(body)
            return
        if path in self.LIFECYCLE_PATHS:
            action = path.rsplit("/", 1)[-1]
            LOG.info("%s received: %s", action, body.decode("utf-8", "replace")[:200])
            self._send("Ok.", "text/plain")
            return
        LOG.info("unhandled POST %s", path)
        self.send_error(404)

    def _handle_delete(self, body: bytes) -> None:
        """Drop torrents by info-hash, the way qBittorrent's /torrents/delete does.

        The call is form-encoded: `hashes` is pipe-delimited, or the literal `all`, and
        `deleteFiles` says whether the payload goes too. Both are recorded, because
        "removed from the client but left the files on disk" is a different outcome from
        "removed and cleaned up", and a test that only counted calls could not tell them
        apart.
        """
        fields = parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True)
        raw = (fields.get("hashes") or [""])[0]
        delete_files = (fields.get("deleteFiles") or ["false"])[0].lower() in ("true", "1")

        if raw.strip().lower() == "all":
            requested = {
                build_torrent(i, self.state.args.state, self.state.args.progress)["hash"]
                for i in range(1, self.state.args.count + 1)
            }
        else:
            requested = {h.strip().lower() for h in raw.split("|") if h.strip()}

        with self.state.lock:
            self.state.deleted_hashes |= requested
            self.state.delete_calls.append(
                {"hashes": sorted(requested), "deleteFiles": delete_files}
            )
            total = len(self.state.delete_calls)

        LOG.info(
            "DELETE call #%d: %d hash(es) %s, deleteFiles=%s",
            total,
            len(requested),
            sorted(requested) or "<none given>",
            delete_files,
        )

        # --refuse-delete is what separates "the caller never asked" from "the caller asked and
        # was told no". Both leave the torrent running, and an application that cannot tell them
        # apart will report a removal it did not perform. The refusal is recorded above BEFORE
        # this branch, so the journal still proves the call arrived.
        if self.state.args.refuse_delete:
            with self.state.lock:
                self.state.deleted_hashes -= requested
            LOG.info("REFUSING the delete with 500; the torrents stay in the queue")
            self._send_status(500, "Unable to delete torrents.")
            return

        self._send("Ok.", "text/plain")

    def _handle_add(self, body: bytes) -> None:
        """Accept a torrent, and optionally reject a repeat of one already held.

        The interesting case is the repeat. An application that cannot record which
        wanted items a single release satisfies will submit the same release again for
        the next item, and a client that already holds it has to say something. Answering
        409 is what qBittorrent 5.2 does.
        """
        info_hash = extract_info_hash(body)
        with self.state.lock:
            already = info_hash is not None and info_hash in self.state.added_hashes
            if not already and info_hash is not None:
                self.state.added_hashes.add(info_hash)
            held = len(self.state.added_hashes)

        if already and self.state.args.conflict_on_duplicate:
            LOG.info("REJECTING duplicate submission of %s with 409 (already held)", info_hash)
            self._send_status(409, "Torrent is already in the download list.")
            return

        if already:
            LOG.info("accepting duplicate submission of %s (409 disabled)", info_hash)
        else:
            LOG.info("accepted %s; now holding %d torrent(s)", info_hash or "<no hash found>", held)
        self._send("Ok.", "text/plain")

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        self._journal("GET", path, b"")
        if path == "/api/v2/app/version":
            self._send("v5.0.2", "text/plain")
        elif path == "/api/v2/app/webapiVersion":
            self._send("2.11.2", "text/plain")
        elif path == "/api/v2/app/preferences":
            # Seed limits off, so QbittorrentSeedLimitEvaluator has unambiguous globals and
            # nothing here depends on the host's idea of a default.
            self._send(json.dumps({
                "max_ratio_enabled": False,
                "max_ratio": -1,
                "max_seeding_time_enabled": False,
                "max_seeding_time": -1,
            }))
        elif path == "/api/v2/torrents/info":
            with self.state.lock:
                self.state.info_requests += 1
            self._send(self.state.torrents_json())
        elif path == "/api/v2/torrents/files":
            self._send("[]")
        elif path == "/api/v2/torrents/properties":
            self._send(json.dumps({"save_path": "/downloads/stub"}))
        else:
            self.send_error(404)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--conflict-on-duplicate",
        action="store_true",
        help=(
            "answer 409 when the same info-hash is submitted twice, as qBittorrent 5.2 does "
            "for a torrent it already holds. Off by default so existing callers are unaffected."
        ),
    )
    parser.add_argument("--port", type=int, default=8111, help="port to listen on")
    parser.add_argument("--count", type=int, default=6, help="how many torrents to serve")
    parser.add_argument(
        "--malformed-index",
        type=int,
        default=0,
        help="1-based position of the torrent to serve malformed; 0 serves all of them well formed",
    )
    parser.add_argument(
        "--malformed-field", default="downloaded", help="which field to serve malformed"
    )
    parser.add_argument("--malformed-kind", choices=("float", "string"), default="float")
    parser.add_argument(
        "--state",
        default="stalledUP",
        help="qBittorrent state string for every torrent (default seeds a completed torrent)",
    )
    parser.add_argument(
        "--progress", type=float, default=1.0, help="progress fraction for every torrent"
    )
    parser.add_argument(
        "--refuse-delete",
        action="store_true",
        help=(
            "answer /torrents/delete with 500 and keep the torrent. The call is still "
            "journaled, so this distinguishes a removal that was never attempted from one "
            "that was attempted and failed."
        ),
    )
    parser.add_argument(
        "--journal",
        default=None,
        help=(
            "append every request to this file as JSON lines. This is what lets a test assert "
            "that a call never arrived, with the calls that did arrive alongside it as the "
            "control that the channel was open."
        ),
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [qbstub] %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.malformed_index and not 1 <= args.malformed_index <= args.count:
        parser.error(f"--malformed-index must be between 1 and --count ({args.count})")

    Handler.state = StubState(args)
    server = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    LOG.info(
        "listening on :%d, %d torrents, malformed index %s",
        args.port,
        args.count,
        args.malformed_index or "none",
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
