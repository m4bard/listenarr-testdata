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

# An ASIN is per-marketplace. The same work carries a DIFFERENT ASIN in each region,
# and an ASIN is simply absent (404) outside its own marketplace — see REGIONAL_SEEDS.
# Anything not listed there is a US-catalogue ASIN.
DEFAULT_REGION = "us"


# Some ASINs live only in a non-US catalogue. build() looks the region up here;
# anything absent is a US-catalogue ASIN. (Distinct from REGIONAL_SEEDS, which
# ASSERTS mutual invisibility across regions -- these are just "fetch me from there".)
REGION_OVERRIDES: dict[str, str] = {
    "B0F48KS3BX": "fr", "B008WB1L70": "fr", "B008Q3A6JI": "fr", "B0DY31J772": "fr",
    "B00B4FPVR2": "de", "B00UXEBBIS": "de", "B00T9V0BU0": "de",
    "B00EOO99WS": "de", "B0DZXWPQNW": "de", "B00JQEQFL4": "de", "B00APWL9E4": "de",
    "B01IDLCAMI": "de", "B004V5UU0A": "de", "B0B1QKNWH3": "de", "B0899BQL13": "de",
    "B08SQ3S34B": "de", "B00769TAK4": "de",
}

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

    # --- series structure: position is a STRING and is not always a number -----
    # These five underpin a proven bug: decimal.TryParse silently discards a real,
    # present position, after which naming cannot tell it from "no position at all".
    ("B00CQ5WAXW", "Haggard", "She And Allan", ["series-dual", "series-order", "title-collision"]),
    ("B0F84DFZ66", "Chesterton", "Father Brown", ["series-range", "omnibus"]),
    ("B002V1PLZK", "Buchan", "Thirty-Nine Steps", ["series-range", "omnibus", "title-lies"]),
    ("B004Q1EFJQ", "Bennett", "Anna of the Five Towns", ["series-no-position"]),
    ("B077SHDLW9", "Hornung", "Amateur Cracksman", ["series-absent"]),

    # --- language / region / edition: many ASINs, one work ---------------------
    ("B0F48KS3BX", "Verne", "", ["non-english", "multi-asin"]),
    ("B008WB1L70", "Verne", "", ["non-english", "abridged", "multi-asin"]),
    ("B008Q3A6JI", "Verne", "", ["non-english", "full-cast", "multi-asin"]),
    ("B0DY31J772", "Verne", "", ["cross-region-language", "multi-asin"]),
    ("B00TPW1FLM", "", "", ["non-english", "multi-asin"]),
    ("B00TDZQG3I", "", "", ["non-english", "multi-narrator", "multi-asin"]),
    ("B01LFD0GWM", "", "", ["multi-asin"]),
    ("B01MU7YH84", "", "", ["full-cast", "multi-asin"]),
    ("B00B4FPVR2", "Grimm", "", ["non-english", "region-lock", "multi-asin"]),
    ("B00UXEBBIS", "Grimm", "", ["non-english", "multi-asin"]),
    ("B00TPKF9QQ", "Grimm", "", ["non-english", "multi-asin"]),
    ("B00T9V0BU0", "", "", ["non-english", "multi-asin"]),
    ("B00EOO99WS", "Goethe", "Faust", ["non-english", "multi-asin"]),
    ("B0DZXWPQNW", "Goethe", "Faust", ["non-english", "multi-asin"]),
    ("B00JQEQFL4", "Goethe", "Faust", ["non-english", "abridged", "multi-asin"]),
    ("B00APWL9E4", "Goethe", "Faust", ["non-english", "abridged", "multi-asin"]),
    ("B01IDLCAMI", "Goethe", "Faust", ["non-english", "abridged", "multi-asin"]),
    ("B004V5UU0A", "Goethe", "Faust", ["non-english", "multi-asin"]),
    ("B0B1QKNWH3", "Goethe", "Faust", ["non-english", "radio-play", "multi-asin"]),
    ("B0899BQL13", "Goethe", "Faust", ["non-english", "full-cast", "multi-asin"]),
    ("B08SQ3S34B", "Goethe", "Faust", ["non-english", "full-cast", "multi-asin"]),
    ("B00769TAK4", "Goethe", "Faust", ["cross-region-language", "multi-asin"]),
    ("B08527ZZZD", "Cervantes", "Quijote", ["non-english", "multi-asin"]),
    ("B07YXBJSVG", "Cervantes", "Quijote", ["non-english", "multi-narrator", "multi-asin"]),
    ("B07YP3R658", "Cervantes", "Quijote", ["non-english", "abridged", "full-cast"]),
    ("B003F6JXC2", "", "", ["non-english", "multi-asin"]),
    ("B07B7MCLB3", "", "", ["non-english", "multi-asin"]),
    ("B07RGRBKS5", "", "", ["non-english", "title-one-letter-apart"]),
    ("B00BYIJW6A", "", "", ["non-english", "abridged", "title-one-letter-apart"]),
    ("B006GDCIY6", "Tolstoy", "", ["non-english", "multi-asin"]),
    ("B08BTM5TDG", "", "", ["cyrillic", "non-english", "multi-part"]),
    ("B08BV2RNS9", "", "", ["cyrillic", "non-english", "multi-part"]),
    ("B08BTZVGS8", "", "", ["cyrillic", "non-english", "multi-part"]),
    ("B006C692NM", "Tolstoy", "", ["non-english", "multi-asin"]),

    # --- pathological metadata: dangerous to write to a filesystem -------------
    ("B08ML2HVVW", "Defoe", "", ["shell-metachars", "omnibus", "long-title"]),
    ("B003AAAU7U", "Trollope", "Forgive Her", ["question-mark"]),
    ("B06VVP98S5", "Collins", "", ["question-mark", "short-title"]),
    ("B005FGR77S", "Bierce", "Can Such Things Be", ["question-mark"]),
    ("B0B441BXY3", "Trollope", "Popenjoy", ["question-mark"]),
    ("B09SZD5QKH", "Sinclair", "", ["percent-sign", "numeric-title"]),
    ("B002UUON10", "Kipling", "Stalky", ["trailing-dot"]),
    ("B005R353GG", "Defoe", "Captain Singleton", ["long-title"]),
    ("B0DNRK5BY1", "", "Cloud of Unknowing", ["anonymous-author"]),
    ("B002V9Z9WW", "Kipling", "", ["short-title", "all-caps"]),
    ("B0CTK91XJ6", "", "", ["cyrillic", "non-latin-author", "byte-length"]),
    ("B0B5Z12CCM", "", "", ["cjk", "non-latin-author", "byte-length"]),
]


# Regional seeds: (asin, region, expect_author, expect_title, tags)
#
# These exist to prove a single point, and it is the most important one in the corpus:
# THE SAME WORK HAS A DIFFERENT ASIN IN EACH MARKETPLACE, AND EACH ASIN 404s OUTSIDE ITS
# OWN REGION. Grimm's Kinder- und Hausmärchen below is the proof — identical title,
# author, narrator and language, two ASINs, and neither is visible from the other's
# catalogue. An ASIN therefore cannot be a work identifier: it is a per-marketplace,
# per-narrator manifestation id.
#
# This is a real, live gap in Listenarr: a user's file may carry the .de ASIN while the
# record holds the .com one, and nothing reconciles them.
REGIONAL_SEEDS: list[tuple[str, str, str, str, list[str]]] = [
    ("B00B4FPO6A", "de", "Grimm", "Kinder- und Hausmärchen",
     ["region-lock", "work-key", "non-english"]),
    ("B00TPKFANI", "us", "Grimm", "Kinder- und Hausmärchen",
     ["region-lock", "work-key", "non-english"]),
]


def fetch(asin: str, region: str = DEFAULT_REGION) -> tuple[dict | None, str | None]:
    url = AUDNEX.format(asin=asin)
    if region != DEFAULT_REGION:
        url += f"?region={region}"
    req = urllib.request.Request(url, headers={"User-Agent": "listenarr-testdata/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode()), None
    except urllib.error.HTTPError as exc:
        return None, f"HTTP {exc.code}"
    except Exception as exc:
        return None, type(exc).__name__


def check_region_lock() -> tuple[list[dict], list[str]]:
    """Assert that each regional ASIN resolves ONLY in its own marketplace.

    This is an assertion, not a lookup. If a regional ASIN ever becomes visible from
    another region, the claim we make upstream — that an ASIN is per-marketplace — is
    wrong, and we want to find that out here rather than in a pull request.
    """
    proofs: list[dict] = []
    problems: list[str] = []
    regions = sorted({region for _, region, _, _, _ in REGIONAL_SEEDS})

    for asin, home, want_author, want_title, tags in REGIONAL_SEEDS:
        row: dict = {"asin": asin, "home_region": home, "tags": tags, "visibility": {}}
        for region in regions:
            data, err = fetch(asin, region)
            row["visibility"][region] = "ok" if data else (err or "unresolved")

            if region == home:
                if data is None:
                    problems.append(f"{asin}: does NOT resolve in its own region '{home}' ({err})")
                    continue
                title = data.get("title") or ""
                authors = ", ".join(a.get("name", "") for a in (data.get("authors") or []))
                if (want_author.lower() not in authors.lower()
                        or want_title.lower() not in title.lower()):
                    problems.append(
                        f"{asin} [{home}]: resolved to '{title}' by '{authors}', "
                        f"expected '{want_title}' by '{want_author}'"
                    )
                    continue
                row.update(
                    title=title,
                    authors=authors.split(", "),
                    narrators=[n.get("name", "") for n in (data.get("narrators") or [])],
                    language=data.get("language"),
                )
            elif data is not None:
                # The whole point is that it should NOT be visible here.
                problems.append(
                    f"{asin}: expected to be invisible outside '{home}', "
                    f"but it RESOLVES in '{region}' — the region-lock claim is broken"
                )
            time.sleep(0.25)

        visible = [r for r, v in row["visibility"].items() if v == "ok"]
        mark = "ok  " if visible == [home] else "BAD "
        print(f"  {mark}      {asin}  [{home}]  visible in: {visible or 'nowhere'}")
        proofs.append(row)

    return proofs, problems


def build() -> tuple[list[dict], list[str]]:
    books: list[dict] = []
    problems: list[str] = []

    for asin, want_author, want_title, tags in SEEDS:
        region = REGION_OVERRIDES.get(asin, DEFAULT_REGION)
        data, err = fetch(asin, region)
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
                "region": region,
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

    print(f"\nasserting region-lock on {len(REGIONAL_SEEDS)} regional ASINs ...", file=sys.stderr)
    proofs, region_problems = check_region_lock()
    problems += region_problems

    print(f"\n  resolved: {len(books)}/{len(SEEDS)}", file=sys.stderr)
    print(f"  region-locked as expected: {len(proofs) - len(region_problems)}/{len(proofs)}",
          file=sys.stderr)
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
    OUT.write_text(
        json.dumps({"books": books, "region_lock_proof": proofs}, indent=2, ensure_ascii=False)
        + "\n"
    )
    print(
        f"\nwrote {OUT.relative_to(ROOT)} "
        f"({len(books)} books, {len(proofs)} region-lock proofs)",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
