#!/usr/bin/env python3
"""A stub Torznab indexer, enough of it for Listenarr to search against.

The harness generates libraries and mocks a download client, but nothing on the search
side. A whole class of behaviour only exists once an indexer answers: what the app does
with a release that satisfies more than one wanted book.

That is the case this exists for. `--box-set` serves a single release whose title names a
series rather than one book, so the same release matches every book in that series. An
application that records which wanted item a release was grabbed for, but not which items
it *satisfies*, will grab it once per book.

Routes, only the ones Listenarr's Torznab provider calls:

    GET /api?t=caps            capabilities, requested once when the indexer is tested
    GET /api?t=search&q=...    the search itself, returning an RSS channel of items

The response shape is what `TorznabNewznabSearchProvider.Parsing` reads: channel/item with
guid, title, link, category and pubDate, plus torznab:attr elements for size, seeders,
peers and magneturl.

NOTE ON WHETHER THE RELEASE SURVIVES THE FILTER. `AudiobookOnlyFilter` runs two checks in
order, and the order decides whether `--audio-evidence` matters at all.

First it returns false, meaning keep, as soon as it sees a runtime, a narrator, or a
metadata source of Audible, Audnexus or Amazon (AudiobookOnlyFilter.cs:63-66). Only if none
of those are present does it consult its print and box-set phrase list at :74. That list is
narrow, and the literal strings are what it matches:

    "Box Set", "3 Books", "3 Book", "3-Book", "Three Volume", "Three Volume Set",
    "Volume Set", "Trilogy", "Collector's Edition", "Slipcase", "Box Set:", "Box set:"

So a title like "Sherlock Holmes: The Complete Collection" matches nothing in it and is kept
with or without `--audio-evidence`. The counting entries are all hardcoded to three, so a
five-book or seven-book set does not match either. Titles that DO carry one of those phrases
need `--audio-evidence` to get past the filter, since the Torznab parser sets no runtime and
no metadata source of its own.

Pass `--audio-evidence` when you want the run to be unambiguous: it separates "the filter
dropped it" from "the app grabbed the same release twice" without standing up enrichment.
"""

from __future__ import annotations

import argparse
import hashlib
import logging
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse
from xml.sax.saxutils import escape

LOG = logging.getLogger("torznab")

CAPS = """<?xml version="1.0" encoding="UTF-8"?>
<caps>
  <server title="torznab stub"/>
  <limits max="100" default="50"/>
  <searching>
    <search available="yes" supportedParams="q"/>
    <book-search available="yes" supportedParams="q,author,title"/>
  </searching>
  <categories>
    <category id="3030" name="Audio/Audiobook"/>
  </categories>
</caps>
"""


def info_hash_for(title: str) -> str:
    """A stable fake info-hash. The same title always yields the same hash, which is the
    whole point: two grabs of one release must be indistinguishable to the client."""
    return hashlib.sha1(title.encode()).hexdigest()


def build_item(
    title: str,
    size: int,
    seeders: int,
    narrator: str | None,
    fmt: str = "m4b",
    describe: bool = False,
) -> str:
    digest = info_hash_for(title)
    magnet = f"magnet:?xt=urn:btih:{digest}&amp;dn={escape(title).replace(' ', '+')}"
    attrs = [
        f'    <torznab:attr name="size" value="{size}"/>',
        f'    <torznab:attr name="seeders" value="{seeders}"/>',
        f'    <torznab:attr name="peers" value="{seeders + 2}"/>',
        f'    <torznab:attr name="magneturl" value="{magnet}"/>',
        f'    <torznab:attr name="format" value="{escape(fmt)}"/>',
    ]
    if narrator:
        # Not a real Torznab attribute. Present so a run can distinguish "the filter
        # dropped it" from "the app grabbed it twice" without standing up enrichment.
        attrs.append(f'    <torznab:attr name="narrator" value="{escape(narrator)}"/>')
    description = f"    <description>{escape(title)}</description>\n" if describe else ""
    return f"""  <item>
    <title>{escape(title)}</title>
{description}
    <guid isPermaLink="false">{digest}</guid>
    <link>{magnet}</link>
    <category>Audio/Audiobook</category>
    <pubDate>{formatdate(usegmt=True)}</pubDate>
    <size>{size}</size>
{chr(10).join(attrs)}
  </item>"""


class Handler(BaseHTTPRequestHandler):
    args: argparse.Namespace
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *a: Any) -> None:
        LOG.debug("%s - %s", self.address_string(), fmt % a)

    def _send(self, body: str, content_type: str = "application/xml") -> None:
        payload = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        mode = (params.get("t") or [""])[0]
        query = (params.get("q") or [""])[0]

        if mode == "caps":
            LOG.info("caps requested")
            self._send(CAPS)
            return

        if mode in ("search", "book", "bookssearch"):
            items = []
            for spec in self.args.item or []:
                spec_title, _, spec_fmt = spec.partition("|")
                items.append(
                    build_item(
                        spec_title,
                        self.args.size,
                        self.args.seeders,
                        self.args.audio_evidence,
                        spec_fmt or "m4b",
                        self.args.describe,
                    )
                )
            if self.args.reverse:
                items.reverse()
            if self.args.box_set:
                # One release, named for the series rather than any single book. Every
                # book in that series matches it.
                title = self.args.box_set
                items.append(
                    build_item(
                        title,
                        self.args.size,
                        self.args.seeders,
                        self.args.audio_evidence,
                    )
                )
            if self.args.item:
                LOG.info("served order: %s", " | ".join(
                    (spec.partition("|")[0]) for spec in
                    (reversed(self.args.item) if self.args.reverse else self.args.item)))
            LOG.info(
                "search q=%r -> %d item(s)%s",
                query,
                len(items),
                f" [{self.args.box_set}]" if self.args.box_set else "",
            )
            body = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:torznab="http://torznab.com/schemas/2015/feed">
<channel>
  <title>torznab stub</title>
{chr(10).join(items)}
</channel>
</rss>
"""
            self._send(body)
            return

        LOG.info("unhandled mode %r", mode)
        self.send_error(400, "unsupported t= mode")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--port",
        type=int,
        required=True,
        help=(
            "port to listen on. REQUIRED, deliberately with no default: this used to "
            "default to 9117, which is Jackett's own port, so anyone running the stub "
            "without thinking about it collided with a real Jackett. Pick a port you "
            "have proven free."
        ),
    )
    parser.add_argument(
        "--box-set",
        metavar="TITLE",
        help=(
            "serve exactly one release with this title for every query, so several wanted "
            "books all match the same release. Example: "
            "'Sherlock Holmes: The Complete Collection'"
        ),
    )
    parser.add_argument(
        "--audio-evidence",
        metavar="NARRATOR",
        help=(
            "add a narrator attribute, so the release reads as audio rather than a print "
            "box set. Needed only for a title carrying one of AudiobookOnlyFilter's box-set "
            "phrases; see the module docstring for the list and why most titles pass without it."
        ),
    )
    parser.add_argument(
        "--item",
        action="append",
        metavar="TITLE[|FORMAT]",
        help=(
            "serve this release, repeatable, in the order given. An optional |FORMAT "
            "sets that item's torznab format attribute, which is what the response "
            "parser maps to a quality label (320, 256, 192, 128 and m4b are the tokens "
            "it recognises). Default m4b. Use several items sharing one FORMAT to make "
            "a set that should tie, and one with a different FORMAT as the control that "
            "has to rank differently."
        ),
    )
    parser.add_argument(
        "--reverse",
        action="store_true",
        help=(
            "serve --item entries in reverse order. The point of the flag: if the "
            "grabbed release changes when only the order changes, and the scores do "
            "not, then return order decided the grab rather than the score."
        ),
    )
    parser.add_argument(
        "--describe",
        action="store_true",
        help=(
            "emit a <description> equal to the title. The parser only derives a quality "
            "from title text when a description element is present, so without this the "
            "format attribute is the only thing that sets quality."
        ),
    )
    parser.add_argument("--size", type=int, default=850_000_000)
    parser.add_argument("--seeders", type=int, default=25)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [torznab] %(message)s",
        datefmt="%H:%M:%S",
    )
    Handler.args = args
    server = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    LOG.info("listening on :%d", args.port)
    if args.item:
        LOG.info(
            "%d catalogue item(s)%s; --box-set %s",
            len(args.item),
            " (reversed)" if args.reverse else "",
            "also set" if args.box_set else "not set",
        )
    elif not args.box_set:
        LOG.info("neither --item nor --box-set given; every search returns zero results")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        LOG.info("stopping")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
