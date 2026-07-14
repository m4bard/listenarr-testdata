#!/usr/bin/env python3
"""Build (and re-verify) corpus.json from live Audnex metadata.

Every ASIN in SEEDS is fetched from api.audnex.us and its returned title/author is
checked against what we expect. An ASIN that does not resolve, or resolves to a
different book, is reported and excluded — it never silently enters the corpus.

This exists so that no ASIN in this repository is ever taken on trust. Re-run it
any time to confirm the corpus still reflects reality:

    python3 tools/build_corpus.py --check     # verify only, non-zero exit on drift
    python3 tools/build_corpus.py             # rewrite corpus/corpus.json
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time
import urllib.error
import urllib.request

AUDNEX = "https://api.audnex.us/books/{asin}"
ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "corpus" / "corpus.json"

# (asin, expect_author_substr, expect_title_substr, tags)
# `tags` name the failure modes this book is useful for. See cases.py.
SEEDS: list[tuple[str, str, str, list[str]]] = [
    # --- work-key proof: two ASINs, same series slot -------------------------
    ("B008DFUGCQ", "Burroughs", "Princess of Mars", ["work-key", "series"]),
    ("B071YLS9YL", "Burroughs", "Princess of Mars", ["work-key", "series"]),
    ("B01FKWL15A", "Verne", "Leagues", ["work-key", "numeral"]),
    ("B076HSP1FT", "Verne", "Leagues", ["work-key", "numeral"]),
    ("B007BR5KZA", "Baum", "Oz", ["work-key", "series"]),
    ("B002V5CJM4", "Baum", "Oz", ["work-key", "series"]),
    ("B002UZJF4U", "Dumas", "Musketeers", ["work-key", "series"]),
    ("B002V0RG8G", "Dumas", "Musketeers", ["work-key", "series"]),
    ("B00BHPI2TS", "Baum", "Marvelous Land of Oz", ["series"]),
    # --- title containment collisions (same author => author check is useless)
    ("B004YWTD30", "Haggard", "She", ["title-collision", "subtitle"]),
    ("B0096QR7Z0", "Haggard", "Allan Quatermain", ["title-collision"]),
    ("B002UZKHRE", "Haggard", "Marie", ["title-collision", "series-order"]),
    ("B00OQQTXE8", "Kipling", "Jungle Book", ["title-collision", "series"]),
    ("B01JWOHBEC", "Kipling", "Jungle Book", ["title-collision", "series"]),
    ("B002V1OVFQ", "Burroughs", "Pellucidar", ["title-collision"]),
    ("B0C6B525PQ", "Burroughs", "Pellucidar", ["title-collision"]),
    ("B002V5B7TK", "Burroughs", "Tarzan", ["title-collision", "series"]),
    ("B01GIO3GFW", "Burroughs", "Tarzan", ["title-collision", "series"]),
    ("B081B7JM9F", "Alcott", "Little Women", ["title-collision", "series"]),
    ("B003750OH4", "Alcott", "Little Men", ["title-collision", "series"]),
    ("B002V1CL4E", "MacDonald", "Princess", ["title-collision"]),
    ("B003KS7JYO", "Trollope", "Phineas", ["title-collision", "series"]),
    ("B0C6FJ6L34", "Barrie", "Peter Pan", ["title-collision", "author-variant"]),
    ("B084J9S79P", "Barrie", "Peter", ["title-collision", "author-variant", "series"]),
    ("B01BKS3DPE", "Burroughs", "Gods of Mars", ["title-collision", "series"]),
    ("B01DPXZKPI", "Burroughs", "Warlord of Mars", ["title-collision", "series"]),
    # --- subtitle / decoration: TRUE matches a fix must not break ------------
    ("B002V59S7S", "Doyle", "Study in Scarlet", ["subtitle", "series"]),
    ("B0036I51QQ", "Doyle", "Sign of Four", ["subtitle", "series"]),
    ("B0036I522E", "Doyle", "Adventures", ["series", "series-order"]),
    ("B0036HXZCO", "Doyle", "Baskervilles", ["series", "series-order"]),
    ("B002UUFXKU", "Doyle", "Valley of Fear", ["series", "series-order"]),
    ("B002VAAA6G", "Stowe", "Uncle Tom", ["subtitle"]),
    ("B002V5CW08", "Defoe", "Robinson Crusoe", ["subtitle"]),
    ("B015D78L0U", "Carroll", "Alice", ["subtitle", "pseudonym", "series"]),
    ("B0036N9OKA", "Melville", "Moby", ["subtitle", "punctuation"]),
    ("B01AGYIKG0", "Verne", "Eighty Days", ["numeral"]),
    ("B002VA3DLK", "Verne", "80 Days", ["numeral"]),
    ("B09HJHRGWQ", "Leblanc", "813", ["numeral", "punctuation", "series"]),
    ("B0GLJYD7RL", "Leblanc", "Lupin", ["subtitle", "diacritic", "series"]),
    ("B071S17YLK", "Austen", "Pride", ["baseline"]),
    ("B00D52SU5M", "Burnett", "Sara Crewe", ["title-variant"]),
    ("B002UZMQCI", "Burnett", "Little Princess", ["title-variant"]),
    # --- series structure ----------------------------------------------------
    ("B0038G2TFW", "Dumas", "Iron Mask", ["series", "series-order"]),
    ("B002V1CJIW", "Verne", "Earth to the Moon", ["series"]),
    ("B0DKK1PKN7", "Verne", "Moon", ["title-variant"]),
    ("B073JR7W68", "Montgomery", "Green Gables", ["series", "author-variant"]),
    ("B002V8L2UQ", "Montgomery", "Avonlea", ["series", "author-variant"]),
    ("B07TKCFMD1", "Lofting", "Dolittle", ["series"]),
    ("B002V8OEG0", "Lofting", "Dolittle", ["series"]),
    ("B00NB9Q736", "Hugo", "Notre", ["punctuation"]),
    # --- author-name edge cases ---------------------------------------------
    ("B007RPQWCG", "Wells", "Time Machine", ["author-initials", "author-collision"]),
    ("B07D1BVGWR", "Forster", "Room with a View", ["author-initials"]),
    ("B00LW3J8RA", "Lovecraft", "Dagon", ["author-initials", "pd-caveat"]),
    ("B01COOZ5C2", "Bront", "Jane Eyre", ["author-collision", "diacritic"]),
    ("B0186DGBCI", "Bront", "Wuthering", ["author-collision", "diacritic"]),
    ("B002V8N2QS", "Bront", "Agnes Grey", ["author-collision", "diacritic"]),
    ("B01DPV47HM", "Grimm", "Fairy Tales", ["author-collision", "multi-author"]),
    ("B01ATTZF38", "James", "Turn of the Screw", ["author-collision"]),
    ("B004FOLXEO", "James", "Antiquary", ["author-collision", "author-initials"]),
    ("B0057AOV4Y", "Twain", "Huckleberry", ["pseudonym", "series"]),
    ("B004S7ANSU", "Eliot", "Middlemarch", ["pseudonym"]),
    ("B0051PPZVI", "Munro", "Clovis", ["pseudonym"]),
    ("B002UZN8HK", "Henry", "Four Million", ["pseudonym"]),
    ("B007ZEANIS", "Henry", "Short Stories", ["pseudonym", "multi-author"]),
    ("B076PQXBV7", "Verne", "Eighty Days", ["translator", "multi-author"]),
    ("B002V9ZF3K", "Dosto", "Crime and Punishment", ["translator", "transliteration"]),
    ("B002V0PVJC", "Tolstoy", "War and Peace", ["translator"]),
    ("B00XLZ2H3E", "Zola", "Germinal", ["diacritic", "series"]),
    ("B086XLJJ33", "Capek", "R.U.R", ["diacritic", "punctuation"]),
    ("B00S710A4U", "Homer", "Odyssey", ["mononym", "translator", "multi-author"]),
    ("B09PML71M1", "Bacon", "Essays", ["common-word-author"]),
    ("B0049CGKLI", "Voltaire", "Candide", ["mononym", "pseudonym"]),
]


def fetch(asin: str) -> tuple[dict | None, str | None]:
    req = urllib.request.Request(
        AUDNEX.format(asin=asin), headers={"User-Agent": "listenarr-testdata/1.0"}
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode()), None
    except urllib.error.HTTPError as exc:
        return None, f"HTTP {exc.code}"
    except Exception as exc:  # noqa: BLE001
        return None, type(exc).__name__


def build() -> tuple[list[dict], list[str]]:
    books: list[dict] = []
    problems: list[str] = []

    for asin, want_author, want_title, tags in SEEDS:
        data, err = fetch(asin)
        if data is None:
            problems.append(f"{asin}: unresolvable ({err})")
            print(f"  DEAD      {asin}  ({err})", file=sys.stderr)
            time.sleep(0.25)
            continue

        title = data.get("title") or ""
        authors = [a.get("name", "") for a in (data.get("authors") or [])]
        narrators = [n.get("name", "") for n in (data.get("narrators") or [])]
        series = data.get("seriesPrimary") or {}
        joined = ", ".join(authors)

        if want_author.lower() not in joined.lower() or want_title.lower() not in title.lower():
            problems.append(
                f"{asin}: resolved to '{title}' by '{joined}', "
                f"expected '{want_title}' by '{want_author}'"
            )
            print(f"  MISMATCH  {asin}  {title} / {joined}", file=sys.stderr)
            time.sleep(0.25)
            continue

        books.append(
            {
                "asin": asin,
                "title": title,
                "subtitle": data.get("subtitle") or None,
                "authors": authors,
                "narrators": narrators,
                "series": series.get("name") or None,
                "series_asin": series.get("asin") or None,
                "series_position": series.get("position") or None,
                "release_date": (data.get("releaseDate") or "")[:10] or None,
                "language": data.get("language") or None,
                "tags": tags,
            }
        )
        pos = f" [{series.get('name')} #{series.get('position')}]" if series.get("name") else ""
        print(f"  ok        {asin}  {title}{pos}")
        time.sleep(0.25)

    return books, problems


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--check",
        action="store_true",
        help="verify only; exit non-zero if any ASIN drifted or died",
    )
    args = ap.parse_args()

    print(f"verifying {len(SEEDS)} ASINs against api.audnex.us ...", file=sys.stderr)
    books, problems = build()

    print(f"\n  resolved: {len(books)}/{len(SEEDS)}", file=sys.stderr)
    if problems:
        print("  PROBLEMS:", file=sys.stderr)
        for p in problems:
            print(f"    - {p}", file=sys.stderr)

    if args.check:
        if problems:
            print("\nFAIL — corpus has drifted from live metadata.", file=sys.stderr)
            return 1
        print("\nOK — every ASIN resolves and matches.", file=sys.stderr)
        return 0

    if problems:
        print(
            "\nRefusing to write a corpus containing unverified entries. "
            "Fix or drop the offending seeds.",
            file=sys.stderr,
        )
        return 1

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"books": books}, indent=2, ensure_ascii=False) + "\n")
    print(f"\nwrote {OUT.relative_to(ROOT)} ({len(books)} books)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
