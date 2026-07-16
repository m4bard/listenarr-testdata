// Pure layout logic, shared by the picker UI (index.html) and the node test.
// No DOM, no dependencies — so it runs identically in the browser and under `node --test`.
//
// Keep the PRESETS in step with corpus/cases.py (LAYOUTS + LAYOUT_ALIASES). The folder patterns
// here are the canonical shapes; the `layout` field is the --layout name to pass the generator.

export const EXAMPLE = {
  author: "Edgar Rice Burroughs",
  series: "Barsoom",
  seriesNumber: "1",
  title: "A Princess of Mars",
  year: "1912",
};

export const TOKENS = ["{Author}", "{Series}", "{SeriesNumber}", "{Title}", "{Year}"];

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

// Substitute the example book into a folder pattern; a {Series}-less book would collapse its
// separators, but the example has a series so this renders every token.
export function renderPath(folderPattern, book = EXAMPLE, ext = "m4b") {
  const values = {
    "{Author}": book.author,
    "{Series}": book.series,
    "{SeriesNumber}": book.seriesNumber,
    "{Title}": book.title,
    "{Year}": book.year,
  };
  let folder = folderPattern;
  for (const [token, value] of Object.entries(values)) {
    folder = folder.split(token).join(value);
  }
  folder = folder.replace(/\/+/g, "/").replace(/^\/|\/$/g, "");
  return `${folder}/${book.title}.${ext}`;
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
