// Pure layout logic, shared by the picker UI (index.html) and the node test.
// No DOM, no dependencies — so it runs identically in the browser and under `node --test`.
//
// Keep the PRESETS in step with corpus/cases.py (LAYOUTS + LAYOUT_ALIASES). The folder patterns
// here are the canonical shapes; the `layout` field is the --layout name to pass the generator.

// Illustrative example values for the preview — factual where possible (real book, real ASIN,
// LibriVox as a public-domain publisher), representative for the rest.
export const EXAMPLE = {
  Author: "Edgar Rice Burroughs",
  Series: "Barsoom",
  SeriesNumber: "1",
  Title: "A Princess of Mars",
  Subtitle: "Barsoom, Book 1",
  Year: "1912",
  Narrator: "Scott Brick",
  Publisher: "LibriVox",
  Edition: "Unabridged",
  Language: "English",
  Asin: "B071YLS9YL",
  Quality: "MP3 128kbps",
  DiskNumber: "1",
  ChapterNumber: "1",
};

// The full Listenarr token vocabulary (FileNamingService.Helpers.cs). {DiskNumber}/{ChapterNumber}
// accept a zero-pad format, e.g. {ChapterNumber:00}. Other tools (Readarr etc.) use different token
// names — see their source in the README menu.
export const TOKENS_SOURCE =
  "https://github.com/Listenarrs/Listenarr/blob/4555ad21e3c455ae3963836e55693207cea66d12/" +
  "listenarr.application/Common/FileNamingService.Helpers.cs";

export const TOKENS = [
  { token: "{Author}", desc: "author" },
  { token: "{Series}", desc: "series name" },
  { token: "{SeriesNumber}", desc: "position in the series" },
  { token: "{Title}", desc: "book title" },
  { token: "{Subtitle}", desc: "subtitle" },
  { token: "{Year}", desc: "publish year" },
  { token: "{Narrator}", desc: "narrator" },
  { token: "{Publisher}", desc: "publisher" },
  { token: "{Edition}", desc: "edition" },
  { token: "{Language}", desc: "language" },
  { token: "{Asin}", desc: "Audible ASIN" },
  { token: "{Quality}", desc: "quality / format" },
  { token: "{DiskNumber}", desc: "disk number — pad with {DiskNumber:00}" },
  { token: "{ChapterNumber}", desc: "chapter/track number — pad with {ChapterNumber:00}" },
];

// Canonical folder shapes. `layout` is the value for `--layout` / `vet-against.sh --layout`.
export const PRESETS = [
  {
    tool: "Listenarr (default) · AudioBookShelf (series)",
    layout: "listenarr",
    folder: "{Author}/{Series}/{Title}",
    source: "https://github.com/Listenarrs/Listenarr/blob/4555ad21e3c455ae3963836e55693207cea66d12/listenarr.domain/Configuration/ApplicationSettings.cs#L33",
  },
  {
    tool: "AudioBookShelf (flat) · Readarr (folder)",
    layout: "author-title",
    folder: "{Author}/{Title}",
    source: "https://audiobookshelf.org/docs/documentation/libraries/book-library/directory-structure/",
  },
  {
    tool: "Plex (community)",
    layout: "plex-community",
    folder: "{Author}/{Author} - {Series} - {Title}",
    source: "https://github.com/seanap/Plex-Audiobook-Guide",
  },
  {
    tool: "AudioBookShelf (chaptered) — the trickiest common format",
    layout: "audiobookshelf",
    folder: "{Author}/{Series}/{SeriesNumber} - {Title}",
    source: "https://audiobookshelf.org/docs/documentation/libraries/book-library/directory-structure/",
  },
  {
    tool: "Listenarr parser input (the one shape it currently parses)",
    layout: "listenarr-native",
    folder: "{Author}/{Year} - {Title}",
    source: "https://github.com/Listenarrs/Listenarr/blob/canary/listenarr.infrastructure/Metadata/Parsing/PathMetadataParser.cs",
  },
];

// Collapse whitespace and lowercase so "{Author}/{Series}/{Title}" matches "{author} / {series}/{title}".
export function normalizePattern(pattern) {
  return pattern.replace(/\s*\/\s*/g, "/").replace(/\s+/g, " ").trim().toLowerCase();
}

// Substitute the example book into any pattern, honouring a zero-pad format ({ChapterNumber:00}).
// Unknown tokens are left verbatim rather than silently dropped, so a typo is visible.
export function renderPattern(pattern, book = EXAMPLE) {
  // {Token} or {Token:00} — the run of zeros is the pad width, matching Listenarr's format.
  return pattern.replace(/\{(\w+)(?::(0+))?\}/g, (match, name, zeros) => {
    const value = book[name];
    if (value === undefined) return match;
    return zeros ? String(value).padStart(zeros.length, "0") : String(value);
  });
}

// A folder pattern rendered into an example path, with a {Title}.ext leaf.
export function renderPath(folderPattern, book = EXAMPLE, ext = "m4b") {
  const folder = renderPattern(folderPattern, book).replace(/\/+/g, "/").replace(/^\/|\/$/g, "");
  return `${folder}/${book.Title}.${ext}`;
}

// Which preset (if any) a folder pattern matches, ignoring case and whitespace.
export function matchPreset(folderPattern) {
  const target = normalizePattern(folderPattern);
  return PRESETS.find((p) => normalizePattern(p.folder) === target) || null;
}

// The command a user should run for a given folder pattern: a --layout if it matches a preset,
// otherwise an honest note that a custom shape has no preset (yet).
export function commandFor(folderPattern) {
  const preset = matchPreset(folderPattern);
  if (preset) {
    return `python3 tools/generate_library.py --layout ${preset.layout} --out ./build/lib`;
  }
  return "# custom shape — no --layout preset matches it. Closest presets are listed above.";
}
