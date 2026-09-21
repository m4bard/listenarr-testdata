#!/usr/bin/env python3
"""Measure the ORDER in which Audible credits a book's authors.

Audible bakes a contributor's role into the author *name* string, so a book comes back as
``["Fyodor Dostoevsky", "Constance Garnett - translator"]``. Readarr's answer to more than
one credited contributor is positional: keep ``Contributors.First()`` and drop the rest. A
positional rule is only safe if the primary author is always credited first.

This probe asks whether that holds, because the answer decides whether the rule is usable:

    author_order_probe.py corpus  corpus/corpus.json   # the committed public corpus
    author_order_probe.py sweep   --out sweep.json     # a broad catalogue-search sample
    author_order_probe.py report  sweep.json           # classify an existing capture

Exit 1 means a multi-contributor product credits a role-suffixed name at index 0, which is the
condition that makes "keep the first" drop a real author. Exit 0 means none did. Exit 2 means
the capture cannot answer the question and must not be read as either.

The classification is deliberately three-way rather than two-way. A run that found only
role-suffixed-first products would look the same whether the catalogue really is ordered
that way or the role detector simply matches everything, so the control that must come out
differently is the count of multi-contributor products where the role suffix appears only
later, plus those carrying no role suffix at all. Both are reported on every run.

A small capture is the other way to get a false all-clear, and it is the likelier one. The
committed public corpus holds three multi-contributor records and reports zero role-first
products, which reads as a green light and is not one: the wider catalogue sample finds the
case easily. So a capture with fewer than MIN_MULTI multi-contributor products refuses to
answer rather than reporting a clean result.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

# ``contributors`` alone comes back empty for some products; the catalogue only populates
# the group when a second group is requested alongside it, so ``product_desc`` rides along.
PRODUCT = ("https://api.audible.com/1.0/catalog/products/{asin}"
           "?response_groups=contributors,product_desc")
SEARCH = "https://api.audible.com/1.0/catalog/products?"
# Audnexus republishes Audible's record unchanged and answers for products the
# unauthenticated catalogue endpoint returns as a bare stub, so it backs the per-ASIN mode.
AUDNEX = "https://api.audnex.us/books/{asin}"
USER_AGENT = "listenarr-testdata/1.0"

# This vocabulary is a PORT of the production detector in
# listenarr.domain/Common/AuthorCredits.cs, and that file is authoritative. This one exists to
# measure the catalogue, which the C# cannot do, and the two disagreeing is a defect: an earlier
# version of this probe checked only the dash form while the production rule also read a trailing
# parenthetical, so a whole notation was measured as absent when it was merely unexamined. Change
# one, change the other, and say in the commit that you did.
#
# The split matches the C# one. An agent noun names a person and can only be a credit. A
# participle or an abstract noun describes an activity, and Audible uses those in brackets to
# describe the work as well ("Lewis Carroll (Illustrated)"). Production removes the role rather
# than the credit, so that ambiguity costs a shortened name and not a deleted person.
AGENT_ROLE_WORDS = (
    "translator|traducteur|traductrice|traduttore|tradutor|tradutora|traductor|traductora|"
    "ubersetzer|übersetzer|editor|editora|editeur|éditeur|illustrator|adapter|adaptateur|"
    "annotator|compiler|contributor"
)
WORK_ROLE_WORDS = (
    "translated|translation|traducao|tradução|traduccion|traducción|edited|adapted|adaptado|"
    "adaptation|illustrated|annotation|introduction|introductions|introduccion|introducción|"
    "foreword|afterword|preface|préface|prefacio|postface|avant-propos|prologue|prologo|"
    "prólogo|essay|notes"
)
ROLE_WORDS = f"(?:{AGENT_ROLE_WORDS})s?|{WORK_ROLE_WORDS}"

# Joiners, and the post-nominals Audible strands after a role ("editor Jr."). A tail of these
# alone is a name rather than a credit, which is why a role word is required as well.
FILLER = r"by|and|or|jr\.?|sr\.?|ph\.?d\.?|m\.?d\.?|series"

# Separators inside a tail are mandatory whitespace, so there is exactly one way to split one.
_TAIL = f"(?:(?:{FILLER})\\s+)*(?:{ROLE_WORDS})(?:\\s+(?:{ROLE_WORDS}|{FILLER}))*"

# The class matches a hyphen, an en dash or an em dash; Audible uses all three.
DASH_TAIL = re.compile(f"\\s[-\u2013\u2014]\\s*(?:{_TAIL})$", re.IGNORECASE)
PAREN_TAIL = re.compile(f"\\s*\\(\\s*(?:{_TAIL})\\s*\\)\\s*$", re.IGNORECASE)

# Below this many multi-contributor products a capture cannot distinguish "the catalogue always
# credits the author first" from "this sample happens not to contain the case".
MIN_MULTI = 30

# so the sample is aimed at them rather than at the catalogue at large.
SWEEP_KEYWORDS = (
    "translated by", "translator", "a new translation", "edited by", "editor",
    "foreword by", "introduction by", "afterword", "preface", "annotated",
    "illustrated by", "adapted by", "abridged", "classic translation",
    "Dostoevsky", "Homer Iliad", "Tolstoy", "Kafka", "Proust", "Dante Inferno",
    "Cervantes Don Quixote", "Flaubert", "Zola", "Chekhov", "Kierkegaard",
    "Beowulf", "Aeschylus", "Sophocles", "Euripides", "Virgil Aeneid",
    "Plato Republic", "Aristotle", "Marcus Aurelius Meditations", "Seneca",
    "Basho haiku", "Rumi", "Bhagavad Gita", "Tao Te Ching", "Confucius",
    "Grimm fairy tales", "Hans Christian Andersen", "Jules Verne",
    "Victor Hugo", "Alexandre Dumas", "Ibsen", "Strindberg", "Goethe Faust",
    "Nietzsche", "Freud", "Machiavelli Prince", "Montaigne essays",
    "One Thousand and One Nights", "Epic of Gilgamesh", "Njal's Saga",
    "Boccaccio Decameron", "Petrarch", "Ovid Metamorphoses", "Herodotus",
    "Thucydides", "Sun Tzu Art of War", "Murasaki Tale of Genji",
)


class Unreachable(Exception):
    """The catalogue could not be reached, which is not the same as an empty answer."""


def carries_role(name: str) -> bool:
    """True when the credited name has a contributor role on the end of it.

    Both notations are checked, which is the point of the port: measuring only the dash form is
    what made an earlier run report zero parenthetical credits in a catalogue that has them.
    """
    trimmed = (name or "").strip()
    if not trimmed:
        return False
    return bool(PAREN_TAIL.search(trimmed) or DASH_TAIL.search(trimmed))


def strip_role(name: str) -> str:
    """The name with its trailing role removed, or unchanged when that would leave nothing."""
    trimmed = (name or "").strip()
    stripped = PAREN_TAIL.sub("", trimmed)
    stripped = DASH_TAIL.sub("", stripped).strip().rstrip(",-\u2013\u2014( ").strip()
    return stripped or trimmed


def fetch(url: str, attempts: int = 3) -> dict[str, Any] | None:
    """Return the decoded document, None on a 404, raising when it stays unreachable."""
    last = ""
    for attempt in range(attempts):
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=40) as response:
                decoded: dict[str, Any] = json.loads(response.read().decode())
                return decoded
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            last = f"HTTP {exc.code}"
        except Exception as exc:  # any transport failure retries the same way
            last = type(exc).__name__
        if attempt < attempts - 1:
            time.sleep(2.0 * (attempt + 1))
    raise Unreachable(last)


def product_authors(asin: str) -> tuple[list[str], str] | None:
    """Credited author names for one ASIN, in order, with the source that answered.

    The unauthenticated catalogue endpoint returns a bare stub for some products rather
    than an error, which is indistinguishable from "this book has no authors" unless a
    second source is asked. Audnexus is that second source.
    """
    document = fetch(PRODUCT.format(asin=asin))
    product = (document or {}).get("product") or {}
    names = [author.get("name", "") for author in product.get("authors") or []]
    if names:
        return names, "audible"
    mirror = fetch(AUDNEX.format(asin=asin))
    if mirror is None:
        return None
    names = [author.get("name", "") for author in mirror.get("authors") or []]
    return (names, "audnex") if names else None


def sweep(keywords: tuple[str, ...], pages: int, per_page: int) -> dict[str, list[str]]:
    """Collect ASIN to ordered author names across a keyword sample of the catalogue."""
    collected: dict[str, list[str]] = {}
    for keyword in keywords:
        for page in range(1, pages + 1):
            query = urllib.parse.urlencode({
                "keywords": keyword,
                "response_groups": "contributors",
                "num_results": per_page,
                "page": page,
                "products_sort_by": "Relevance",
            })
            try:
                document = fetch(SEARCH + query)
            except Unreachable as exc:
                print(f"  ! {keyword!r} page {page}: {exc}", file=sys.stderr)
                continue
            for product in (document or {}).get("products") or []:
                asin = product.get("asin")
                names = [a.get("name", "") for a in product.get("authors") or []]
                if asin and names:
                    collected[asin] = names
            print(f"  {keyword!r} page {page}: {len(collected)} products so far", file=sys.stderr)
            time.sleep(0.3)
    return collected


def classify(captured: dict[str, list[str]]) -> dict[str, list[tuple[str, list[str]]]]:
    """Split multi-contributor products by where the role suffix sits."""
    buckets: dict[str, list[tuple[str, list[str]]]] = {
        "role_first": [], "role_later": [], "no_role": [], "all_role": [],
    }
    for asin, names in captured.items():
        if len(names) < 2:
            if names and carries_role(names[0]):
                buckets["all_role"].append((asin, names))
            continue
        flags = [carries_role(name) for name in names]
        if all(flags):
            buckets["all_role"].append((asin, names))
        if not any(flags):
            buckets["no_role"].append((asin, names))
        elif flags[0]:
            buckets["role_first"].append((asin, names))
        else:
            buckets["role_later"].append((asin, names))
    return buckets


def report(captured: dict[str, list[str]]) -> int:
    """Print the three-way classification and return the exit code."""
    buckets = classify(captured)
    multi = sum(1 for names in captured.values() if len(names) > 1)
    print(f"products captured             : {len(captured)}")
    print(f"multi-contributor products    : {multi}")
    print(f"  role suffix at index 0      : {len(buckets['role_first'])}   <- breaks 'keep first'")
    print(f"  role suffix only later      : {len(buckets['role_later'])}   <- control")
    print(f"  no dash-role detected       : {len(buckets['no_role'])}   <- control")
    print(f"every credit role-suffixed    : {len(buckets['all_role'])}   <- breaks 'drop suffixed'")

    losses = [
        (asin, names) for asin, names in buckets["role_first"]
        if any(not carries_role(name) for name in names[1:])
    ]
    if losses:
        print()
        print("keeping index 0 would keep a contributor and drop a credited author:")
        for asin, names in sorted(losses):
            dropped = [name for name in names[1:] if not carries_role(name)]
            print(f"  {asin}  keeps {names[0]!r}  drops {dropped}")
    if buckets["all_role"]:
        print()
        print("dropping every role-suffixed credit would leave these books with no author:")
        for asin, names in sorted(buckets["all_role"])[:20]:
            print(f"  {asin}  {names}")

    # Order matters. A capture with no control cannot support the finding either, because a
    # detector matching every name produces exactly that capture, so it refuses first. Size
    # only ever gates the all-clear: one role-suffixed first credit proves the case exists
    # however thin the sample, and refusing to say so would discard the one thing a small
    # capture can establish.
    if not buckets["role_later"] and not buckets["no_role"]:
        print()
        print("NO CONTROL: nothing in this capture credits a primary author first, so a role "
              "detector that matched every name would look identical to this result.",
              file=sys.stderr)
        return 2

    if buckets["role_first"]:
        return 1

    if multi < MIN_MULTI:
        print()
        print(f"CAPTURE TOO SMALL: {multi} multi-contributor products, fewer than {MIN_MULTI}. "
              "Finding none role-suffixed first says nothing at this size. Widen the sample "
              "before reading this as a clean result.", file=sys.stderr)
        return 2

    return 0


def load_asins(path: pathlib.Path) -> list[str]:
    """ASINs from a corpus.json, or from a plain JSON list of ASIN strings."""
    document = json.loads(path.read_text())
    if isinstance(document, dict) and "books" in document:
        return [book["asin"] for book in document["books"]]
    if isinstance(document, list):
        return [str(entry) for entry in document]
    raise ValueError(f"{path}: not a corpus.json and not a list of ASINs")


def run_corpus(args: argparse.Namespace) -> int:
    asins = load_asins(pathlib.Path(args.corpus))
    captured: dict[str, list[str]] = {}
    unreachable: list[str] = []
    unanswered: list[str] = []
    for index, asin in enumerate(asins, 1):
        try:
            answer = product_authors(asin)
        except Unreachable as exc:
            unreachable.append(f"{asin} ({exc})")
            answer = None
        if answer is None:
            unanswered.append(asin)
            print(f"  [{index}/{len(asins)}] {asin} no answer from either source", file=sys.stderr)
        else:
            names, source = answer
            captured[asin] = names
            print(f"  [{index}/{len(asins)}] {asin} via {source}: {names}", file=sys.stderr)
        time.sleep(0.35)
    if unanswered:
        print(f"{len(unanswered)} ASINs answered by neither source: {unanswered}", file=sys.stderr)
    if args.out:
        pathlib.Path(args.out).write_text(json.dumps(captured, indent=1, ensure_ascii=False))
    if unreachable:
        print(f"unreachable: {unreachable}", file=sys.stderr)
    return report(captured)


def run_sweep(args: argparse.Namespace) -> int:
    captured = sweep(SWEEP_KEYWORDS, args.pages, args.per_page)
    if args.out:
        pathlib.Path(args.out).write_text(json.dumps(captured, indent=1, ensure_ascii=False))
    return report(captured)


def run_report(args: argparse.Namespace) -> int:
    return report(json.loads(pathlib.Path(args.capture).read_text()))


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="mode", required=True)

    corpus = sub.add_parser("corpus", help="probe every ASIN in a corpus.json")
    corpus.add_argument("corpus")
    corpus.add_argument("--out", help="write the raw capture here")
    corpus.set_defaults(func=run_corpus)

    swp = sub.add_parser("sweep", help="probe a keyword sample of the wider catalogue")
    swp.add_argument("--out", help="write the raw capture here")
    swp.add_argument("--pages", type=int, default=2)
    swp.add_argument("--per-page", type=int, default=50)
    swp.set_defaults(func=run_sweep)

    rep = sub.add_parser("report", help="classify a capture written by an earlier run")
    rep.add_argument("capture")
    rep.set_defaults(func=run_report)

    args = parser.parse_args()
    code: int = args.func(args)
    return code


if __name__ == "__main__":
    sys.exit(main())
