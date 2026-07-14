#!/usr/bin/env python3
"""The case matrix: what a generated test library is actually FOR.

A test library is the cross product of two independent axes:

  LAYOUT    — how the folders are named on disk
  TAG_STATE — what the embedded ID3/MP4 tags say relative to the folder

A scanner can fail on either axis independently, and the interesting bugs live in
the corners where both are adverse at once. Each case declares its EXPECTED
outcome, which is what turns generated data into a conformance suite rather than
just a pile of files.

Expected outcomes are deliberately written as what a CORRECT scanner should do,
not as what Listenarr does today. A case whose expectation Listenarr fails is a
bug report, not a broken fixture.
"""
from __future__ import annotations

from dataclasses import dataclass, field


# --------------------------------------------------------------------------
# Axis 1: on-disk layouts
# --------------------------------------------------------------------------
# `pattern` uses {author} {series} {series_position} {title} {year} placeholders.
# A layout is a folder pattern plus a filename pattern.

@dataclass(frozen=True)
class Layout:
    key: str
    folder: str
    filename: str
    note: str


LAYOUTS: list[Layout] = [
    Layout(
        key="listenarr-native",
        folder="{author}/{year} - {title}",
        filename="{title}",
        note="The layout PathMetadataParser actually requires: '{Year} - {Title}'. "
             "The only one that currently parses.",
    ),
    Layout(
        key="listenarr-native-series",
        folder="{author}/{series}/{year} - {title} [{series} {series_position}]",
        filename="{title}",
        note="Native layout with the series bracket the parser also understands.",
    ),
    Layout(
        key="audnex-plex",
        folder="{author}/{author} - {series} - {title}",
        filename="{author} - {series} - {title}",
        note="The common Audnex/Plex convention. Carries no year, so the native "
             "parser rejects it outright — this is the layout that parses at 0%.",
    ),
    Layout(
        key="author-series-title",
        folder="{author}/{series}/{title}",
        filename="{title}",
        note="Plain author/series/title. No year anywhere.",
    ),
    Layout(
        key="audiobookshelf",
        folder="{author}/{series}/{series_position} - {title}",
        filename="{series} - {series_position} - {title}",
        note="AudioBookShelf-style: series-creator folder, numbered episode files. "
             "The layout PR #688 was written to rescue.",
    ),
    Layout(
        key="flat",
        folder="{author} - {title}",
        filename="{author} - {title}",
        note="Single flat folder per book, no author directory level.",
    ),
    Layout(
        key="loose",
        folder="",
        filename="{author} - {title}",
        note="Loose files at the library root, no containing folder at all.",
    ),
    Layout(
        key="title-only",
        folder="{title}",
        filename="{title}",
        note="Folder carries the title and nothing else — no author anywhere on the "
             "path, so any author-in-path heuristic has nothing to bite on.",
    ),
]


# --------------------------------------------------------------------------
# Axis 2: embedded tag states
# --------------------------------------------------------------------------
# Each state is a transform applied to the book's true metadata before it is
# written into the file's tags. This is where "the tags lie" gets modelled.

@dataclass(frozen=True)
class TagState:
    key: str
    note: str
    # what a correct scanner should do with a file in this state, given that the
    # FOLDER identifies the book correctly:
    expect: str


TAG_STATES: list[TagState] = [
    TagState(
        key="correct-with-asin",
        note="Tags agree with the folder and carry the book's real ASIN.",
        expect="link — ASIN is definitive",
    ),
    TagState(
        key="correct-no-asin",
        note="Tags agree with the folder but carry NO ASIN. This is the common real-world "
             "case: an entire library can contain zero ASINs.",
        expect="link — title+author agree",
    ),
    TagState(
        key="no-tags",
        note="File carries no usable tags at all.",
        expect="link — fall back to the folder; absence of tags is not evidence against",
    ),
    TagState(
        key="wrong-title-same-author",
        note="Tag names a DIFFERENT book by the SAME author. The single most common "
             "disagreement in a real library, and the one where the author check "
             "provides no discrimination whatsoever.",
        expect="do NOT link on tags — conflict; prefer folder, surface for review",
    ),
    TagState(
        key="wrong-author",
        note="Tag names a different author entirely.",
        expect="do NOT link on tags — conflict; prefer folder, surface for review",
    ),
    TagState(
        key="wrong-asin",
        note="Tag carries a real ASIN, but of a DIFFERENT book. A regional or "
             "re-release ASIN lands here too.",
        expect="do NOT link — a definitive signal pointing at the wrong book must not win",
    ),
    TagState(
        key="colliding-title",
        note="Tag names a different book whose title CONTAINS (or is contained by) the "
             "folder's title, e.g. folder 'She' vs tag 'She and Allan'. Defeats naive "
             "containment matching. See the title-collision entries in corpus.json.",
        expect="do NOT link on tags — distinct works",
    ),
    TagState(
        key="subtitled-title",
        note="Tag carries the same work's FULL title with subtitle, e.g. folder 'She' vs "
             "tag 'She: A History of Adventure'. A TRUE match that must survive any fix "
             "aimed at colliding-title.",
        expect="link — same work",
    ),
    TagState(
        key="author-variant",
        note="Same author, different credited spelling: 'H.G. Wells' vs 'H. G. Wells', "
             "'Émile Zola' vs 'Emile Zola', 'Dostoyevsky' vs 'Dostoevsky', "
             "'Saki' vs 'Hector Hugh Munro'.",
        expect="link — same author; strict equality here is a false negative",
    ),
    TagState(
        key="translator-as-author",
        note="Tag credits the translator instead of the author "
             "(e.g. Constance Garnett rather than Dostoevsky).",
        expect="link — do not reject on an author the record also lists",
    ),
    TagState(
        key="numeral-variant",
        note="Same work, numeral style differs: 'Twenty Thousand Leagues' vs "
             "'20,000 Leagues'; 'Eighty Days' vs '80 Days'. Both are real, live titles.",
        expect="link — same work",
    ),
]


# --------------------------------------------------------------------------
# Axis 3: file structure — how one BOOK maps to files on disk
# --------------------------------------------------------------------------
# A book is not one file. CalculateBasePath walks up to the common parent of the
# files it found, so the shape of the file set directly drives what BasePath
# becomes — and BasePath is what later scans trust.

@dataclass(frozen=True)
class FileStructure:
    key: str
    note: str
    # file paths relative to the book folder; {n} is the part index
    files: list[str]
    parts: int = 1


FILE_STRUCTURES: list[FileStructure] = [
    FileStructure(
        key="single",
        note="One .m4b for the whole book. The clean case.",
        files=["{title}.m4b"],
    ),
    FileStructure(
        key="multi-part",
        note="Several numbered parts in one folder.",
        files=["{title} - Part {n:02d}.mp3"],
        parts=3,
    ),
    FileStructure(
        key="multi-disc",
        note="Parts in per-disc SUBFOLDERS. The book's files no longer share a "
             "direct parent, so the common-parent walk has to climb — this is where "
             "BasePath can end up pointing at the wrong level.",
        files=["CD{n}/{title} - CD{n}.mp3"],
        parts=2,
    ),
    FileStructure(
        key="per-chapter",
        note="One file per chapter. Dozens of files per book.",
        files=["{n:03d} - Chapter {n}.mp3"],
        parts=40,
    ),
    FileStructure(
        key="nested-single",
        note="A single file buried one level deeper than expected (e.g. in an "
             "'Audio' or 'MP3' subfolder).",
        files=["Audio/{title}.m4b"],
    ),
]


# --------------------------------------------------------------------------
# Axis 4: path hazards — metadata that is DANGEROUS to write to a filesystem
# --------------------------------------------------------------------------
# These are the destructive ones. A matching bug leaves a file unlinked; a
# renaming bug destroys data. Each hazard is a transform applied to the title or
# author before it is used to build a path.
#
# Most need no new books — they are transforms. Where a REAL public-domain book
# already exhibits the hazard, it is named, because a real one is better evidence.

@dataclass(frozen=True)
class PathHazard:
    key: str
    note: str
    hazard: str
    real_example: str = ""


PATH_HAZARDS: list[PathHazard] = [
    PathHazard(
        key="colon",
        note="Title contains a colon (every subtitle does).",
        hazard="`:` is illegal on NTFS and is the alternate-data-stream separator. "
               "On macOS the Finder shows it as `/`.",
        real_example="Moby-Dick; or, The Whale / She: A History of Adventure",
    ),
    PathHazard(
        key="slash",
        note="Title contains a forward slash.",
        hazard="`/` is the path separator on POSIX — it CANNOT appear in a filename. "
               "Naive interpolation silently creates a nested directory.",
    ),
    PathHazard(
        key="question-mark",
        note="Title ends in a question mark.",
        hazard="`?` is illegal on Windows and is a glob metacharacter.",
        real_example="What Is Man? (Twain)",
    ),
    PathHazard(
        key="reserved-name",
        note="Author or title is a reserved Windows device name.",
        hazard="CON, PRN, AUX, NUL, COM1-9, LPT1-9 cannot be a filename on Windows "
               "even WITH an extension. Creating one fails or hangs.",
    ),
    PathHazard(
        key="trailing-dot-space",
        note="Title ends with a dot or a space.",
        hazard="Windows silently STRIPS trailing dots and spaces, so the path written "
               "is not the path read back — a rename can lose the file.",
    ),
    PathHazard(
        key="leading-dot",
        note="Title begins with a dot.",
        hazard="Creates a hidden file on Unix; many scanners skip dotfiles, so the "
               "book vanishes from its own library.",
    ),
    PathHazard(
        key="component-length",
        note="A single path COMPONENT over 255 BYTES. Note: bytes, not characters.",
        hazard="ext4/APFS cap one component at 255 BYTES. UTF-8 is variable-width, so a "
               "title that looks short can be long on disk — measured from real corpus "
               "entries: 'Белые ночи' is 10 chars but 19 bytes; '杜子春' is 3 chars but "
               "9 bytes. A CJK or Cyrillic title therefore overflows at roughly a THIRD "
               "of the character count you would expect. Any length check written against "
               "len(str) rather than len(str.encode()) is wrong.",
        real_example="Defoe's full Robinson Crusoe title (long); Белые ночи, 杜子春 (dense)",
    ),
    PathHazard(
        key="total-path-length",
        note="Total path over 260 characters.",
        hazard="Windows MAX_PATH is 260 unless long paths are explicitly enabled. "
               "{Root}/{Author}/{Series}/{Year} - {Title} reaches it easily.",
    ),
    PathHazard(
        key="unicode-normalization",
        note="Title/author contains precomposed vs decomposed accents (NFC vs NFD).",
        hazard="macOS normalizes filenames to NFD. A name written as NFC reads back as "
               "a DIFFERENT BYTE STRING, so an exact-match lookup fails and the renamer "
               "can create a duplicate folder alongside the original.",
        real_example="Karel Čapek, Émile Zola, Charlotte Brontë",
    ),
    PathHazard(
        key="case-collision",
        note="Two books whose paths differ only by case.",
        hazard="APFS and NTFS are case-INSENSITIVE by default; ext4 is case-sensitive. "
               "Two distinct folders on Linux collide into one on macOS/Windows — one "
               "book overwrites the other.",
    ),
    PathHazard(
        key="path-traversal",
        note="Metadata field contains `../` or an absolute path.",
        hazard="SECURITY. Tags are attacker-controlled input. A title of `../../etc` "
               "interpolated into a rename target escapes the library root. Must be "
               "rejected, not sanitized-and-used.",
    ),
    PathHazard(
        key="control-chars",
        note="Tag value contains a newline, tab, or NUL.",
        hazard="Embedded tags can carry arbitrary bytes. A newline in a title breaks "
               "line-oriented tooling; a NUL truncates the path in C-level syscalls.",
    ),
    PathHazard(
        key="shell-metachars",
        note="Title contains `&`, `$`, backtick, `%`, `#`, `*`, quotes.",
        hazard="Harmless if paths are passed as argv; catastrophic if any code path "
               "builds a shell string. ffprobe is invoked as a subprocess — this is a "
               "live concern, not a theoretical one.",
    ),
    PathHazard(
        key="empty-field",
        note="Author or title is empty, whitespace-only, or literally 'Unknown'.",
        hazard="Produces a path like `{Root}//{Title}` or a folder named ` `. Also: "
               "authors credited 'Various' or 'Anonymous' collapse an entire library "
               "into one folder.",
    ),
    PathHazard(
        key="rtl-and-bidi",
        note="Title contains right-to-left script or bidi control characters.",
        hazard="Bidi overrides can make a filename DISPLAY differently from its bytes — "
               "a classic spoofing vector, and a rename that looks correct but isn't.",
    ),
]


# --------------------------------------------------------------------------
# Axis 5: tag dialect — the same tag means different things per container
# --------------------------------------------------------------------------
# This is why PathMetadataParser.ExtractAsin carries twenty-odd spellings. ffprobe
# surfaces different key names depending on the container, so a scanner that only
# knows one dialect silently reads nothing.

@dataclass(frozen=True)
class TagDialect:
    key: str
    container: str
    asin_keys: list[str]
    note: str


TAG_DIALECTS: list[TagDialect] = [
    TagDialect(
        key="mp4-atoms",
        container="m4b",
        asin_keys=["----:com.apple.iTunes:ASIN", "----:com.apple.iTunes:CDEK"],
        note="iTunes freeform atoms. The Audible-native shape.",
    ),
    TagDialect(
        key="id3v23",
        container="mp3",
        asin_keys=["TXXX:ASIN"],
        note="ID3v2.3 user-defined text frame. Note v2.3 uses different frame "
             "semantics from v2.4 — date handling in particular.",
    ),
    TagDialect(
        key="id3v24",
        container="mp3",
        asin_keys=["TXXX:ASIN", "TXXX:AUDIBLE_ASIN"],
        note="ID3v2.4.",
    ),
    TagDialect(
        key="vorbis",
        container="flac",
        asin_keys=["ASIN", "asin"],
        note="Vorbis comments are case-insensitive by spec but ffprobe surfaces them "
             "verbatim — so case handling actually matters here.",
    ),
    TagDialect(
        key="none",
        container="mp3",
        asin_keys=[],
        note="No ASIN in any spelling. The common real-world case.",
    ),
]


# --------------------------------------------------------------------------
# Axis 6: clutter — everything in a library that is NOT the book
# --------------------------------------------------------------------------
# CollectCandidates grabs every audio file under the scan root, and
# PathMetadataParser reads desc.txt / reader.txt / cover.* sidecars. Both behaviours
# are exercised here.

CLUTTER: dict[str, str] = {
    "sidecars": "desc.txt, reader.txt, cover.jpg — read by PathMetadataParser",
    "cover-variants": "cover.jpg, folder.png, cover.webp alongside each other",
    "sample-track": "sample.mp3 / preview.mp3 — an AUDIO file that is not the book",
    "intro-outro": "'00 - Intro.mp3', 'Outro.mp3' — audio, not chapters",
    "bonus-content": "'Bonus - Interview with the Author.mp3' — real audio, wrong book",
    "non-audio": "book.pdf, info.nfo, playlist.cue, subtitles.srt",
    "os-detritus": ".DS_Store, Thumbs.db, @eaDir/, .stfolder/ — must be ignored",
    "zero-byte": "a 0-byte .mp3 — ffprobe returns nothing; must not crash the scan",
    "corrupt-audio": "a file with an audio extension that is not audio",
}


# --------------------------------------------------------------------------
# Scenarios: the library-level shapes worth generating whole
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Scenario:
    key: str
    note: str
    layouts: list[str]
    tag_states: list[str]
    expect: str = ""
    extras: dict = field(default_factory=dict)


SCENARIOS: list[Scenario] = [
    Scenario(
        key="happy-path",
        note="The layout Listenarr was built for, tags correct. Everything should link. "
             "If this fails, nothing else is worth reading.",
        layouts=["listenarr-native", "listenarr-native-series"],
        tag_states=["correct-with-asin", "correct-no-asin"],
        expect="100% of files linked",
    ),
    Scenario(
        key="existing-library-adoption",
        note="THE headline bug. A pre-existing library in the common Audnex/Plex layout, "
             "correctly tagged. Listenarr synthesizes BasePath from metadata and dead-ends "
             "when that folder does not exist, so nothing is discovered.",
        layouts=["audnex-plex", "author-series-title"],
        tag_states=["correct-no-asin"],
        expect="100% linked. Listenarr today: 0%.",
    ),
    Scenario(
        key="tag-fallback-rescue",
        note="The case PR #688 targets: layout carries neither title nor author in a form "
             "the path heuristics accept, but the tags are good.",
        layouts=["audiobookshelf", "title-only"],
        tag_states=["correct-with-asin", "correct-no-asin"],
        expect="linked via embedded tags",
    ),
    Scenario(
        key="lying-tags",
        note="The case PR #688 does NOT handle. Tags actively disagree with the folder. "
             "A mis-link is worse than a miss: it silently attaches the wrong book and the "
             "user has no reason to look.",
        layouts=["audnex-plex", "listenarr-native"],
        tag_states=["wrong-title-same-author", "wrong-author", "wrong-asin", "colliding-title"],
        expect="0 files linked via tags; every one surfaced as a conflict",
    ),
    Scenario(
        key="title-collision",
        note="The Haggard trap. Three distinct novels — She / She and Allan / "
             "Ayesha: The Return of She — under one author. A bidirectional Contains "
             "attributes all three to 'She'. The subtitled form must still match.",
        layouts=["audnex-plex", "listenarr-native"],
        tag_states=["colliding-title", "subtitled-title"],
        expect="subtitled-title links; colliding-title does not",
    ),
    Scenario(
        key="author-variance",
        note="Same author, spelled differently across sources. Initials, diacritics, "
             "transliterations, pseudonyms, translators. All false-NEGATIVE risks.",
        layouts=["audnex-plex"],
        tag_states=["author-variant", "translator-as-author", "numeral-variant"],
        expect="all link — rejecting these is a false negative",
    ),
    Scenario(
        key="series-work-key",
        note="Two distinct ASINs for the same work, sharing one series ASIN and position "
             "(A Princess of Mars, 20,000 Leagues, Oz, The Three Musketeers). Demonstrates "
             "that the book ASIN is a manifestation id and (series_asin, position) is the "
             "stable work key. Also covers the Sherlock canon, whose positions really do "
             "run 1,2,3,5,7 because collections occupy the gaps.",
        layouts=["listenarr-native-series"],
        tag_states=["correct-with-asin"],
        expect="the pairs dedupe to one work; series positions survive round-trip",
    ),
    Scenario(
        key="rename-hazards",
        note="THE DESTRUCTIVE ONES. Metadata that is dangerous to write to a filesystem: "
             "path-illegal characters, length bombs, reserved Windows names, unicode "
             "normalization, and path traversal. A matching bug leaves a file unlinked; a "
             "renaming bug LOSES DATA. Generate this, point a rename at it, and confirm "
             "every file still exists afterwards and no path escaped the root.",
        layouts=["listenarr-native", "audnex-plex"],
        tag_states=["correct-no-asin"],
        expect="every hazard is sanitized or refused; NO file is lost, NO path escapes "
               "the library root; a dry run reports exactly what a real run would do",
        extras={"hazards": [h.key for h in PATH_HAZARDS]},
    ),
    Scenario(
        key="multi-file-books",
        note="One book, many files: multi-part, multi-disc subfolders, per-chapter. "
             "Drives CalculateBasePath's common-parent walk — with per-disc subfolders "
             "the files share no direct parent, so BasePath has to climb, and climbing "
             "one level too far swallows a sibling book.",
        layouts=["listenarr-native", "audnex-plex"],
        tag_states=["correct-no-asin", "correct-with-asin"],
        expect="all parts link to ONE book; BasePath is the book folder, never its parent",
        extras={"structures": ["multi-part", "multi-disc", "per-chapter", "nested-single"]},
    ),
    Scenario(
        key="clutter",
        note="Everything in a library that is not the book: samples, intros, bonus tracks, "
             "sidecars, cover art, OS detritus, a zero-byte mp3, a corrupt file with an "
             "audio extension. CollectCandidates grabs EVERY audio file under the root, "
             "so all of this lands in the candidate set.",
        layouts=["listenarr-native", "audnex-plex"],
        tag_states=["correct-no-asin"],
        expect="the book's real files link; samples/intros/bonus do not; sidecars are read; "
               "the zero-byte and corrupt files do not crash or hang the scan",
        extras={"clutter": list(CLUTTER)},
    ),
    Scenario(
        key="tag-dialects",
        note="The same ASIN written in every spelling ffprobe might surface it under, "
             "across m4b/mp3/flac. ExtractAsin carries twenty-odd spellings for a reason; "
             "nothing currently proves it reads them all.",
        layouts=["listenarr-native"],
        tag_states=["correct-with-asin"],
        expect="the ASIN is found in every dialect",
        extras={"dialects": [d.key for d in TAG_DIALECTS]},
    ),
    Scenario(
        key="duplicate-editions",
        note="The same work present twice: two formats, abridged vs unabridged, two "
             "narrators, or the two distinct ASINs that share a series slot. Which wins? "
             "Nothing in the code says.",
        layouts=["audnex-plex"],
        tag_states=["correct-with-asin"],
        expect="the duplicate is DETECTED, not silently double-added or silently dropped",
    ),
    Scenario(
        key="scale",
        note="Volume, not variety. Explodes the corpus into a library of tens of thousands "
             "of files via per-chapter splits. This is the scenario that MEASURES the "
             "ffprobe fan-out cost rather than estimating it: with no BasePath, scanRoot "
             "falls back to the library root and the tag fallback ffprobes every unmatched "
             "file in the library, once per audiobook scanned. Time it.",
        layouts=["audnex-plex"],
        tag_states=["correct-no-asin"],
        expect="scan completes in bounded time; record wall-clock and ffprobe process count",
        extras={"structures": ["per-chapter"], "repeat_factor": 20},
    ),
    Scenario(
        key="mixed-reality",
        note="What an actual library looks like: several layouts side by side, a realistic "
             "spread of tag states, some loose files, some untagged. Nothing is uniform. "
             "This is the integration test.",
        layouts=[layout.key for layout in LAYOUTS],
        tag_states=[state.key for state in TAG_STATES],
        expect="see per-case expectations; report a pass/fail table",
        extras={"tag_state_weights": {
            # roughly matches what a real library looks like: most tags are fine,
            # a meaningful minority actively lie, a few files have nothing at all.
            "correct-no-asin": 0.62,
            "correct-with-asin": 0.05,
            "wrong-title-same-author": 0.11,
            "wrong-author": 0.06,
            "colliding-title": 0.04,
            "subtitled-title": 0.04,
            "author-variant": 0.04,
            "no-tags": 0.03,
            "wrong-asin": 0.01,
        }},
    ),
]


LAYOUTS_BY_KEY = {layout.key: layout for layout in LAYOUTS}
TAG_STATES_BY_KEY = {state.key: state for state in TAG_STATES}
STRUCTURES_BY_KEY = {structure.key: structure for structure in FILE_STRUCTURES}
HAZARDS_BY_KEY = {hazard.key: hazard for hazard in PATH_HAZARDS}
DIALECTS_BY_KEY = {dialect.key: dialect for dialect in TAG_DIALECTS}
SCENARIOS_BY_KEY = {scenario.key: scenario for scenario in SCENARIOS}


if __name__ == "__main__":
    print("axes")
    print(f"  {len(LAYOUTS):>2} layouts          on-disk folder conventions")
    print(f"  {len(TAG_STATES):>2} tag states       what the tags say vs the folder")
    print(f"  {len(FILE_STRUCTURES):>2} file structures  how one book maps to files")
    print(f"  {len(PATH_HAZARDS):>2} path hazards     metadata that is unsafe to write to disk")
    print(f"  {len(TAG_DIALECTS):>2} tag dialects     per-container tag spellings")
    print(f"  {len(CLUTTER):>2} clutter kinds    everything that is not the book")
    print()
    print(f"{len(SCENARIOS)} scenarios")
    for scenario in SCENARIOS:
        print(f"  {scenario.key:28} {scenario.expect[:64]}")
    print()
    print("destructive (a bug here LOSES DATA, it does not merely fail to match):")
    for hazard in PATH_HAZARDS:
        print(f"  {hazard.key:22} {hazard.hazard.splitlines()[0][:58]}")
