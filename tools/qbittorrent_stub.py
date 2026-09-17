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

REFLECT MODE, for an automatic post-download import. The synthetic queue above is enough to
measure what a queue RESPONSE does to the poller, and no use at all for measuring an import,
because an import needs the client to point at files that are really there. Its default
`save_path` of /downloads/stub does not exist, `torrents/files` answers `[]`, and the hashes it
serves are derived from a counter rather than from what was submitted, so Listenarr can never
tie its own grab to a queue row.

`--reflect-root` switches all three:

    --reflect-root HOSTDIR       the directory on this host that holds the content
    --reflect-save-path PATH     what to report as save_path, i.e. HOSTDIR as the APPLICATION
                                 sees it (inside its container). Defaults to --reflect-root,
                                 which is only right when both sides see the same path.
    --reflect-entry NAME         the top-level entry inside HOSTDIR that is this torrent: a
                                 single file for a single-file torrent, a directory for a
                                 multi-file one.

In reflect mode `torrents/info` serves one torrent per info-hash that was actually submitted to
`torrents/add`, so the hash Listenarr grabbed is the hash it finds. `torrents/files` reports the
real files, relative to save_path exactly as qBittorrent does — a bare filename for a
single-file torrent, `entry/relative/path` for a directory — and `torrents/properties` reports
the same save_path. Sizes are read from disk, so a reported size and an on-disk size cannot
disagree by accident.

`--hold-open` keeps a read descriptor on every reflected file for the life of the stub, which is
the closest a stub gets to "still seeding": the point is to be able to say whether an import
behaves differently when the client still has the file open, rather than to assume it does not.

Writes nothing and reads nothing outside --reflect-root, which it only ever reads. Serves from
memory and exits on SIGTERM.

    ./tools/qbittorrent_stub.py --port 8111 --count 6
    ./tools/qbittorrent_stub.py --port 8111 --count 6 --malformed-index 3
    ./tools/qbittorrent_stub.py --port 8111 --reflect-root /tmp/dl \
        --reflect-save-path /downloads --reflect-entry 'Some Book.mp3'
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import pathlib
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


class ReflectedContent:
    """One torrent described by what is actually on disk under --reflect-root.

    qBittorrent reports file names RELATIVE to save_path, and the shape of that relative name
    is the difference between a single-file torrent and a directory torrent: a bare filename
    in the first case, `entry/child` in the second. Listenarr reconstructs the content path
    from save_path plus those names rather than from any field the client sends for it
    (TorrentClientPathMapper.ResolveQbittorrentContentPath), so getting the relative shape
    right is the whole job here.

    Sizes come from stat(), so the size the client reports and the size on disk are the same
    number by construction rather than by agreement.
    """

    def __init__(
        self,
        host_root: pathlib.Path,
        save_path: str,
        entry: str,
        name: str | None = None,
        hold_open: bool = False,
    ) -> None:
        self.host_root = host_root.resolve()
        self.save_path = save_path.rstrip("/") or "/"
        self.entry = entry.strip("/")
        self.name = name or self.entry
        self.target = self.host_root / self.entry
        if not self.target.exists():
            raise FileNotFoundError(
                f"--reflect-entry {self.entry!r} not found under {self.host_root}"
            )
        self.files = self._discover()
        self._held: list[int] = []
        if hold_open:
            self._hold()

    def _discover(self) -> list[tuple[str, int]]:
        """(relative-name-as-the-client-reports-it, size) for every file in the torrent."""
        if self.target.is_file():
            # A single-file torrent's one file is reported as a bare name, with no directory
            # component at all, even though it sits directly in save_path.
            return [(self.target.name, self.target.stat().st_size)]
        found = [
            (path.relative_to(self.host_root).as_posix(), path.stat().st_size)
            for path in sorted(self.target.rglob("*"))
            if path.is_file()
        ]
        if not found:
            raise FileNotFoundError(f"--reflect-entry {self.entry!r} contains no files")
        return found

    def _hold(self) -> None:
        for relative, _ in self.files:
            self._held.append(os.open(self.host_root / relative, os.O_RDONLY))
        LOG.info("holding %d read descriptor(s) open on the reflected content", len(self._held))

    @property
    def total_size(self) -> int:
        return sum(size for _, size in self.files)

    def torrent(self, info_hash: str, state: str, progress: float) -> dict[str, Any]:
        return {
            "hash": info_hash,
            "name": self.name,
            "progress": progress,
            "size": self.total_size,
            "downloaded": self.total_size,
            "dlspeed": 0,
            "eta": 8640000,
            "state": state,
            "added_on": 1700000000,
            "num_seeds": 3,
            "num_leechs": 1,
            "ratio": 1.5,
            "save_path": self.save_path,
            "content_path": f"{self.save_path}/{self.entry}",
            "category": "",
            "seeding_time": 600,
            "ratio_limit": -2,
            "seeding_time_limit": -2,
        }

    def files_json(self) -> list[dict[str, Any]]:
        return [
            {
                "index": index,
                "name": relative,
                "size": size,
                "progress": 1.0,
                "priority": 1,
                "is_seed": True,
                "piece_range": [0, 1],
                "availability": 1.0,
            }
            for index, (relative, size) in enumerate(self.files)
        ]

    def describe(self) -> str:
        listing = ", ".join(f"{relative} ({size}B)" for relative, size in self.files)
        return f"name={self.name!r} save_path={self.save_path!r} files=[{listing}]"


class StubState:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.lock = threading.Lock()
        self.info_requests = 0
        # Info-hashes this client has been told to hold. A real qBittorrent keeps them
        # across the session, which is exactly why a second submission of the same
        # release is rejected rather than silently re-added.
        self.added_hashes: set[str] = set()
        # Insertion order matters in reflect mode: the queue is served in the order the
        # hashes arrived, so a run that grabs twice is readable in the response.
        self.added_order: list[str] = []
        self.reflected: ReflectedContent | None = None

    def torrents_json(self, hashes_filter: list[str] | None = None) -> str:
        if self.reflected is not None:
            return self._reflected_torrents_json(hashes_filter)
        torrents = [
            build_torrent(i, self.args.state, self.args.progress)
            for i in range(1, self.args.count + 1)
        ]
        if self.args.malformed_index:
            position = self.args.malformed_index - 1
            corrupt(torrents[position], self.args.malformed_field, self.args.malformed_kind)
            LOG.info(
                "serving %d torrents; #%d has a %s %r",
                len(torrents),
                self.args.malformed_index,
                self.args.malformed_kind,
                self.args.malformed_field,
            )
        else:
            LOG.info("serving %d torrents, all well formed", len(torrents))
        return json.dumps(torrents)

    def _reflected_torrents_json(self, hashes_filter: list[str] | None) -> str:
        """Serve one torrent per hash that was really submitted, all describing the content.

        Serving nothing until something has been added is deliberate. If the grab never
        reached the client, the queue is empty and that is a different diagnosis from a
        queue row the importer would not touch; a stub that invented a row would erase the
        difference.
        """
        assert self.reflected is not None
        with self.lock:
            hashes = list(self.added_order)
        if hashes_filter:
            wanted = {value.lower() for value in hashes_filter}
            hashes = [value for value in hashes if value.lower() in wanted]
        torrents = [
            self.reflected.torrent(info_hash, self.args.state, self.args.progress)
            for info_hash in hashes
        ]
        LOG.info(
            "reflect: serving %d torrent(s) for %s%s",
            len(torrents),
            self.reflected.describe(),
            f" (filtered to {hashes_filter})" if hashes_filter else "",
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

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        if path == "/api/v2/auth/login":
            LOG.info("login accepted")
            self._send("Ok.", "text/plain", cookie=True)
            return
        if path == "/api/v2/torrents/add":
            self._handle_add(body)
            return
        if path == "/api/v2/torrents/setCategory":
            # Listenarr's mark-as-imported step posts here when a postImportCategory is
            # configured, and treats a non-2xx as "unable to mark the item imported", which
            # blocks the import for a reason that has nothing to do with the files. Answering
            # 200 keeps that out of the way of whatever is being measured.
            LOG.info("setCategory accepted")
            self._send("Ok.", "text/plain")
            return
        self.send_error(404)

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
                self.state.added_order.append(info_hash)
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
        parsed = urlparse(self.path)
        path = parsed.path
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
            hashes = (parse_qs(parsed.query).get("hashes") or [""])[0]
            self._send(self.state.torrents_json(
                [value for value in hashes.split("|") if value] or None
            ))
        elif path == "/api/v2/torrents/files":
            reflected = self.state.reflected
            if reflected is None:
                self._send("[]")
            else:
                self._send(json.dumps(reflected.files_json()))
        elif path == "/api/v2/torrents/properties":
            reflected = self.state.reflected
            save_path = "/downloads/stub" if reflected is None else reflected.save_path
            self._send(json.dumps({"save_path": save_path}))
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
        "--reflect-root",
        metavar="HOSTDIR",
        help=(
            "serve one torrent describing what is really on disk under HOSTDIR instead of the "
            "synthetic queue. Needed for anything that measures an IMPORT, because an import "
            "reads the files the client points at."
        ),
    )
    parser.add_argument(
        "--reflect-save-path",
        metavar="PATH",
        help=(
            "the save_path to report, i.e. --reflect-root as the application sees it. Defaults "
            "to --reflect-root, which is correct only when both sides see the same path."
        ),
    )
    parser.add_argument(
        "--reflect-entry",
        metavar="NAME",
        help=(
            "the top-level entry inside --reflect-root that is this torrent: a file for a "
            "single-file torrent, a directory for a multi-file one. Defaults to the only entry "
            "when the root holds exactly one."
        ),
    )
    parser.add_argument(
        "--reflect-name",
        metavar="TITLE",
        help="torrent name to report; defaults to --reflect-entry",
    )
    parser.add_argument(
        "--hold-open",
        action="store_true",
        help=(
            "keep a read descriptor open on every reflected file, the nearest a stub gets to "
            "a torrent still being seeded, so 'the client still has it open' can be measured "
            "rather than assumed"
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

    if args.reflect_save_path and not args.reflect_root:
        parser.error("--reflect-save-path is meaningless without --reflect-root")
    if args.reflect_entry and not args.reflect_root:
        parser.error("--reflect-entry is meaningless without --reflect-root")
    if args.hold_open and not args.reflect_root:
        parser.error("--hold-open is meaningless without --reflect-root: nothing to hold")

    state = StubState(args)
    if args.reflect_root:
        root = pathlib.Path(args.reflect_root)
        if not root.is_dir():
            parser.error(f"--reflect-root {args.reflect_root!r} is not a directory")
        entry = args.reflect_entry
        if not entry:
            candidates = sorted(child.name for child in root.iterdir())
            if len(candidates) != 1:
                parser.error(
                    f"--reflect-root holds {len(candidates)} entries, so --reflect-entry is "
                    "required to say which one is the torrent"
                )
            entry = candidates[0]
        try:
            state.reflected = ReflectedContent(
                root,
                args.reflect_save_path or str(root),
                entry,
                args.reflect_name,
                hold_open=args.hold_open,
            )
        except (FileNotFoundError, OSError) as error:
            parser.error(str(error))

    Handler.state = state
    server = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    if state.reflected is not None:
        LOG.info("listening on :%d, reflecting %s", args.port, state.reflected.describe())
    else:
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
