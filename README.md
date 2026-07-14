# listenarr-testdata

A generator for synthetic audiobook libraries, built to reproduce and demonstrate scan, match and rename bugs in [Listenarr](https://github.com/Listenarrs/Listenarr) — without anybody having to share a real library.

It is a generator, not a library. The repository holds a manifest of real books and the scripts that lay them out on disk. It synthesizes one-second silent audio files with ffmpeg, writes genuine embedded tags onto them, and arranges them in whichever folder convention the scenario calls for. A library regenerates in seconds; the repo stays a few hundred kilobytes. **No audio is committed, ever.**

The point is that a bug report should not rest on "trust me, my library does this". Clone it, generate the library, point Listenarr at it, and watch the bug happen on your own machine.

## Reproduce a bug in four commands

```bash
git clone https://github.com/m4bard/listenarr-testdata && cd listenarr-testdata
python3 -m venv .venv && .venv/bin/pip install -e .

# an ordinary pre-existing library: the Audnex/Plex layout, correctly tagged
.venv/bin/python tools/generate_library.py \
    --scenario existing-library-adoption --out ./build/library --seed 1

# point Listenarr at ./build/library, run a scan, then:
.venv/bin/python tools/verify_scan.py \
    --manifest ./build/library/manifest.json --db /path/to/listenarr.db
```

`verify_scan.py` prints the expected outcome against the observed one, per case. Every book in this scenario is correctly tagged and sitting in a perfectly ordinary folder structure, so every one of them should link. If none of them is discovered, the table says so and says nothing else:

```
scenario   existing-library-adoption
expect     100% linked. Listenarr today: 0%.
observed   0 linked files

layout                     case                        pass  fail  outcome
----------------------------------------------------------------------------
audnex-plex                correct-no-asin                0   100  FAIL  link — title+author agree
author-series-title        correct-no-asin                0    23  FAIL  link — title+author agree
----------------------------------------------------------------------------
TOTAL                                                     0   123
```

The claim being tested is that this is not a *partial* failure: a correctly-tagged library in a common third-party layout is not partly discovered, it is not discovered at all. Run it against your own build and find out — that is what the repository is for.

## Why the data is trustworthy

Every book is real, in the public domain, and has audio freely available from LibriVox. Every ASIN in `corpus/corpus.json` is **machine-verified against live Audible metadata** — `tools/build_corpus.py` fetches each one from [Audnex](https://api.audnex.us), checks it resolves to the book we expected, and refuses to write an entry that does not. No ASIN in this repository was ever typed by hand or taken on trust; a plausible-looking `B0XXXXXXXX` is trivial to invent and impossible to spot by eye.

Re-verify the whole corpus against reality at any time:

```bash
python3 tools/build_corpus.py --check   # non-zero exit if anything drifted or died
```

It has already earned its keep: it caught an ASIN that had gone dead after previously being reported as verified.

The corpus is 123 public-domain works covering 49 distinct failure modes, plus two region-lock proofs.

## The finding worth leading with

Four public-domain works each have **two distinct book ASINs that share one series ASIN and one series position**:

| Work | ASIN A | ASIN B | Series + position |
|---|---|---|---|
| A Princess of Mars | `B008DFUGCQ` | `B071YLS9YL` | Barsoom (`B007D0J4H0`) #1 |
| 20,000 Leagues Under the Sea | `B01FKWL15A` | `B076HSP1FT` | Captain Nemo (`B09CLW5RN4`) #1 |
| The Wonderful Wizard of Oz | `B007BR5KZA` | `B002V5CJM4` | Oz (`B005NAUFS4`) #1 |
| The Three Musketeers | `B002UZJF4U` | `B002V0RG8G` | Musketeers Cycle (`B007C4SDU6`) #1 |

The Verne pair differs *only* in numeral style — "Twenty Thousand" versus "20,000". Both are live, current Audible editions.

So **the book ASIN is a manifestation id, not a work id**, and `(series_asin, position)` is the stable work key. The same conclusion arrives from the other direction too: the corpus contains one German work present under two ASINs, one per marketplace, and each ASIN returns a 404 outside its own region. An identifier that changes per storefront cannot identify a work.

None of this is asserted on trust. `build_corpus.py` re-derives it from live metadata, and asserts the region-lock rather than assuming it.

Also live-verified, and worth its own scenario: Audnex reports the Sherlock Holmes canon at positions **1, 2, 3, 5, 7**. The short-story collections occupy slots 4 and 6, so publication order and series position genuinely diverge. Code that assumes contiguous positions is wrong about the real world, not merely about this corpus.

## What gets generated

Six axes, composed into fourteen scenarios. `python3 corpus/cases.py` prints the matrix; each scenario declares the outcome a **correct** scanner should reach, which is what makes a generated tree a conformance suite rather than a pile of files.

| Axis | What it varies |
|---|---|
| **Layouts** (8) | Folder conventions: the native `{Year} - {Title}`, Audnex/Plex, AudioBookShelf, flat, loose files, title-only |
| **Tag states** (11) | What the tags say *relative to the folder* — correct, absent, or actively lying |
| **File structures** (5) | One book to many files: multi-part, multi-disc subfolders, per-chapter, buried single |
| **Path hazards** (15) | Metadata that is dangerous to write to a filesystem |
| **Tag dialects** (5) | The same ASIN as an iTunes atom, a `TXXX` frame, a Vorbis comment |
| **Clutter** (9) | Everything that is not the book: samples, intros, sidecars, cover art, OS detritus |

A few scenarios worth knowing about:

- **`existing-library-adoption`** — the headline bug above.
- **`lying-tags`** — the case a tag-fallback does *not* handle. The folder is right and the tags disagree with it. A mis-link is worse than a miss: it silently attaches the wrong book, and the user has no reason to go looking.
- **`title-collision`** — the Haggard trap. *She* and *She And Allan* are two distinct novels by one author, and each title contains the other. A bidirectional `Contains` attributes both to the same work, and because the author agrees, the author check cannot arbitrate. Note that the canonical title is *She: A History of Adventure* — Audnex folds the subtitle in — so the collision only appears on the base title, which is also the only comparison available against a folder, because nobody names a folder *She: A History of Adventure*. That cuts both ways: folder *She* with a tag reading *She: A History of Adventure* is a **true** match, so a fix that simply tightens containment until the collision goes away will break it.
- **`rename-hazards`** — the destructive one. See below.
- **`scale`** — volume rather than variety: ~98,000 files. This is the scenario that lets you *measure* the ffprobe fan-out instead of estimating it.

Everything is deterministic. The same `--seed` regenerates a byte-identical tree, so a maintainer and a reporter can be certain they are looking at the same library.

## The destructive axis

Most scan bugs leave a file unlinked, which is annoying. A rename bug destroys data, and the two are often one line apart in the same code path. So the hazard cases are generated for real and the rename is audited:

```bash
.venv/bin/python tools/generate_library.py --scenario rename-hazards --out ./build/hz --seed 1
.venv/bin/python tools/verify_scan.py --manifest ./build/hz/manifest.json --snapshot before.json

# ... now run Listenarr's rename against ./build/hz ...

.venv/bin/python tools/verify_scan.py --manifest ./build/hz/manifest.json --audit before.json
```

The audit asserts that **no file was lost and no path escaped the library root**. Files are tracked by content, not by name — a rename is precisely a change of name, so the question is not whether a given path still exists but whether every byte that was there still exists somewhere under the root.

The hazards are the ones that really bite: path-illegal characters, the 255-**byte** component limit (bytes, not characters — a Cyrillic or CJK title overflows at roughly a third of the character count you would expect), Windows `MAX_PATH`, reserved device names like `CON`, NFC/NFD normalization, case collisions, and **path traversal**.

That last one deserves saying plainly: **embedded tags are attacker-controlled input.** A title of `../../../../etc` interpolated into a rename target escapes the library root. The generator writes exactly that string into a real tag, so you can find out what your renamer does with it.

## Layout

```
corpus/corpus.json          123 verified books, generated — do not hand-edit
corpus/cases.py             the six axes and fourteen scenarios. Start here.
tools/build_corpus.py       fetches and verifies every ASIN against live metadata
tools/generate_library.py   the generator
tools/verify_scan.py        expected vs observed; the rename audit
tests/                      the test suite
```

Requires Python 3.11+, ffmpeg and ffprobe on `PATH`, and `mutagen`. Development extras (`pip install -e '.[dev]'`) add pytest, ruff and mypy; `python -m pytest` runs the suite.

## Provenance and licence

The code is MIT. The metadata — titles, authors, narrators, ASINs, series positions — is factual, and is fetched from Audnex rather than authored here. The books themselves are in the public domain, and their recordings are freely available from LibriVox.

The generated audio is one second of digital silence, synthesized on your machine at generation time. It is not a recording of anything.
