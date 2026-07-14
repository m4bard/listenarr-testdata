#!/usr/bin/env python3
"""Generate a synthetic audiobook library for a named scenario.

    python3 tools/generate_library.py --scenario mixed-reality --out ./build/library --seed 42

Reads `corpus/corpus.json` (real books, machine-verified ASINs) and `corpus/cases.py`
(the six axes and the scenarios that compose them), and writes a real on-disk library:
folders named per the scenario's layouts, ~1-second silent audio files synthesized with
ffmpeg, and genuine embedded tags written with mutagen.

Two properties matter and are enforced:

* **Deterministic.** The same --seed produces a byte-identical tree. All randomness runs
  through one seeded `random.Random`; the silent audio is generated with ffmpeg's bitexact
  flags and copied, never re-encoded per file.
* **Answer key.** `manifest.json` in the output root records, for every file written, the
  book it *actually* belongs to and the outcome a correct scanner should reach. Without it
  a generated library is a pile of files; with it, it is a conformance suite.

The path-hazard axis deserves a note, because it is the one that can destroy data. A hazard
is written **into the tag**, always and verbatim — tags are attacker-controlled input, and
the tag is what a renamer interpolates into a path. The hazard reaches the on-disk path only
where POSIX permits it (a colon, a question mark, a trailing space, `CON`, a bidi override
are all legal filenames on ext4, and writing them for real is better evidence than
describing them). Where POSIX forbids it — a `/`, a NUL, a `../` escape, a component over
255 bytes — the path falls back to a neutralized rendering, which is exactly what a correct
renamer must do with the same value.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import random
import re
import shutil
import subprocess
import sys
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, ClassVar

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "corpus"))

import cases  # noqa: E402  (needs the sys.path line above)

CORPUS = ROOT / "corpus" / "corpus.json"

# ext4/APFS cap a single path component at 255 BYTES, not characters. See the
# component-length hazard in cases.py: a Cyrillic or CJK title overflows at roughly a
# third of the character count you would expect.
MAX_COMPONENT_BYTES = 255

CONTAINER_FOR_DIALECT = {
    "mp4-atoms": "m4b",
    "id3v23": "mp3",
    "id3v24": "mp3",
    "vorbis": "flac",
    "none": "mp3",
}
DIALECT_FOR_EXT = {"m4b": "mp4-atoms", "m4a": "mp4-atoms", "mp3": "id3v24", "flac": "vorbis"}


# --------------------------------------------------------------------------
# Path rendering
# --------------------------------------------------------------------------

def clamp_bytes(value: str, limit: int = MAX_COMPONENT_BYTES) -> str:
    """Truncate to `limit` BYTES of UTF-8 without splitting a character."""
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value
    return encoded[:limit].decode("utf-8", errors="ignore")


def posix_component(value: str) -> str:
    """Render one metadata value as a single path component that ext4 will accept.

    This is deliberately the *minimum* neutralization a working renamer must perform, not a
    general-purpose sanitizer. It removes only what POSIX cannot represent — the separator,
    control characters, an empty component — and clamps to the 255-byte limit. Everything
    else survives to disk verbatim: colons, question marks, ampersands, reserved Windows
    device names, leading dots, trailing spaces, bidi overrides. Those are all legal here,
    and a hazard that really exists on disk is better evidence than one we only describe.
    """
    cleaned = value.replace("/", "-")
    cleaned = "".join(ch for ch in cleaned if unicodedata.category(ch) != "Cc")
    if cleaned.strip(". ") == "":
        # empty, whitespace-only, or a bare "." / ".." — none of which can name a folder
        return "_"
    return clamp_bytes(cleaned)


def posix_filename(name: str) -> str:
    """Like posix_component, but for a leaf FILE: clamp without eating the extension.

    The 255-byte cap applies to the whole component, extension included. Clamping the name
    first and appending `.m4b` afterwards overflows; clamping afterwards truncates the
    extension away and leaves a file no scanner will even look at. The extension has to be
    reserved out of the budget before the stem is cut — which is precisely the mistake a
    renamer makes when it treats the limit as a character count on the title alone.
    """
    cleaned = name.replace("/", "-")  # before any split: a title's slash is not a separator
    stem, dot, suffix = cleaned.rpartition(".")
    if not dot or not stem:
        return posix_component(cleaned)  # no extension, or a dotfile like '.DS_Store'
    extension = f".{suffix}"
    budget = MAX_COMPONENT_BYTES - len(extension.encode("utf-8"))
    return clamp_bytes(posix_component(stem), budget) + extension


def drop_empty(segment: str, values: dict[str, str]) -> str:
    """Remove placeholders this book cannot fill, along with their orphaned separators.

    A standalone book has no series, so `{author} - {series} - {title}` must render as
    'Austen - Persuasion', not 'Austen -  - Persuasion'. Real libraries do not leave the
    gap, and a generator that does is testing its own artefact rather than the scanner.
    """
    for key, value in values.items():
        if value:
            continue
        placeholder = "{" + key + "}"
        if placeholder not in segment:
            continue
        segment = re.sub(rf"\s*-\s*{re.escape(placeholder)}", "", segment)
        segment = re.sub(rf"{re.escape(placeholder)}\s*-\s*", "", segment)
        segment = segment.replace(placeholder, "")
    segment = re.sub(r"\s+([\]\)])", r"\1", segment)   # '[Barsoom ]' -> '[Barsoom]'
    segment = re.sub(r"[\[\(]\s*[\]\)]", "", segment)  # an empty bracket group
    return re.sub(r"\s{2,}", " ", segment).strip()


def render(
    pattern: str,
    values: dict[str, str],
    allow_empty: bool = False,
) -> pathlib.PurePosixPath | None:
    """Expand a layout pattern into a relative path, one safe component per segment.

    Returns None if a whole segment would be empty — which is how a book opts out of a
    layout it cannot express. A standalone book has no `{series}` directory level to stand
    in, so it does not appear in a series layout at all; it does not get a blank folder.

    `allow_empty` is for the empty-field HAZARD, where the blank is the entire point: an
    author credited '' (or 'Various', or 'Anonymous') is real, and the file must land on
    disk under whatever degenerate component that produces. Without this the book would be
    quietly skipped and the hazard would test nothing at all — which is exactly what it did
    until a test asked whether every declared hazard was really being generated.
    """
    parts: list[str] = []
    for segment in pattern.split("/"):
        if not segment:
            continue
        try:
            expanded = drop_empty(segment, values).format(**values)
        except KeyError:
            return None
        if "{" in segment and not expanded.strip(" -[]") and not allow_empty:
            return None
        parts.append(posix_component(expanded))
    return pathlib.PurePosixPath(*parts) if parts else pathlib.PurePosixPath()


# --------------------------------------------------------------------------
# Axis 2: tag-state transforms — "the tags lie", modelled
# --------------------------------------------------------------------------

@dataclass
class Meta:
    """The metadata actually written into a file's tags. May be a lie."""

    title: str
    authors: list[str]
    narrators: list[str] = field(default_factory=list)
    series: str | None = None
    series_position: str | None = None
    asin: str | None = None
    year: str | None = None

    @classmethod
    def truth(cls, book: dict[str, Any]) -> Meta:
        return cls(
            title=book["title"],
            authors=list(book["authors"]),
            narrators=list(book["narrators"]),
            series=book["series"],
            series_position=book["series_position"],
            asin=book["asin"],
            year=(book["release_date"] or "")[:4] or None,
        )


AUTHOR_VARIANTS: list[tuple[str, str]] = [
    # (matches this author, credit them as this instead) — all four kinds named in cases.py:
    # initials spacing, diacritic folding, transliteration, pseudonym <-> real name.
    ("H. G. Wells", "H.G. Wells"),
    ("E. M. Forster", "E.M. Forster"),
    ("H. P. Lovecraft", "HP Lovecraft"),
    ("M. R. James", "M.R. James"),
    ("J. M. Barrie", "J.M. Barrie"),
    ("Émile Zola", "Emile Zola"),
    ("Karel Čapek", "Karel Capek"),
    ("Charlotte Brontë", "Charlotte Bronte"),
    ("Emily Brontë", "Emily Bronte"),
    ("Anne Brontë", "Anne Bronte"),
    ("Fyodor Dostoevsky", "Fyodor Dostoyevsky"),
    ("Dostoyevsky", "Dostoevsky"),
    ("Leo Tolstoy", "Lev Tolstoi"),
    ("Mark Twain", "Samuel Clemens"),
    ("George Eliot", "Mary Ann Evans"),
    ("Saki", "Hector Hugh Munro"),
    ("O. Henry", "William Sydney Porter"),
    ("Voltaire", "François-Marie Arouet"),
    ("Maurice Leblanc", "Maurice Marie Émile Leblanc"),
]

# Translators really credited on these works. `translator-as-author` writes one of these into
# the artist field instead of the author — a false-negative trap, since the record lists them.
TRANSLATORS: dict[str, str] = {
    "Fyodor Dostoevsky": "Constance Garnett",
    "Leo Tolstoy": "Louise Maude",
    "Jules Verne": "George Makepeace Towle",
    "Homer": "Samuel Butler",
    "Émile Zola": "Havelock Ellis",
    "Miguel de Cervantes": "John Ormsby",
    "Johann Wolfgang von Goethe": "Bayard Taylor",
    "Victor Hugo": "Isabel F. Hapgood",
}

# Same work, different numeral style. Both sides of each pair are real, live Audible titles —
# the Verne pair is the work-key proof: two ASINs, one series slot, titles differing only here.
NUMERAL_VARIANTS: list[tuple[str, str]] = [
    ("Twenty Thousand Leagues", "20,000 Leagues"),
    ("20,000 Leagues", "Twenty Thousand Leagues"),
    ("Eighty Days", "80 Days"),
    ("80 Days", "Eighty Days"),
    ("Two Years", "2 Years"),
    ("Thirty-Nine Steps", "39 Steps"),
    ("Four Million", "4 Million"),
]


def author_variant(name: str) -> str | None:
    """Credit the same author under a different spelling, or None if we know no variant."""
    for canonical, variant in AUTHOR_VARIANTS:
        if canonical.lower() in name.lower():
            return name.replace(canonical, variant) if canonical in name else variant
    # Fall back to a generic but realistic transform: fold diacritics.
    folded = "".join(
        ch for ch in unicodedata.normalize("NFD", name) if unicodedata.category(ch) != "Mn"
    )
    return folded if folded != name else None


def numeral_variant(title: str) -> str | None:
    for spelled, numeric in NUMERAL_VARIANTS:
        if spelled in title:
            return title.replace(spelled, numeric)
    return None


def base_title(title: str) -> str:
    """The title with any subtitle removed: 'She: A History of Adventure' -> 'She'.

    Audnex does not reliably split these. For most of the corpus the canonical title carries
    the subtitle inline and the `subtitle` field is null, so the base form has to be
    recovered from the string — which is exactly what a scanner comparing a folder name to a
    record title has to do, and exactly where the Haggard collision bites.
    """
    return title.split(":", 1)[0].strip()


def subtitled_title(book: dict[str, Any]) -> str | None:
    """The same work's FULL title, where the folder carries only the base form.

    A true match that must survive any fix aimed at colliding-title. Folder 'She', tag
    'She: A History of Adventure' — same work. A containment rule tightened until the
    Haggard collision goes away will usually kill this one too, which is why it is here.
    """
    title = str(book["title"])
    if book["subtitle"]:
        return f"{title}: {book['subtitle']}"
    if ":" in title:
        return title
    return None


def same_author_other_book(book: dict[str, Any], corpus: list[dict[str, Any]]) -> dict | None:
    """A DIFFERENT book by the SAME author. The author check gives no discrimination here."""
    mine = {a.lower() for a in book["authors"]}
    others = [
        other
        for other in corpus
        if other["asin"] != book["asin"]
        and other["title"] != book["title"]
        and mine & {a.lower() for a in other["authors"]}
    ]
    return others[0] if others else None


def colliding_title_book(book: dict[str, Any], corpus: list[dict[str, Any]]) -> dict | None:
    """A different book whose title contains, or is contained by, this one.

    The Haggard trap: folder says 'She', tag says 'She and Allan'. A bidirectional Contains
    match attributes both to the same work. Restricted to same-author collisions, which is
    what makes it vicious — the author check agrees, so it cannot arbitrate.

    Compared on BASE titles, because that is the comparison a scanner actually performs: the
    canonical title is 'She: A History of Adventure' and no folder is ever named that.
    """
    mine = base_title(book["title"]).lower()
    mine_authors = {a.lower() for a in book["authors"]}
    candidates = [
        other
        for other in corpus
        if other["asin"] != book["asin"]
        and base_title(other["title"]).lower() != mine
        and mine_authors & {a.lower() for a in other["authors"]}
        and (mine in base_title(other["title"]).lower()
             or base_title(other["title"]).lower() in mine)
    ]
    return candidates[0] if candidates else None


def apply_tag_state(
    state: str,
    book: dict[str, Any],
    corpus: list[dict[str, Any]],
    rng: random.Random,
) -> tuple[Meta | None, Meta, bool]:
    """Build the metadata for this tag state. Returns (tags, path, applied).

    THE FOLDER TELLS THE TRUTH; ONLY THE TAGS LIE. Every expectation in cases.py is written
    as what a correct scanner should do *given that the folder identifies the book
    correctly* — 'prefer the folder, surface the conflict'. So the path metadata is the
    book's real metadata, and the tag metadata is what the transform made of it. Building
    the folder from the lie too would leave no disagreement to detect, and the scenario
    would pass against a scanner that has no conflict handling at all.

    `tags` is None for no-tags. `applied` is False when this book cannot express this state
    — no subtitle, no colliding sibling, no known author variant — and the caller then
    records what actually happened, so the manifest never claims a transform that did not
    take place.
    """
    tags = Meta.truth(book)
    path = Meta.truth(book)

    if state == "correct-with-asin":
        return tags, path, True

    if state == "correct-no-asin":
        tags.asin = None
        return tags, path, True

    if state == "no-tags":
        return None, path, True

    if state == "wrong-title-same-author":
        other = same_author_other_book(book, corpus)
        if not other:
            return tags, path, False
        tags.title = other["title"]
        tags.asin = None
        return tags, path, True

    if state == "wrong-author":
        others = [b for b in corpus if not ({a.lower() for a in b["authors"]}
                                            & {a.lower() for a in book["authors"]})]
        if not others:
            return tags, path, False
        tags.authors = list(rng.choice(others)["authors"])
        tags.asin = None
        return tags, path, True

    if state == "wrong-asin":
        # Borrow a REAL ASIN from a different corpus book. Never invent one.
        others = [b for b in corpus if b["asin"] != book["asin"]]
        tags.asin = rng.choice(others)["asin"]
        return tags, path, True

    if state == "colliding-title":
        other = colliding_title_book(book, corpus)
        if not other:
            return tags, path, False
        tags.title = other["title"]
        tags.asin = None
        return tags, path, True

    if state == "subtitled-title":
        full = subtitled_title(book)
        if not full:
            return tags, path, False
        # The one state where the folder is NOT the canonical title: no human names a folder
        # 'She: A History of Adventure'. The folder carries the base form, the tag the full
        # one, and they are the same work.
        tags.title = full
        path.title = base_title(full)
        tags.asin = None
        return tags, path, True

    if state == "author-variant":
        variants = [author_variant(a) for a in book["authors"]]
        if not any(variants):
            return tags, path, False
        tags.authors = [v or a for v, a in zip(variants, book["authors"], strict=True)]
        tags.asin = None
        return tags, path, True

    if state == "translator-as-author":
        for author in book["authors"]:
            for known, translator in TRANSLATORS.items():
                if known.lower() in author.lower():
                    tags.authors = [translator]
                    tags.asin = None
                    return tags, path, True
        return tags, path, False

    if state == "numeral-variant":
        variant = numeral_variant(book["title"])
        if not variant:
            return tags, path, False
        tags.title = variant
        tags.asin = None
        return tags, path, True

    raise ValueError(f"unknown tag state: {state}")


# --------------------------------------------------------------------------
# Axis 4: path hazards — metadata that is dangerous to write to a filesystem
# --------------------------------------------------------------------------
# Each hazard transforms one field. The result goes into the TAG verbatim; the on-disk path
# gets whatever posix_component() can keep. Hazards marked `twin` emit the book a SECOND
# time — a collision needs two sides to be a collision.

@dataclass(frozen=True)
class HazardSpec:
    key: str
    target: str                       # "title" or "author"
    transform: Callable[[str], str]
    twin: Callable[[str], str] | None = None  # second emission, for collision pairs
    expect: str = ""


LONG_PAD = "Being a Narrative of Certain Particulars Herein Faithfully Set Down "
RLO = "‮"  # RIGHT-TO-LEFT OVERRIDE — makes a filename display unlike its bytes


HAZARDS: list[HazardSpec] = [
    HazardSpec("colon", "title", lambda t: f"{t}: A History of Adventure",
               expect="colon survives on POSIX; must be sanitized, not dropped, on NTFS"),
    HazardSpec("slash", "title", lambda t: f"{t} / Or, The Modern Prometheus",
               expect="a slash in a tag must NOT create a nested directory"),
    HazardSpec("question-mark", "title", lambda t: f"Can {t} Be?",
               expect="illegal on Windows, glob metacharacter everywhere"),
    HazardSpec("reserved-name", "title", lambda _t: "CON",
               expect="CON/PRN/AUX/NUL cannot be a filename on Windows even with an extension"),
    HazardSpec("trailing-dot-space", "title", lambda t: f"{t}. ",
               expect="Windows silently strips these — the path written is not the path read"),
    HazardSpec("leading-dot", "title", lambda t: f".{t}",
               expect="a dotfile: the book vanishes from its own library"),
    HazardSpec("component-length", "title", lambda t: (t + " " + LONG_PAD * 6).strip(),
               expect="over 255 BYTES; any check written against len(str) is wrong"),
    HazardSpec("total-path-length", "title", lambda t: f"{t} {LONG_PAD * 2}".strip(),
               expect="pushes the full path past the Windows MAX_PATH of 260"),
    HazardSpec("unicode-normalization", "title",
               lambda t: unicodedata.normalize("NFC", t),
               twin=lambda t: unicodedata.normalize("NFD", t),
               expect="NFC and NFD are two folders on ext4 and one on APFS"),
    HazardSpec("case-collision", "title", lambda t: t, twin=lambda t: t.upper(),
               expect="two folders on ext4, one on APFS/NTFS — one book overwrites the other"),
    HazardSpec("path-traversal", "title", lambda _t: "../../../../etc/listenarr",
               expect="SECURITY: must be REFUSED, not sanitized-and-used. No path may escape "
                      "the library root"),
    HazardSpec("control-chars", "title", lambda t: f"{t}\nlisten\tarr\r",
               expect="a newline in a title breaks line-oriented tooling"),
    HazardSpec("shell-metachars", "title", lambda t: f"{t} & $PATH `id` 100% #1 *",
               expect="catastrophic if any code path builds a shell string; ffprobe is a "
                      "subprocess, so this is live"),
    HazardSpec("empty-field", "author", lambda _a: "",
               expect="produces //, or a folder named ' '; must fall back, not collapse"),
    HazardSpec("rtl-and-bidi", "title", lambda t: f"{RLO}{t}",
               expect="the filename DISPLAYS differently from its bytes — a spoofing vector"),
]

HAZARDS_BY_KEY = {h.key: h for h in HAZARDS}


def apply_hazard(spec: HazardSpec, meta: Meta, use_twin: bool = False) -> Meta:
    """Return a copy of `meta` with the hazard applied to its target field."""
    transform = spec.twin if (use_twin and spec.twin) else spec.transform
    assert transform is not None
    hazardous = Meta(**vars(meta))
    hazardous.authors = list(meta.authors)
    hazardous.narrators = list(meta.narrators)
    if spec.target == "title":
        hazardous.title = transform(meta.title)
    else:
        hazardous.authors = [transform(a) for a in meta.authors]
    return hazardous


# --------------------------------------------------------------------------
# Audio: synthesize once, copy per file
# --------------------------------------------------------------------------

class SilenceFactory:
    """One second of silence per container, generated once and copied thereafter.

    Per-file ffmpeg would make the `scale` scenario — tens of thousands of files — take
    hours. The bitexact flags make ffmpeg's output byte-stable, so a copied base file plus
    deterministic tagging gives a byte-identical tree for a given seed.
    """

    CODECS: ClassVar[dict[str, tuple[str, str]]] = {
        "m4b": ("aac", "mp4"), "m4a": ("aac", "mp4"),
        "mp3": ("libmp3lame", "mp3"), "flac": ("flac", "flac"),
    }

    def __init__(self, cache: pathlib.Path) -> None:
        self.cache = cache
        self.cache.mkdir(parents=True, exist_ok=True)
        self._made: dict[str, pathlib.Path] = {}

    def base(self, ext: str) -> pathlib.Path:
        if ext in self._made:
            return self._made[ext]
        codec, container = self.CODECS[ext]
        out = self.cache / f"silence.{ext}"
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-fflags", "+bitexact",
            "-f", "lavfi", "-i", "anullsrc=r=22050:cl=mono",
            "-t", "1", "-c:a", codec, "-flags:a", "+bitexact",
        ]
        if container == "mp3":
            cmd += ["-write_xing", "0"]  # the Xing header carries an encoder version string
        cmd += ["-f", container, str(out)]
        subprocess.run(cmd, check=True, capture_output=True)
        self._made[ext] = out
        return out

    def place(self, ext: str, dest: pathlib.Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self.base(ext), dest)


# --------------------------------------------------------------------------
# Axis 5: tag dialects — write the ASIN where this container actually carries it
# --------------------------------------------------------------------------

def write_tags(path: pathlib.Path, meta: Meta, dialect: str) -> dict[str, str]:
    """Write embedded tags and return what was written, for the manifest.

    The ASIN lands under the dialect-appropriate key: an iTunes freeform atom in an m4b, a
    TXXX frame in an mp3, a plain Vorbis comment in a flac. `ExtractAsin` upstream carries
    twenty-odd spellings and nothing proves it reads them all — this is what proves it.
    """
    from mutagen.flac import FLAC
    from mutagen.id3 import ID3, TALB, TCOM, TDRC, TIT2, TPE1, TPE2, TXXX, ID3NoHeaderError
    from mutagen.mp4 import MP4, MP4FreeForm

    written: dict[str, str] = {}
    author = ", ".join(meta.authors)
    narrator = ", ".join(meta.narrators)
    album = meta.series or meta.title

    if dialect == "mp4-atoms":
        audio = MP4(path)
        audio["\xa9nam"] = [meta.title]
        audio["\xa9ART"] = [author]
        audio["aART"] = [author]
        audio["\xa9alb"] = [album]
        if narrator:
            audio["\xa9wrt"] = [narrator]  # the Audible convention: narrator in composer
        if meta.year:
            audio["\xa9day"] = [meta.year]
        if meta.series:
            audio["----:com.apple.iTunes:SERIES"] = [
                MP4FreeForm(meta.series.encode("utf-8"))
            ]
            written["----:com.apple.iTunes:SERIES"] = meta.series
        if meta.series_position:
            audio["----:com.apple.iTunes:SERIES-PART"] = [
                MP4FreeForm(meta.series_position.encode("utf-8"))
            ]
            written["----:com.apple.iTunes:SERIES-PART"] = meta.series_position
        if meta.asin:
            audio["----:com.apple.iTunes:ASIN"] = [MP4FreeForm(meta.asin.encode("utf-8"))]
            written["----:com.apple.iTunes:ASIN"] = meta.asin
        audio.save()

    elif dialect in ("id3v23", "id3v24"):
        try:
            audio_id3 = ID3(path)
        except ID3NoHeaderError:
            audio_id3 = ID3()
        audio_id3.add(TIT2(encoding=3, text=[meta.title]))
        audio_id3.add(TPE1(encoding=3, text=[author]))
        audio_id3.add(TPE2(encoding=3, text=[author]))
        audio_id3.add(TALB(encoding=3, text=[album]))
        if narrator:
            audio_id3.add(TCOM(encoding=3, text=[narrator]))
        if meta.year:
            audio_id3.add(TDRC(encoding=3, text=[meta.year]))
        if meta.series:
            audio_id3.add(TXXX(encoding=3, desc="SERIES", text=[meta.series]))
            written["TXXX:SERIES"] = meta.series
        if meta.series_position:
            audio_id3.add(TXXX(encoding=3, desc="SERIES-PART", text=[meta.series_position]))
            written["TXXX:SERIES-PART"] = meta.series_position
        if meta.asin:
            audio_id3.add(TXXX(encoding=3, desc="ASIN", text=[meta.asin]))
            written["TXXX:ASIN"] = meta.asin
            if dialect == "id3v24":
                audio_id3.add(TXXX(encoding=3, desc="AUDIBLE_ASIN", text=[meta.asin]))
                written["TXXX:AUDIBLE_ASIN"] = meta.asin
        # v2.3 and v2.4 differ in frame semantics, date handling in particular. Writing both
        # is the point: a parser that only understands one silently reads nothing from the other.
        audio_id3.save(path, v2_version=3 if dialect == "id3v23" else 4)

    elif dialect == "vorbis":
        audio_flac = FLAC(path)
        audio_flac["title"] = [meta.title]
        audio_flac["artist"] = [author]
        audio_flac["albumartist"] = [author]
        audio_flac["album"] = [album]
        if narrator:
            audio_flac["composer"] = [narrator]
        if meta.year:
            audio_flac["date"] = [meta.year]
        if meta.series:
            audio_flac["series"] = [meta.series]
            written["SERIES"] = meta.series
        if meta.series_position:
            audio_flac["series-part"] = [meta.series_position]
            written["SERIES-PART"] = meta.series_position
        if meta.asin:
            # Vorbis comments are case-insensitive by spec, but ffprobe surfaces them
            # verbatim — so a lowercase key really can be missed by a case-sensitive reader.
            audio_flac["asin"] = [meta.asin]
            written["asin"] = meta.asin
        audio_flac.save()

    elif dialect == "none":
        # An mp3 with ordinary tags and no ASIN in any spelling — the common real-world case.
        try:
            audio_id3 = ID3(path)
        except ID3NoHeaderError:
            audio_id3 = ID3()
        audio_id3.add(TIT2(encoding=3, text=[meta.title]))
        audio_id3.add(TPE1(encoding=3, text=[author]))
        audio_id3.add(TALB(encoding=3, text=[album]))
        audio_id3.save(path, v2_version=4)

    else:
        raise ValueError(f"unknown tag dialect: {dialect}")

    written["title"] = meta.title
    written["artist"] = author
    written["album"] = album
    return written


# --------------------------------------------------------------------------
# Axis 6: clutter — everything in a library that is not the book
# --------------------------------------------------------------------------

def emit_clutter(
    book_dir: pathlib.Path,
    root: pathlib.Path,
    book: dict[str, Any],
    kinds: list[str],
    silence: SilenceFactory,
) -> list[dict[str, Any]]:
    """Write the non-book files and return their manifest entries.

    CollectCandidates upstream grabs *every* audio file under the scan root, so the sample,
    the intro and the bonus interview all land in the candidate set alongside the book.
    """
    entries: list[dict[str, Any]] = []

    def note(path: pathlib.Path, kind: str, expect: str) -> None:
        entries.append({
            "path": str(path.relative_to(root)),
            "kind": "clutter",
            "clutter_kind": kind,
            "belongs_to_asin": None,
            # Nothing here is the book. CollectCandidates grabs every audio file under the
            # root, so the sample, the intro and the bonus interview all reach the candidate
            # set — and a correct scanner attaches none of them to anything.
            "expect_linked_asin": None,
            "expect": expect,
        })

    for kind in kinds:
        if kind == "sidecars":
            (book_dir / "desc.txt").write_text(
                f"{book['title']} by {', '.join(book['authors'])}. "
                "Read for LibriVox; this recording is in the public domain.\n"
            )
            (book_dir / "reader.txt").write_text(
                ", ".join(book["narrators"]) + "\n" if book["narrators"] else "Anonymous\n"
            )
            note(book_dir / "desc.txt", kind, "read by PathMetadataParser as the description")
            note(book_dir / "reader.txt", kind, "read by PathMetadataParser as the narrator")

        elif kind == "cover-variants":
            for name in ("cover.jpg", "folder.png", "cover.webp"):
                (book_dir / name).write_bytes(b"\xff\xd8\xff\xe0not-a-real-image")
                note(book_dir / name, kind, "cover art; not a scan candidate")

        elif kind == "sample-track":
            for name in ("sample.mp3", "preview.mp3"):
                silence.place("mp3", book_dir / name)
                note(book_dir / name, kind,
                     "AUDIO, but not the book — must not be linked as a part of it")

        elif kind == "intro-outro":
            for name in ("00 - Intro.mp3", "Outro.mp3"):
                silence.place("mp3", book_dir / name)
                note(book_dir / name, kind, "audio, not a chapter — must not become a part")

        elif kind == "bonus-content":
            name = "Bonus - Interview with the Author.mp3"
            silence.place("mp3", book_dir / name)
            note(book_dir / name, kind, "real audio, wrong book — must not be linked")

        elif kind == "non-audio":
            for name, blob in (
                ("book.pdf", b"%PDF-1.4\n%not-a-real-pdf\n"),
                ("info.nfo", b"released by nobody\n"),
                ("playlist.cue", b'FILE "book.mp3" MP3\n  TRACK 01 AUDIO\n'),
                ("subtitles.srt", b"1\n00:00:00,000 --> 00:00:01,000\nsilence\n"),
            ):
                (book_dir / name).write_bytes(blob)
                note(book_dir / name, kind, "not audio; must be ignored")

        elif kind == "os-detritus":
            (book_dir / ".DS_Store").write_bytes(b"\x00\x00\x00\x01Bud1")
            (book_dir / "Thumbs.db").write_bytes(b"\xd0\xcf\x11\xe0")
            for hidden in ("@eaDir", ".stfolder"):
                (book_dir / hidden).mkdir(exist_ok=True)
                (book_dir / hidden / "junk.mp3").write_bytes(b"")
                note(book_dir / hidden / "junk.mp3", kind,
                     "inside a Synology/Syncthing metadata dir — must be ignored entirely")
            note(book_dir / ".DS_Store", kind, "OS detritus; must be ignored")
            note(book_dir / "Thumbs.db", kind, "OS detritus; must be ignored")

        elif kind == "zero-byte":
            (book_dir / "empty.mp3").write_bytes(b"")
            note(book_dir / "empty.mp3", kind,
                 "0 bytes: ffprobe returns nothing. Must not crash or hang the scan")

        elif kind == "corrupt-audio":
            (book_dir / "corrupt.m4b").write_bytes(b"this is not an mp4 atom tree\n" * 8)
            note(book_dir / "corrupt.m4b", kind,
                 "audio extension, not audio. Must not crash or hang the scan")

    return entries


# --------------------------------------------------------------------------
# Generation
# --------------------------------------------------------------------------

def load_corpus() -> list[dict[str, Any]]:
    data = json.loads(CORPUS.read_text(encoding="utf-8"))
    books: list[dict[str, Any]] = data["books"]
    return sorted(books, key=lambda b: b["asin"])  # a stable order, independent of file order


def pattern_values(meta: Meta) -> dict[str, str]:
    return {
        "author": ", ".join(meta.authors) if meta.authors else "",
        "title": meta.title,
        "series": meta.series or "",
        "series_position": meta.series_position or "",
        "year": meta.year or "",
    }


def files_for(structure: cases.FileStructure, meta: Meta) -> list[tuple[str, int]]:
    """Expand a file structure into (relative path, part index) pairs."""
    out: list[tuple[str, int]] = []
    safe_title = posix_component(meta.title)
    for n in range(1, structure.parts + 1):
        for template in structure.files:
            rel = template.format(title=safe_title, n=n)
            segments = rel.split("/")
            rendered = [posix_component(p) for p in segments[:-1]] + [posix_filename(segments[-1])]
            out.append(("/".join(rendered), n))
    return out


Unit = tuple[str, int, int]  # (asin, copy index, hazard emission) — one book, once


def twin_differs(spec: HazardSpec, meta: Meta) -> bool:
    """Does this hazard's twin actually differ from its first emission for THIS book?

    NFC and NFD are the same string for a title with no combining marks; upper-casing does
    nothing to a CJK or all-digit title. Emitting a twin anyway would write the same path
    twice and quietly overwrite the first — the manifest would then claim a file that is not
    there. A collision needs two distinguishable sides, or it is not a collision.
    """
    first = apply_hazard(spec, meta, use_twin=False)
    second = apply_hazard(spec, meta, use_twin=True)
    return (first.title, tuple(first.authors)) != (second.title, tuple(second.authors))


def claim(
    path: pathlib.Path,
    unit: Unit,
    owner: dict[str, Unit],
    book: dict[str, Any],
    reuse: bool,
) -> tuple[pathlib.Path, bool]:
    """Reserve `path` for this book, disambiguating if another book already holds it.

    Two ASINs of the same work — the Barsoom pair, the Verne pair — render to an identical
    path under any layout that carries no year. Left alone, the second write overwrites the
    first and the manifest claims a file that is not on disk. A real library disambiguates
    these by narrator or edition, so that is what we do here. Silently losing a book is the
    exact failure this repository exists to make visible; a generator may not commit it.

    `reuse` is True for a book's own folder, which its several files all share, and False
    for a file, which nothing may ever write twice.
    """
    def free(candidate: pathlib.Path) -> bool:
        held = owner.get(str(candidate))
        return held is None or (reuse and held == unit)

    if free(path):
        owner[str(path)] = unit
        return path, False

    stem, dot, suffix = path.name.rpartition(".")
    tokens = [", ".join(book["narrators"])] if book["narrators"] else []
    tokens.append(book["asin"])
    for token in tokens:
        name = f"{stem} [{token}]{dot}{suffix}" if dot else f"{path.name} [{token}]"
        candidate = path.parent / posix_filename(name)
        if free(candidate):
            owner[str(candidate)] = unit
            return candidate, True

    raise RuntimeError(f"cannot disambiguate {path} for {book['asin']}")


def generate(
    scenario: cases.Scenario,
    out: pathlib.Path,
    seed: int,
    limit: int | None = None,
) -> dict[str, Any]:
    """Generate the library for one scenario and return its manifest."""
    rng = random.Random(seed)
    corpus = load_corpus()
    if limit:
        corpus = corpus[:limit]

    out.mkdir(parents=True, exist_ok=True)
    silence = SilenceFactory(out / ".silence-cache")

    layouts = [cases.LAYOUTS_BY_KEY[k] for k in scenario.layouts]
    tag_states = list(scenario.tag_states)
    weights: dict[str, float] = scenario.extras.get("tag_state_weights", {})
    structures = [cases.STRUCTURES_BY_KEY[k]
                  for k in scenario.extras.get("structures", ["single"])]
    dialects: list[str] = scenario.extras.get("dialects", [])
    hazards: list[str] = scenario.extras.get("hazards", [])
    clutter: list[str] = scenario.extras.get("clutter", [])
    repeat = int(scenario.extras.get("repeat_factor", 1))

    entries: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    dir_owner: dict[str, Unit] = {}
    file_owner: dict[str, Unit] = {}

    for copy_index in range(repeat):
        for index, book in enumerate(corpus):
            truth = Meta.truth(book)

            # --- layout: a book opts out of a layout it cannot express (no series, no year)
            eligible = [
                layout for layout in layouts
                if render(layout.folder, pattern_values(truth)) is not None
                and render(layout.filename, pattern_values(truth)) is not None
            ]
            if not eligible:
                skipped.append({"asin": book["asin"], "why": "no layout this book can express"})
                continue
            layout = eligible[(index + copy_index) % len(eligible)]

            # --- tag state
            if weights:
                known = [s for s in tag_states if s in weights]
                state = rng.choices(known, weights=[weights[s] for s in known], k=1)[0]
            else:
                state = tag_states[(index + copy_index) % len(tag_states)]

            tags, path, applied = apply_tag_state(state, book, corpus, rng)
            if not applied:
                # This book cannot express this state (no subtitle, no colliding sibling, no
                # known author variant). Record the truth and say so — never claim a
                # transform that did not happen.
                state = "correct-no-asin" if tags else state
                if tags:
                    tags.asin = None

            structure = structures[(index + copy_index) % len(structures)]

            # --- hazard: the twin emission is what makes a collision a collision
            hazard_keys: list[str | None] = [None]
            if hazards:
                chosen = HAZARDS_BY_KEY[hazards[(index + copy_index) % len(hazards)]]
                hazard_keys = [chosen.key]
                if chosen.twin and twin_differs(chosen, path):
                    hazard_keys.append(chosen.key)

            for emission, hazard_key in enumerate(hazard_keys):
                # The folder is built from the TRUTH; only the tags carry the transform.
                path_meta = path
                tag_meta = tags
                spec = HAZARDS_BY_KEY[hazard_key] if hazard_key else None
                if spec:
                    path_meta = apply_hazard(spec, path_meta, use_twin=emission == 1)
                    tag_meta = apply_hazard(spec, tag_meta, use_twin=emission == 1) if tag_meta \
                        else None

                folder = render(layout.folder, pattern_values(path_meta),
                                allow_empty=spec is not None)
                if folder is None:
                    skipped.append({"asin": book["asin"], "why": "layout unrenderable"})
                    continue
                # The `loose` layout renders to no components at all: its files sit at the
                # library root and the FILENAME has to carry the metadata. PurePosixPath()
                # stringifies as '.', so emptiness is a question about parts, not about str().
                foldered = bool(folder.parts)
                suffix = f" ({copy_index + 1})" if repeat > 1 else ""
                book_dir = out / folder if foldered else out
                if suffix and foldered:
                    book_dir = out / pathlib.PurePosixPath(
                        *[*folder.parts[:-1], posix_component(folder.parts[-1] + suffix)]
                    )

                unit: Unit = (book["asin"], copy_index, emission)
                moved = False
                if foldered:
                    book_dir, moved = claim(book_dir, unit, dir_owner, book, reuse=True)

                # --- dialect decides the container; otherwise the structure's extension does
                if dialects:
                    dialect = dialects[(index + copy_index + emission) % len(dialects)]
                else:
                    dialect = ""

                for rel, part in files_for(structure, path_meta):
                    ext = pathlib.PurePosixPath(rel).suffix.lstrip(".")
                    if dialect:
                        ext = CONTAINER_FOR_DIALECT[dialect]
                        rel = str(pathlib.PurePosixPath(rel).with_suffix(f".{ext}"))
                        this_dialect = dialect
                    else:
                        this_dialect = DIALECT_FOR_EXT[ext]

                    # The loose layout has no folder, so the filename must carry the metadata.
                    if not foldered:
                        stem = layout.filename.format(**pattern_values(path_meta))
                        part_of = f" - Part {part:02d}" if structure.parts > 1 else ""
                        rel = posix_filename(f"{stem}{part_of}.{ext}")

                    dest, file_moved = claim(book_dir / rel, unit, file_owner, book, reuse=False)
                    silence.place(ext, dest)

                    written_tags: dict[str, str] = {}
                    if tag_meta is not None:
                        written_tags = write_tags(dest, tag_meta, this_dialect)

                    entries.append({
                        "path": str(dest.relative_to(out)),
                        "kind": "book",
                        "disambiguated": moved or file_moved,
                        "belongs_to_asin": book["asin"],
                        "true_title": book["title"],
                        "true_authors": book["authors"],
                        "true_series": book["series"],
                        "true_series_asin": book["series_asin"],
                        "true_series_position": book["series_position"],
                        "layout": layout.key,
                        "tag_state": state,
                        "structure": structure.key,
                        "dialect": this_dialect,
                        "hazard": hazard_key,
                        "hazard_twin": bool(spec and spec.twin and emission == 1),
                        "part": part,
                        "of": structure.parts,
                        "tags_written": written_tags,
                        # The machine-checkable expectation. Whatever the tags say, this file
                        # is a part of THIS book and a correct scanner links it to THIS book.
                        # A tag that lies is a reason to surface a conflict, never a reason to
                        # attach the file to the book the tag names.
                        "expect_linked_asin": book["asin"],
                        "expect": cases.TAG_STATES_BY_KEY[state].expect,
                        "expect_hazard": spec.expect if spec else None,
                    })

                if clutter and foldered:
                    entries.extend(emit_clutter(book_dir, out, book, clutter, silence))

    shutil.rmtree(out / ".silence-cache")

    manifest = {
        "scenario": scenario.key,
        "note": scenario.note,
        "expect": scenario.expect,
        "seed": seed,
        "corpus_books": len(corpus),
        "files": len([e for e in entries if e["kind"] == "book"]),
        "clutter_files": len([e for e in entries if e["kind"] == "clutter"]),
        "skipped": skipped,
        "entries": entries,
    }
    (out / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    return manifest


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scenario", help="scenario key; see --list")
    ap.add_argument("--out", type=pathlib.Path, help="output directory (created)")
    ap.add_argument("--seed", type=int, default=1, help="same seed => byte-identical tree")
    ap.add_argument("--limit", type=int, help="use only the first N corpus books (for a "
                                              "quick smoke test)")
    ap.add_argument("--list", action="store_true", help="list the scenarios and exit")
    ap.add_argument("--force", action="store_true", help="overwrite a non-empty --out")
    args = ap.parse_args()

    if args.list:
        for scenario in cases.SCENARIOS:
            print(f"{scenario.key:28} {scenario.expect}")
        return 0

    if not args.scenario or not args.out:
        ap.error("--scenario and --out are required (or --list)")

    if args.scenario not in cases.SCENARIOS_BY_KEY:
        ap.error(f"unknown scenario '{args.scenario}'. Known: "
                 f"{', '.join(cases.SCENARIOS_BY_KEY)}")
    scenario = cases.SCENARIOS_BY_KEY[args.scenario]

    if args.out.exists() and any(args.out.iterdir()):
        if not args.force:
            ap.error(f"{args.out} exists and is not empty; pass --force to overwrite")
        shutil.rmtree(args.out)

    manifest = generate(scenario, args.out, args.seed, args.limit)
    print(f"scenario   {manifest['scenario']}")
    print(f"seed       {manifest['seed']}")
    print(f"books      {manifest['corpus_books']}")
    print(f"files      {manifest['files']} audio + {manifest['clutter_files']} clutter")
    if manifest["skipped"]:
        print(f"skipped    {len(manifest['skipped'])}")
    print(f"expect     {manifest['expect']}")
    print(f"manifest   {args.out / 'manifest.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
