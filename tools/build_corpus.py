#!/usr/bin/env python3
"""Build (and re-verify) corpus.json from live Audnex and LibriVox metadata.

Every ASIN in SEEDS is fetched from api.audnex.us and its returned title/author is
checked against what we expect. An ASIN that does not resolve, or resolves to a
different book, is reported and excluded — it never silently enters the corpus. A seed
that expects an empty author or title is refused without being fetched at all, because a
substring test against the empty string accepts anything and so verifies nothing.

Every seed also names a LibriVox project id, and that project is fetched from
librivox.org and checked the same way. LibriVox establishes provenance before it records
anything and publishes only public-domain texts, so a work it has recorded is one whose
rights analysis somebody else has already done and published. That is the closest thing
to a machine-checkable public-domain claim available to us; asserting the status in a
comment and never checking it again is not.

Note what the LibriVox pin does and does not prove. It is a claim about the WORK, not
about the particular Audible edition: the recording pinned may be in another language,
and where it is, the run says so and corpus.json records it. It says nothing about a
narrator's performance, a modern translation or an adaptation, which is why a seed whose
second credit is a modern contributor does not belong here whatever LibriVox has of the
text underneath it.

This exists so that nothing in this repository is ever taken on trust. Re-run it
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
LIBRIVOX = "https://librivox.org/api/feed/audiobooks/?id={book_id}&format=json"
LIBRIVOX_TIMEOUT = 90
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
    "B01IDLCAMI": "de", "B0B1QKNWH3": "de", "B08SQ3S34B": "de",
    "B00769TAK4": "de",
}

# LibriVox project id -> (expected author fragment, expected title fragment).
#
# The public-domain half of what this repository claims is checked here. LibriVox records
# only public-domain texts and settles the rights question before a project is opened, so a
# work with a LibriVox recording is one where the analysis has been done by someone with a
# reason to get it right. Every seed names one of these ids and the run fetches it; a seed
# whose recording does not resolve, or resolves to a different book, is excluded exactly as
# an unresolvable ASIN is.
#
# The id is the pin, not a title search. A LibriVox project id is permanent and identifies one
# recording; a title search returns whatever the catalogue contains on the day it runs, and
# "Jungle Book" alone matches three of them. The fragments are here so the id is checked too:
# a mistyped id that happens to land on another book by the same author would otherwise pass.
#
# Several ASINs share one id on purpose. The corpus holds many editions of one work, and the
# question LibriVox answers is about the work.
LIBRIVOX_RECORDINGS: dict[str, tuple[str, str]] = {
    "59": ("Twain", "Adventures of Huckleberry Finn"),
    "65": ("Homer", "Odyssey"),
    "75": ("Stowe", "Uncle Tom's Cabin"),
    "120": ("Dumas", "Three Musketeers"),
    "123": ("Hornung", "Amateur Cracksman"),
    "133": ("Brontë", "Jane Eyre"),
    "145": ("Montgomery", "Anne of Avonlea"),
    "146": ("Montgomery", "Anne of Green Gables"),
    "175": ("Burnett", "Little Princess"),
    "188": ("Kipling", "If"),
    "200": ("Carroll", "Alice's Adventures in Wonderland"),
    "205": ("Burroughs", "Princess of Mars"),
    "245": ("Burnett", "Sara Crewe"),
    "253": ("Austen", "Pride and Prejudice"),
    "314": ("Doyle", "Adventures of Sherlock Holmes"),
    "332": ("Baum", "Wonderful Wizard of Oz"),
    "355": ("Baum", "Marvelous Land of Oz"),
    "375": ("Grimm", "Grimms' Fairy Tales"),
    "382": ("Burroughs", "Gods of Mars"),
    "383": ("James", "Ghost Stories of an Antiquary"),
    "388": ("Henry", "Four Million"),
    "416": ("Lofting", "Story of Doctor Dolittle"),
    "424": ("Chesterton", "Innocence of Father Brown"),
    "431": ("James", "Turn of the Screw"),
    "490": ("Burroughs", "Warlord of Mars"),
    "527": ("Kafka", "Metamorphosis"),
    "529": ("Alighieri", "Divina Commedia"),  # Italian
    "557": ("Dostoyevsky", "Белые ночи"),  # Russian
    "594": ("Verne", "From the Earth to the Moon"),
    "608": ("Leblanc", "Extraordinary Adventures of Arsène Lupin, Gentleman-Burglar"),
    "624": ("Lofting", "Voyages of Doctor Dolittle"),
    "628": ("Alcott", "Little Women"),
    "629": ("Defoe", "Journal of the Plague Year"),
    "662": ("Kipling", "Jungle Book"),
    "665": ("Verne", "Twenty Thousand Leagues Under the Sea"),
    "696": ("Defoe", "Robinson Crusoe"),
    "714": ("Verne", "Around the World in Eighty Days"),
    "744": ("Bacon", "Essays of Francis Bacon"),
    "753": ("Melville", "Moby Dick, or the Whale"),
    "755": ("Dostoyevsky", "Crime and Punishment"),
    "761": ("Voltaire", "Candide"),
    "788": ("Eliot", "Middlemarch"),
    "816": ("Barrie", "Peter Pan"),
    "817": ("Wells", "Time Machine"),
    "830": ("Haggard", "Allan Quatermain"),
    "849": ("Alcott", "Little Men"),
    "855": ("Goethe", "Faust I"),
    "901": ("Doyle", "Hound of the Baskervilles"),
    "911": ("Brontë", "Wuthering Heights"),
    "938": ("Forster", "Room with a View"),
    "939": ("Kipling", "Stalky & Co."),
    "966": ("Doyle", "Sign of the Four"),
    "1004": ("MacDonald", "Princess and the Goblin"),
    "1078": ("Verne", "Round the Moon"),
    "1103": ("Trollope", "Can You Forgive Her?"),
    "1199": ("Burroughs", "Return of Tarzan"),
    "1303": ("Burroughs", "Tarzan of the Apes"),
    "1352": ("Haggard", "She"),
    "1462": ("Saki", "Chronicles of Clovis"),
    "1573": ("Burroughs", "Pellucidar"),
    "1679": ("Trollope", "Phineas Finn"),
    "1750": ("Brontë", "Agnes Grey"),
    "2213": ("Haggard", "She and Allan"),
    "2257": ("Lovecraft", "Collected Public Domain Works"),
    "2258": ("Buchan", "Thirty-nine Steps"),
    "2452": ("Doyle", "Study in Scarlet"),
    "2555": ("Kipling", "Second Jungle Book"),
    "2661": ("Hugo", "Hunchback of Notre Dame"),
    "2689": ("Kafka", "Verwandlung"),  # German
    "2770": ("Dumas", "Man in the Iron Mask"),
    "3008": ("Barrie", "Peter Pan in Kensington Gardens"),
    "3103": ("Verne", "Vingt mille lieues sous les mers"),  # French
    "3298": ("Bennett", "Anna of the Five Towns"),
    "3332": ("Defoe", "Captain Singleton"),
    "3664": ("Doyle", "Valley of Fear"),
    "4473": ("Goethe", "Faust, Der Tragödie zweiter Teil"),  # German
    "4998": ("Goethe", "Faust, Der Tragödie erster Teil"),  # German
    "5721": ("Barrie", "Peter and Wendy"),
    "6032": ("Trollope", "Is He Popenjoy"),
    "6061": ("Tolstoy", "Anna Karenina"),
    "6200": ("Anonymous", "Cloud of Unknowing"),
    "6281": ("Leblanc", "813"),
    "6504": ("Bierce", "Can Such Things Be?"),
    "6770": ("Akutagawa", "杜子春"),  # Japanese
    "7178": ("Tolstoy", "War and Peace"),
    "7375": ("Collins", "Miss or Mrs.?"),
    "8175": ("Sinclair", "100%: The Story of a Patriot"),
    "8883": ("Alighieri", "Divina Comedia"),  # Spanish
    "8910": ("Čapek", "R.U.R."),
    "9602": ("Haggard", "Marie"),
    "11369": ("Grimm", "Kinder- und Hausmärchen"),  # German
    "12810": ("Zola", "Germinal"),
    "20083": ("Burroughs", "Tanar of Pellucidar"),
    "20198": ("Cervantes", "Don Quijote de la Mancha"),  # Spanish
}

# (asin, expect_author_substr, expect_title_substr, tags, librivox_id)
# `tags` name the failure modes this book is useful for. See cases.py.
#
# Both expectations are matched as substrings of what the API returns, so NEITHER MAY BE
# EMPTY: `"" in anything` is True, and a seed expecting nothing accepts whatever comes back
# under that ASIN, including a different book. check_fragment() refuses such a seed before
# it is fetched. Where a work has no Latin title at all the fragment is its native title;
# that is weaker than a chosen fragment, because a short title and its fragment coincide,
# and still enormously stronger than expecting nothing.
SEEDS: list[tuple[str, str, str, list[str], str]] = [
    # --- work-key proof: two ASINs, same series slot -------------------------
    ("B008DFUGCQ", "Burroughs", "Princess of Mars", ["work-key", "series"], "205"),
    ("B071YLS9YL", "Burroughs", "Princess of Mars", ["work-key", "series"], "205"),
    ("B01FKWL15A", "Verne", "Twenty Thousand Leagues", ["work-key", "numeral"], "665"),
    ("B076HSP1FT", "Verne", "20,000 Leagues", ["work-key", "numeral"], "665"),
    ("B007BR5KZA", "Baum", "Wonderful Wizard", ["work-key", "series"], "332"),
    ("B002V5CJM4", "Baum", "Wonderful Wizard", ["work-key", "series"], "332"),
    ("B002UZJF4U", "Dumas", "Musketeers", ["work-key", "series"], "120"),
    ("B002V0RG8G", "Dumas", "Musketeers", ["work-key", "series"], "120"),
    ("B00BHPI2TS", "Baum", "Marvelous Land of Oz", ["series"], "355"),
    # --- title containment collisions (same author => author check is useless)
    ("B004YWTD30", "Haggard", "History of Adventure", ["title-collision", "subtitle"], "1352"),
    ("B0096QR7Z0", "Haggard", "Allan Quatermain", ["title-collision"], "830"),
    ("B002UZKHRE", "Haggard", "Marie", ["title-collision", "series-order"], "9602"),
    ("B00OQQTXE8", "Kipling", "The Jungle Book", ["title-collision", "series"], "662"),
    ("B01JWOHBEC", "Kipling", "Second Jungle Book", ["title-collision", "series"], "2555"),
    ("B002V1OVFQ", "Burroughs", "Pellucidar", ["title-collision"], "1573"),
    ("B0C6B525PQ", "Burroughs", "Tanar", ["title-collision"], "20083"),
    ("B002V5B7TK", "Burroughs", "Tarzan of the Apes", ["title-collision", "series"], "1303"),
    ("B01GIO3GFW", "Burroughs", "Return of Tarzan", ["title-collision", "series"], "1199"),
    ("B081B7JM9F", "Alcott", "Little Women", ["title-collision", "series"], "628"),
    ("B003750OH4", "Alcott", "Little Men", ["title-collision", "series"], "849"),
    ("B002V1CL4E", "MacDonald", "Princess", ["title-collision"], "1004"),
    ("B003KS7JYO", "Trollope", "Phineas", ["title-collision", "series"], "1679"),
    # --- Barrie: four credited spellings of one author -----------------------
    #
    # PUBLIC-DOMAIN NOTE, applying to all four Barrie seeds below. The texts are public
    # domain: Barrie died in 1937, and Peter Pan in Kensington Gardens (1906) and Peter and
    # Wendy (1911) are long out of copyright. Separately, Great Ormond Street Hospital holds
    # a perpetual entitlement to royalties on certain UK uses of Peter Pan under Schedule 6
    # of the Copyright, Designs and Patents Act 1988. This repository stores metadata and
    # generates one second of synthesized silence, so nothing here engages that entitlement.
    # Recorded so a reader who knows about Schedule 6 can see that we did too.
    #
    # Three of the four spellings differ from each other ONLY in punctuation, which is the
    # class a normalising fix collapses:
    #     B078X1NX28  'J. M. Barrie'      B084J9S79P  'J.M. Barrie'
    #     B002V1M36U  'J M Barrie'
    # The first two are the same work, Peter and Wendy, under two ASINs, so between those two
    # nothing whatsoever differs except a space and the position of two dots. The fourth,
    # B0C6FJ6L34 'James M. Barrie', is abbreviation drift, which no punctuation rule collapses
    # and which is kept precisely as the case that shows where such a fix stops.
    ("B0C6FJ6L34", "Barrie", "Peter Pan", ["title-collision", "author-variant"], "3008"),
    ("B084J9S79P", "Barrie", "Peter and Wendy",
     ["title-collision", "author-variant", "author-punctuation", "series"], "5721"),
    ("B078X1NX28", "Barrie", "Peter and Wendy",
     ["title-collision", "author-variant", "author-punctuation"], "5721"),
    ("B002V1M36U", "Barrie", "Peter Pan",
     ["title-collision", "author-variant", "author-punctuation", "full-cast"], "816"),
    ("B01BKS3DPE", "Burroughs", "Gods of Mars", ["title-collision", "series"], "382"),
    ("B01DPXZKPI", "Burroughs", "Warlord of Mars", ["title-collision", "series"], "490"),
    # --- subtitle / decoration: TRUE matches a fix must not break ------------
    ("B002V59S7S", "Doyle", "Study in Scarlet", ["subtitle", "series"], "2452"),
    ("B0036I51QQ", "Doyle", "Sign of Four", ["subtitle", "series"], "966"),
    ("B0036I522E", "Doyle", "Adventures", ["series", "series-order"], "314"),
    ("B0036HXZCO", "Doyle", "Baskervilles", ["series", "series-order"], "901"),
    ("B002UUFXKU", "Doyle", "Valley of Fear", ["series", "series-order"], "3664"),
    ("B002VAAA6G", "Stowe", "Uncle Tom", ["subtitle"], "75"),
    ("B002V5CW08", "Defoe", "Robinson Crusoe", ["subtitle"], "696"),
    ("B015D78L0U", "Carroll", "Alice", ["subtitle", "pseudonym", "series"], "200"),
    ("B0036N9OKA", "Melville", "Moby", ["subtitle", "punctuation"], "753"),
    ("B01AGYIKG0", "Verne", "Eighty Days", ["numeral"], "714"),
    ("B002VA3DLK", "Verne", "80 Days", ["numeral"], "714"),
    ("B09HJHRGWQ", "Leblanc", "813", ["numeral", "punctuation", "series"], "6281"),
    ("B0GLJYD7RL", "Leblanc", "Lupin", ["subtitle", "diacritic", "series"], "608"),
    ("B071S17YLK", "Austen", "Pride", ["baseline"], "253"),
    ("B00D52SU5M", "Burnett", "Sara Crewe", ["title-variant"], "245"),
    ("B002UZMQCI", "Burnett", "Little Princess", ["title-variant"], "175"),
    # --- series structure ----------------------------------------------------
    ("B0038G2TFW", "Dumas", "Iron Mask", ["series", "series-order"], "2770"),
    ("B002V1CJIW", "Verne", "Earth to the Moon", ["series"], "594"),
    ("B0DKK1PKN7", "Verne", "Around the Moon", ["title-variant"], "1078"),
    # Both spelling-critical: "L. M. Montgomery" against "Lucy Maud Montgomery".
    ("B073JR7W68", "Montgomery", "Green Gables", ["series", "author-variant"], "146"),
    ("B002V8L2UQ", "Montgomery", "Avonlea", ["series", "author-variant"], "145"),
    ("B07TKCFMD1", "Lofting", "Story of Doctor Dolittle", ["series"], "416"),
    ("B002V8OEG0", "Lofting", "Voyages of Doctor Dolittle", ["series"], "624"),
    ("B00NB9Q736", "Hugo", "Notre", ["punctuation"], "2661"),
    # --- author-name edge cases ---------------------------------------------
    ("B007RPQWCG", "Wells", "Time Machine", ["author-initials", "author-collision"], "817"),
    ("B07D1BVGWR", "Forster", "Room with a View", ["author-initials"], "938"),
    ("B00LW3J8RA", "Lovecraft", "Dagon", ["author-initials", "pd-caveat"], "2257"),
    ("B01COOZ5C2", "Bront", "Jane Eyre", ["author-collision", "diacritic"], "133"),
    ("B0186DGBCI", "Bront", "Wuthering", ["author-collision", "diacritic"], "911"),
    ("B002V8N2QS", "Bront", "Agnes Grey", ["author-collision", "diacritic"], "1750"),
    # Spelling-critical: the only record crediting "Brothers Grimm" rather than "Brüder".
    ("B01DPV47HM", "Grimm", "Fairy Tales", ["author-collision", "multi-author"], "375"),
    ("B01ATTZF38", "James", "Turn of the Screw", ["author-collision"], "431"),
    ("B004FOLXEO", "James", "Antiquary", ["author-collision", "author-initials"], "383"),
    ("B0057AOV4Y", "Twain", "Huckleberry", ["pseudonym", "series"], "59"),
    ("B004S7ANSU", "Eliot", "Middlemarch", ["pseudonym"], "788"),
    ("B0051PPZVI", "Munro", "Clovis", ["pseudonym"], "1462"),
    ("B002UZN8HK", "Henry", "Four Million", ["pseudonym"], "388"),
    ("B007ZEANIS", "Henry", "Short Stories", ["pseudonym", "multi-author"], "388"),
    ("B076PQXBV7", "Verne", "Eighty Days", ["translator", "multi-author"], "714"),
    ("B002V9ZF3K", "Dosto", "Crime and Punishment", ["translator", "transliteration"], "755"),
    ("B002V0PVJC", "Tolstoy", "War and Peace", ["translator"], "7178"),
    ("B00XLZ2H3E", "Zola", "Germinal", ["diacritic", "series"], "12810"),
    ("B086XLJJ33", "Capek", "R.U.R", ["diacritic", "punctuation"], "8910"),
    ("B00S710A4U", "Homer", "Odyssey", ["mononym", "translator", "multi-author"], "65"),
    ("B09PML71M1", "Bacon", "Essays", ["common-word-author"], "744"),
    ("B0049CGKLI", "Voltaire", "Candide", ["mononym", "pseudonym"], "761"),

    # --- series structure: position is a STRING and is not always a number -----
    # These five underpin a proven bug: decimal.TryParse silently discards a real,
    # present position, after which naming cannot tell it from "no position at all".
    ("B00CQ5WAXW", "Haggard", "She And Allan",
     ["series-dual", "series-order", "title-collision"], "2213"),
    ("B0F84DFZ66", "Chesterton", "Father Brown", ["series-range", "omnibus"], "424"),
    ("B002V1PLZK", "Buchan", "Thirty-Nine Steps",
     ["series-range", "omnibus", "title-lies"], "2258"),
    ("B004Q1EFJQ", "Bennett", "Anna of the Five Towns", ["series-no-position"], "3298"),
    ("B077SHDLW9", "Hornung", "Amateur Cracksman", ["series-absent"], "123"),

    # --- language / region / edition: many ASINs, one work ---------------------
    ("B0F48KS3BX", "Verne", "Vingt mille lieues", ["non-english", "multi-asin"], "3103"),
    ("B008WB1L70", "Verne", "Vingt mille lieues",
     ["non-english", "abridged", "multi-asin"], "3103"),
    ("B008Q3A6JI", "Verne", "Vingt mille lieues",
     ["non-english", "full-cast", "multi-asin"], "3103"),
    ("B0DY31J772", "Verne", "Vingt mille lieues", ["cross-region-language", "multi-asin"], "3103"),
    ("B00TPW1FLM", "Kafka", "Verwandlung", ["non-english", "multi-asin"], "2689"),
    ("B00TDZQG3I", "Kafka", "Verwandlung", ["non-english", "multi-narrator", "multi-asin"], "2689"),
    ("B01LFD0GWM", "Kafka", "The Metamorphosis", ["multi-asin"], "527"),
    ("B01MU7YH84", "Kafka", "Metamorphosis", ["full-cast", "multi-asin"], "527"),
    ("B00B4FPVR2", "Grimm", "schönsten Kinder",
     ["non-english", "region-lock", "multi-asin"], "11369"),
    ("B00UXEBBIS", "Grimm", "Sämtliche", ["non-english", "multi-asin"], "11369"),
    ("B00TPKF9QQ", "Grimm", "Dornröschen", ["non-english", "multi-asin"], "11369"),
    ("B00T9V0BU0", "Grimm", "schönsten Märchen", ["non-english", "multi-asin"], "11369"),
    ("B00EOO99WS", "Goethe", "Faust", ["non-english", "multi-asin"], "4998"),
    ("B0DZXWPQNW", "Goethe", "komplette Hörbuch", ["non-english", "multi-asin"], "4998"),
    ("B00JQEQFL4", "Goethe", "Faust", ["non-english", "abridged", "multi-asin"], "4998"),
    # Spelling-critical: the only Goethe record that drops the "von".
    ("B00APWL9E4", "Goethe", "Faust", ["non-english", "abridged", "multi-asin"], "4998"),
    ("B01IDLCAMI", "Goethe", "Faust I + II", ["non-english", "abridged", "multi-asin"], "4998"),
    ("B0B1QKNWH3", "Goethe", "Tragödie Erster Teil",
     ["non-english", "radio-play", "multi-asin"], "4998"),
    ("B08SQ3S34B", "Goethe", "Faust 2", ["non-english", "full-cast", "multi-asin"], "4473"),
    ("B00769TAK4", "Goethe", "Faust", ["cross-region-language", "multi-asin"], "855"),
    ("B08527ZZZD", "Cervantes", "Quijote", ["non-english", "multi-asin"], "20198"),
    ("B07YXBJSVG", "Cervantes", "Quijote",
     ["non-english", "multi-narrator", "multi-asin"], "20198"),
    ("B07YP3R658", "Cervantes", "Quijote", ["non-english", "abridged", "full-cast"], "20198"),
    ("B003F6JXC2", "Dante", "Divina Commedia", ["non-english", "multi-asin"], "529"),
    ("B07RGRBKS5", "Dante", "Divina Comedia", ["non-english", "title-one-letter-apart"], "8883"),
    ("B00BYIJW6A", "Dante", "Divina Comedia",
     ["non-english", "abridged", "title-one-letter-apart"], "8883"),
    ("B006GDCIY6", "Tolstoy", "War and Peace", ["non-english", "multi-asin"], "7178"),
    ("B08BTM5TDG", "Толстой", "Война и мир 1", ["cyrillic", "non-english", "multi-part"], "7178"),
    ("B08BV2RNS9", "Толстой", "Война и мир 2", ["cyrillic", "non-english", "multi-part"], "7178"),
    ("B08BTZVGS8", "Толстой", "Война и мир 3", ["cyrillic", "non-english", "multi-part"], "7178"),
    ("B006C692NM", "Tolstoy", "Anna Karenina", ["non-english", "multi-asin"], "6061"),

    # --- pathological metadata: dangerous to write to a filesystem -------------
    ("B08ML2HVVW", "Defoe", "Plague Year", ["shell-metachars", "omnibus", "long-title"], "629"),
    ("B003AAAU7U", "Trollope", "Forgive Her", ["question-mark"], "1103"),
    ("B06VVP98S5", "Collins", "Miss or Mrs", ["question-mark", "short-title"], "7375"),
    ("B005FGR77S", "Bierce", "Can Such Things Be", ["question-mark"], "6504"),
    ("B0B441BXY3", "Trollope", "Popenjoy", ["question-mark"], "6032"),
    ("B09SZD5QKH", "Sinclair", "100%", ["percent-sign", "numeric-title"], "8175"),
    ("B002UUON10", "Kipling", "Stalky", ["trailing-dot"], "939"),
    ("B005R353GG", "Defoe", "Captain Singleton", ["long-title"], "3332"),
    ("B0DNRK5BY1", "Anonymous", "Cloud of Unknowing", ["anonymous-author"], "6200"),
    ("B002V9Z9WW", "Kipling", "IF", ["short-title", "all-caps"], "188"),
    ("B0CTK91XJ6", "Достоевский", "Белые ночи",
     ["cyrillic", "non-latin-author", "byte-length"], "557"),
    ("B0B5Z12CCM", "芥川", "杜子春", ["cjk", "non-latin-author", "byte-length"], "6770"),
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
REGIONAL_SEEDS: list[tuple[str, str, str, str, list[str], str]] = [
    ("B00B4FPO6A", "de", "Grimm", "Kinder- und Hausmärchen",
     ["region-lock", "work-key", "non-english"], "11369"),
    ("B00TPKFANI", "us", "Grimm", "Kinder- und Hausmärchen",
     ["region-lock", "work-key", "non-english"], "11369"),
]


# ASIN -> (the exact credited author string, why THIS seed depends on it)
#
# A seed declares itself spelling-critical by appearing here, and each of the seeds below
# carries a comment at its own line saying so, because a declaration you can only find by
# reading a table three hundred lines away is one an editor will not know they broke.
#
# The author check on an ordinary seed is a substring: "Barrie" is enough, because what such a
# seed asserts is which work the ASIN is, and a publisher retitling the credit from
# "H. G. Wells" to "H.G. Wells" changes nothing it claims. Matching exactly everywhere would
# fail the build on harmless edits and teach people to loosen the check.
#
# For the seeds below the exact string IS the claim. Each is the SOLE record in the corpus
# carrying its spelling, so a publisher edit to any one of them destroys an author-drift pair
# outright, and the substring check would keep passing while it happened. That is the failure
# this table exists to make loud.
#
# Sole carrier is the rule, and it is why the other side of two of these pairs is absent:
# 'Johann Wolfgang von Goethe' is credited on seven records and 'Brüder Grimm' on four, so
# those pairs survive an edit to any single one of them and pinning one record would assert
# more than is true.
SPELLING_CRITICAL: dict[str, tuple[str, str]] = {
    "B078X1NX28": (
        "J. M. Barrie",
        "with B084J9S79P ('J.M. Barrie') it is the corpus's only pair of records that are the "
        "same work and differ ONLY in author punctuation",
    ),
    "B084J9S79P": (
        "J.M. Barrie",
        "with B078X1NX28 ('J. M. Barrie') it is the corpus's only pair of records that are the "
        "same work and differ ONLY in author punctuation",
    ),
    "B002V1M36U": (
        "J M Barrie",
        "the only record carrying the unpunctuated initials, which is the third of the three "
        "spellings a punctuation rule has to fold together",
    ),
    "B0C6FJ6L34": (
        "James M. Barrie",
        "the abbreviation case that punctuation folding does NOT collapse, kept to show where "
        "such a fix stops",
    ),
    "B073JR7W68": (
        "L. M. Montgomery",
        "with B002V8L2UQ ('Lucy Maud Montgomery') it is one of the corpus's four author-drift "
        "pairs",
    ),
    "B002V8L2UQ": (
        "Lucy Maud Montgomery",
        "with B073JR7W68 ('L. M. Montgomery') it is one of the corpus's four author-drift pairs",
    ),
    "B00APWL9E4": (
        "Johann Wolfgang Goethe",
        "the only record that drops the nobiliary particle; every other Goethe record credits "
        "'Johann Wolfgang von Goethe', so this one alone carries the particle-drift pair",
    ),
    "B01DPV47HM": (
        "Brothers Grimm",
        "the only record crediting the translated form; every other Grimm record credits "
        "'Brüder Grimm', so this one alone carries the translated-name pair",
    ),
}


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


def check_fragment(asin: str, want_author: str, want_title: str) -> str | None:
    """Reject an expectation that cannot fail, returning why, or None if the seed is usable.

    Acceptance is a substring test, and every string contains the empty string. A seed whose
    expected author or title is blank therefore agrees with any answer the API gives, so the
    ASIN behind it has never actually been verified even though the run reports it as ok.
    """
    blank = [f"expected {name} is empty"
             for name, value in (("author", want_author), ("title", want_title))
             if not value.strip()]
    if not blank:
        return None
    return (f"{asin}: {' and '.join(blank)}, which matches every book; "
            "give it a fragment that names the work")


def check_spelling(asin: str, authors: list[str]) -> str | None:
    """Refuse a spelling-critical seed whose credited author string has changed at all.

    Returns why it was refused, or None when the seed is not spelling-critical or still
    carries the string it was chosen for. The substring check every seed gets asks "is this
    the right work"; this asks "is this still the right SPELLING", which for these seeds is
    the only reason they are in the corpus.
    """
    expected = SPELLING_CRITICAL.get(asin)
    if expected is None:
        return None
    want, because = expected
    if want in authors:
        return None
    return (
        f"{asin}: credited author is now {authors!r}, and this seed requires exactly {want!r}. "
        f"It is spelling-critical because {because}. "
        "Do NOT relax this check to make the build pass: without that exact string the case "
        "it encodes is gone from the corpus and nothing here reproduces it any more. Find "
        "another ASIN still carrying the spelling, or delete the seed and say in the commit "
        "message which case the corpus no longer covers."
    )


def fetch_librivox(book_id: str, attempts: int = 3) -> tuple[dict | None, str | None]:
    """Fetch one LibriVox project by id, returning the project record or a reason it failed.

    LibriVox answers 404 with a JSON body when an id does not exist, so an absent project and
    an unreachable server both arrive here as an error string rather than as an empty record.

    A 404 is an answer and is returned at once. Anything else — a gateway timeout, a dropped
    connection — is retried, because the site is small and goes away for a few seconds at a
    time, and a build that refuses the whole corpus over one blip teaches people to rerun it
    until it passes, which is the habit this check exists to prevent.
    """
    url = LIBRIVOX.format(book_id=book_id)
    req = urllib.request.Request(url, headers={"User-Agent": "listenarr-testdata/1.0"})
    error = "not attempted"
    for attempt in range(attempts):
        try:
            # Generous, because the site is often merely slow: measured at six to nineteen
            # seconds under load. A timeout short enough to trip on that turns one slow answer
            # into three requests, which is the opposite of being easy on it.
            with urllib.request.urlopen(req, timeout=LIBRIVOX_TIMEOUT) as resp:
                payload = json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None, "HTTP 404"
            error = f"HTTP {exc.code}"
        except Exception as exc:
            error = type(exc).__name__
        else:
            books = payload.get("books") or []
            if not books:
                return None, "no project in response"
            return books[0], None
        time.sleep(2 * (attempt + 1))
    return None, error


def verify_librivox() -> tuple[dict[str, dict], list[str]]:
    """Resolve every recording in LIBRIVOX_RECORDINGS and confirm it is the one we named.

    One request per distinct project, not per seed: several ASINs are editions of one work and
    share a pin. Requests are spaced out because LibriVox is run by volunteers and this is the
    only thing here that reads from them.
    """
    verified: dict[str, dict] = {}
    problems: list[str] = []

    for book_id, (want_author, want_title) in LIBRIVOX_RECORDINGS.items():
        unusable = check_fragment(f"librivox {book_id}", want_author, want_title)
        if unusable is not None:
            problems.append(unusable)
            print(f"  NOCHECK   lv:{book_id}  (nothing expected of it)", file=sys.stderr)
            continue

        data, err = fetch_librivox(book_id)
        time.sleep(1.0)
        if data is None:
            problems.append(f"librivox {book_id}: unresolvable ({err})")
            print(f"  DEAD      lv:{book_id}  ({err})", file=sys.stderr)
            continue

        title = data.get("title") or ""
        authors = [
            " ".join(part for part in (a.get("first_name"), a.get("last_name")) if part)
            for a in (data.get("authors") or [])
        ]
        joined = ", ".join(authors)
        if want_author.lower() not in joined.lower() or want_title.lower() not in title.lower():
            problems.append(
                f"librivox {book_id}: resolved to '{title}' by '{joined}', "
                f"expected '{want_title}' by '{want_author}'"
            )
            print(f"  MISMATCH  lv:{book_id}  {title} / {joined}", file=sys.stderr)
            continue

        verified[book_id] = {
            "id": book_id,
            "title": title,
            "authors": authors,
            "language": data.get("language") or "",
            "url": data.get("url_librivox") or "",
        }
        print(f"  ok        lv:{book_id}  {title}", flush=True)

    return verified, problems


def check_librivox_table() -> list[str]:
    """Refuse a seed with no LibriVox pin, and a pin no seed uses.

    Two tables keyed to each other drift the moment one is edited alone, and the failure is
    silent in both directions: a seed with no pin would go unchecked, and a stale recording
    row would keep passing long after the book it belonged to had gone.
    """
    pinned = [seed[4] for seed in SEEDS] + [seed[5] for seed in REGIONAL_SEEDS]
    problems = [
        f"{seed[0]}: names librivox id {seed[4]}, which is not in LIBRIVOX_RECORDINGS"
        for seed in SEEDS
        if seed[4] not in LIBRIVOX_RECORDINGS
    ]
    problems += [
        f"{seed[0]}: names librivox id {seed[5]}, which is not in LIBRIVOX_RECORDINGS"
        for seed in REGIONAL_SEEDS
        if seed[5] not in LIBRIVOX_RECORDINGS
    ]
    problems += [
        f"librivox {book_id}: in LIBRIVOX_RECORDINGS but no seed names it"
        for book_id in LIBRIVOX_RECORDINGS
        if book_id not in set(pinned)
    ]
    return problems


def check_spelling_table() -> list[str]:
    """Refuse a spelling-critical entry naming an ASIN no seed uses.

    check_spelling() only ever runs for ASINs that reach build(), so an entry for a seed that
    has since been removed would sit here asserting nothing while looking like protection.
    """
    seeded = {seed[0] for seed in SEEDS}
    return [
        f"{asin}: in SPELLING_CRITICAL but no seed uses it, so its spelling is never checked"
        for asin in SPELLING_CRITICAL
        if asin not in seeded
    ]


def librivox_fields(record: dict, language: str | None) -> dict:
    """The LibriVox columns a corpus entry carries, including whether the languages agree.

    `same_language` is derived from the two live answers rather than asserted anywhere, and it
    is the honest part of the claim: where it is False the work is established as public
    domain but no free recording of THIS edition's language has been shown to exist.
    """
    lv_language = record["language"]
    return {
        "librivox_id": record["id"],
        "librivox_title": record["title"],
        "librivox_url": record["url"],
        "librivox_language": lv_language,
        "librivox_same_language": language is not None
        and language.lower() == lv_language.lower(),
    }




def check_region_lock(librivox: dict[str, dict]) -> tuple[list[dict], list[str]]:
    """Assert that each regional ASIN resolves ONLY in its own marketplace.

    This is an assertion, not a lookup. If a regional ASIN ever becomes visible from
    another region, the claim we make upstream — that an ASIN is per-marketplace — is
    wrong, and we want to find that out here rather than in a pull request.
    """
    proofs: list[dict] = []
    problems: list[str] = []
    regions = sorted({region for _, region, _, _, _, _ in REGIONAL_SEEDS})

    for asin, home, want_author, want_title, tags, book_id in REGIONAL_SEEDS:
        unusable = check_fragment(asin, want_author, want_title)
        if unusable is not None:
            problems.append(unusable)
            print(f"  BAD       {asin}  (nothing expected of it)", flush=True)
            continue

        recording = librivox.get(book_id)
        if recording is None:
            problems.append(
                f"{asin}: its librivox pin {book_id} did not verify, "
                "so its public-domain status is unproven"
            )
            print(f"  BAD       {asin}  (librivox {book_id} unverified)", flush=True)
            continue

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
                    **librivox_fields(recording, data.get("language")),
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
        print(f"  {mark}      {asin}  [{home}]  visible in: {visible or 'nowhere'}", flush=True)
        proofs.append(row)

    return proofs, problems


def build(librivox: dict[str, dict]) -> tuple[list[dict], list[str]]:
    books: list[dict] = []
    problems: list[str] = []

    for asin, want_author, want_title, tags, book_id in SEEDS:
        unusable = check_fragment(asin, want_author, want_title)
        if unusable is not None:
            problems.append(unusable)
            print(f"  NOCHECK   {asin}  (nothing expected of it)", file=sys.stderr)
            continue

        recording = librivox.get(book_id)
        if recording is None:
            problems.append(
                f"{asin}: its librivox pin {book_id} did not verify, "
                "so its public-domain status is unproven"
            )
            print(f"  NOPD      {asin}  (librivox {book_id} unverified)", file=sys.stderr)
            continue

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

        misspelled = check_spelling(asin, authors)
        if misspelled is not None:
            problems.append(misspelled)
            print(f"  SPELLING  {asin}  {joined}", file=sys.stderr)
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
                **librivox_fields(recording, data.get("language")),
            }
        )
        pos = f" [{series.get('name')} #{series.get('position')}]" if series.get("name") else ""
        print(f"  ok        {asin}  {title}{pos}", flush=True)
        time.sleep(0.25)

    return books, problems


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--check",
        action="store_true",
        help="verify only; exit non-zero if any ASIN or LibriVox pin drifted or died",
    )
    args = ap.parse_args()

    problems = check_librivox_table() + check_spelling_table()

    print(
        f"verifying {len(LIBRIVOX_RECORDINGS)} recordings against librivox.org ...",
        file=sys.stderr,
    )
    librivox, librivox_problems = verify_librivox()
    problems += librivox_problems

    print(f"\nverifying {len(SEEDS)} ASINs against api.audnex.us ...", file=sys.stderr)
    books, build_problems = build(librivox)
    problems += build_problems

    print(f"\nasserting region-lock on {len(REGIONAL_SEEDS)} regional ASINs ...", file=sys.stderr)
    proofs, region_problems = check_region_lock(librivox)
    problems += region_problems

    print(f"\n  librivox recordings verified: {len(librivox)}/{len(LIBRIVOX_RECORDINGS)}",
          file=sys.stderr)
    print(f"  resolved: {len(books)}/{len(SEEDS)}", file=sys.stderr)
    print(f"  region-locked as expected: {len(proofs) - len(region_problems)}/{len(proofs)}",
          file=sys.stderr)

    cross = [b for b in books if not b["librivox_same_language"]]
    if cross:
        print(
            f"\n  {len(cross)} entries pin a recording in another language. The work is "
            "established as\n  public domain; free audio in the edition's own language is not:",
            file=sys.stderr,
        )
        for entry in cross:
            print(f"    - {entry['asin']}  {entry['title'][:44]}  "
                  f"({entry['language']} -> librivox {entry['librivox_language']})",
                  file=sys.stderr)

    if problems:
        print("  PROBLEMS:", file=sys.stderr)
        for p in problems:
            print(f"    - {p}", file=sys.stderr)

    if args.check:
        if problems:
            print("\nFAIL — corpus has drifted from live metadata.", file=sys.stderr)
            return 1
        print("\nOK — every ASIN resolves and matches, and every entry has a verified\n"
              "LibriVox recording of the same work.", file=sys.stderr)
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
