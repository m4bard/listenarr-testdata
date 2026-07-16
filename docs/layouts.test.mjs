// Tests for the picker's pure logic. Run with `node --test docs/` (no npm dependencies).
import test from "node:test";
import assert from "node:assert/strict";

import {
  EXAMPLE,
  PRESETS,
  TOKENS,
  normalizePattern,
  renderPattern,
  renderPath,
  matchPreset,
  commandFor,
} from "./layouts.js";

test("renderPath substitutes the example book into a folder pattern", () => {
  assert.equal(
    renderPath("{Author}/{Series}/{Title}"),
    "Edgar Rice Burroughs/Barsoom/A Princess of Mars/A Princess of Mars.m4b",
  );
});

test("renderPath handles the flat and author-dash shapes", () => {
  assert.equal(
    renderPath("{Author}/{Title}"),
    "Edgar Rice Burroughs/A Princess of Mars/A Princess of Mars.m4b",
  );
  assert.equal(
    renderPath("{Author}/{Author} - {Series} - {Title}"),
    "Edgar Rice Burroughs/Edgar Rice Burroughs - Barsoom - A Princess of Mars/A Princess of Mars.m4b",
  );
});

test("renderPath renders the chaptered (numbered) shape", () => {
  assert.equal(
    renderPath("{Author}/{Series}/{SeriesNumber} - {Title}"),
    "Edgar Rice Burroughs/Barsoom/1 - A Princess of Mars/A Princess of Mars.m4b",
  );
});

test("normalizePattern ignores case and whitespace around slashes", () => {
  assert.equal(
    normalizePattern("{Author} / {Series}/{Title}"),
    normalizePattern("{author}/{series}/{title}"),
  );
});

test("matchPreset identifies the Listenarr default from an equivalent pattern", () => {
  const preset = matchPreset("{author}/{series}/{title}");
  assert.ok(preset);
  assert.equal(preset.layout, "listenarr");
});

test("matchPreset returns null for a shape no preset defines", () => {
  assert.equal(matchPreset("{Year}/{Title}/{Author}"), null);
});

test("commandFor emits a --layout for a known shape and a note for a custom one", () => {
  assert.match(commandFor("{Author}/{Series}/{Title}"), /--layout listenarr\b/);
  assert.match(commandFor("{Year}/{Title}/{Author}"), /custom shape/);
});

test("every preset renders a non-empty example path and has a source URL", () => {
  for (const preset of PRESETS) {
    assert.ok(renderPath(preset.folder).length > 0, preset.layout);
    assert.match(preset.source, /^https:\/\//, preset.layout);
  }
});

test("every offered token has an example value, so the preview never shows a raw {Token}", () => {
  for (const { token } of TOKENS) {
    const name = token.slice(1, -1); // {Author} -> Author
    assert.ok(EXAMPLE[name], `no example value for ${name}`);
  }
});

test("renderPattern fills the full Listenarr token set with no leftovers", () => {
  const rendered = renderPattern(
    "{Author}|{Series}|{SeriesNumber}|{Title}|{Subtitle}|{Year}|{Narrator}|" +
    "{Publisher}|{Edition}|{Language}|{Asin}|{Quality}|{DiskNumber}|{ChapterNumber}",
  );
  assert.ok(!rendered.includes("{"), rendered);
});

test("renderPattern honours a zero-pad format specifier", () => {
  assert.equal(renderPattern("{ChapterNumber:00}"), "01");
  assert.equal(renderPattern("{DiskNumber:000}"), "001");
});

test("renderPattern leaves an unknown token verbatim rather than dropping it", () => {
  assert.equal(renderPattern("{Nonsense}/{Title}"), "{Nonsense}/A Princess of Mars");
});
