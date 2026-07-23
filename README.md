# listenarr-testdata

A generator for synthetic audiobook libraries, built to reproduce and demonstrate scan, match and rename bugs in [Listenarr](https://github.com/Listenarrs/Listenarr) — without anybody having to share a real library.

It is a generator, not a library. The repository holds a manifest of real books and the scripts that lay them out on disk. It synthesizes one-second silent audio files with ffmpeg, writes genuine embedded tags onto them, and arranges them in whichever folder convention the scenario calls for. A library regenerates in seconds; the repo stays a few hundred kilobytes. **No audio is committed, ever.**

The point is that a bug report should not rest on "trust me, my library does this". Clone it, generate the library, point Listenarr at it, and watch the bug happen on your own machine.

> **New:** build a folder shape and get the matching command with the [layout picker](https://m4bard.github.io/listenarr-testdata/).

## Requirements

Linux or macOS (or Windows via WSL2). Validated on **Ubuntu 25.10**. It needs a container
runtime — **podman** (rootless, preferred) or **docker** — plus **bash**, **curl**, **sqlite3**,
**ffmpeg**/**ffprobe**, and **Python 3.11+** with a venv.

On Ubuntu 25.10 the non-default pieces are one line:

```bash
sudo apt install podman curl sqlite3 ffmpeg python3-venv
```

Then the Python side, once:

```bash
python3 -m venv .venv && .venv/bin/pip install -e .
```

`benchmark_scan.sh` checks for each of these at startup and tells you which is missing rather than
failing partway through.

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

For a gate rather than a read, add `--strict` (exit non-zero on any failure) and, for machines, `--json -` or `--junit report.xml`. The JSON carries a `summary.overall` of `pass`, `fail`, or `inconclusive` — the last being a rotted source or an empty scope, so a broken *harness* never reads as a green *scan*. On a run that adds only some of the library (a perf sweep), scope the verdict with `--only-asin` so `--strict` counts only the books you actually scanned.

The verdict folds in the *work-level* assertions too, not just per-file links: a `BasePath` that swallowed a sibling, and duplicate editions of one work that were not deduplicated to a single record (`dedup_problems` in the JSON). A library where every file is linked to the right ASIN can still be wrong at the work level, and `--strict` treats that as a failure rather than a footnote.

## Cross-platform ffprobe validation

Listenarr reads audio metadata by shelling out to one binary — `ffprobe` — exactly once per file
(`ffprobe -v quiet -print_format json -show_format -show_streams`) and consuming a fixed set of
fields. This repository carries a harness for validating and provisioning *that* dependency,
independent of the library generator above.

At its core is **one** provisioning harness, **`tools/ffmpeg_harness.py`**, that everything else
builds on. It is source-agnostic (`johnvansickle` — Listenarr's current Linux-only source — or
`jellyfin`, the one org-maintained source covering every platform Listenarr ships) and
binary-agnostic (`ffmpeg` to *create* fixture audio, or `ffprobe` to *read* metadata — both ride in
a single pinned archive). It verifies each archive against a recorded sha256 **before** extraction,
so a rolled or tampered build raises and never unpacks, and caches the extracted binary. This repo
dogfoods it both ways: the generator pulls **ffmpeg** through it to synthesize fixtures, and it
provisions **ffprobe** into the config of the Listenarr container the benchmark runs against — so
that container finds it (`File.Exists`) and skips its own unpinned first-boot download, keeping the
benchmark deterministic and race-free.

- **`tools/ffmpeg_harness.py`** — the shared provisioner described above. `--verify` re-downloads a
  source's pins and re-checks their sha256 to catch upstream drift (johnvansickle rolls its
  "release" build by design; jellyfin assets are immutable per tag).
- **`tools/ffprobe_provisioner.py`** — a thin wrapper that provisions ffprobe via the harness and
  drops it at `<config>/ffmpeg/ffprobe`, so Listenarr finds it (`File.Exists`) and skips its own
  unpinned first-boot download — removing both the download race and the run-to-run non-determinism.
- **`tools/ffprobe_equivalence.py`** runs Listenarr's exact ffprobe command against a fixed
  corpus covering every supported format (m4b/mp3/flac/ogg/opus/m4a/aac/wav) and compares only the
  fields `FfprobeMetadataMapper` reads — so "does this ffprobe behave the way Listenarr needs"
  becomes a precise, automatable check rather than a guess. Works for *any* ffprobe source.
- **`.github/workflows/ffprobe-cross-platform.yml`** runs that check on every platform Listenarr
  ships for — linux-x64, linux-arm64, win-x64, and osx-x64 (the last validated under Rosetta on the
  Apple-Silicon runner, which is how Listenarr runs on Apple Silicon) — against both the current
  source and jellyfin-ffmpeg, and reports per-platform outcomes.
- **`tools/package_ffbinary.py`** packages just one binary for each RID via the shared harness —
  `--program ffprobe` (Listenarr's need) or `--program ffmpeg` — verifying each archive against its
  sha256 **before** extraction and emitting a `manifest.json` that records both the archive and
  extracted-binary hashes. `--verify` re-checks the live release against the pins to catch upstream
  drift; `--zip` emits a per-platform `<program>-<rid>.zip` (binary + manifest) — the shape a release
  ships.
- **`.github/workflows/release.yml`** cuts a SemVer release of this repo's own binary — the pinned,
  verified **ffmpeg** it uses to synthesize fixtures: push a tag `vX.Y.Z` and it attaches
  `ffmpeg-<rid>.zip` for every RID plus `manifest.json` as release assets. (ffprobe isn't published
  here — this repo only uses it to provision the Listenarr container it benchmarks;
  `package_ffbinary.py --program ffprobe` can emit ffprobe bundles on demand for anyone who wants
  them.) `workflow_dispatch` builds the bundles as run artifacts without publishing, for a dry run.
- **`tools/install_ffbinary.py`** consumes a release the other way: it detects the host RID,
  downloads the matching `<program>-<rid>.zip` from a release (a pinned `--tag` or `latest`), verifies
  the binary against the `manifest.json` sha256 inside the zip, and drops it into `--dest`. GitHub
  release asset URLs are stable, so it's a reliable, self-contained way to fetch exactly the pinned
  binary a release published — no live upstream fetch.

## Library layouts — generate one that matches your tool

`--layout <name>` produces a library in a single on-disk convention, so you can mirror whatever
tool you actually run instead of describing a layout by hand. `--list-layouts` prints the full
menu; the provenance for each convention is a permalink in the `source` field of `corpus/cases.py`,
not just this table.

Not sure which shape you have? The **[layout picker](https://m4bard.github.io/listenarr-testdata/)**
lets you build a folder pattern, see an example path, and get the matching `--layout` command.

Example paths use one book — *A Princess of Mars* (Edgar Rice Burroughs, Barsoom #1):

| Your tool | `--layout` | Example path | Source |
|---|---|---|---|
| **Listenarr** (default) | `listenarr` | `Edgar Rice Burroughs/Barsoom/A Princess of Mars/A Princess of Mars.m4b` | [code](https://github.com/Listenarrs/Listenarr/blob/4555ad21e3c455ae3963836e55693207cea66d12/listenarr.domain/Configuration/ApplicationSettings.cs#L33) |
| **AudioBookShelf** (series) | `audiobookshelf-series` | same as Listenarr | [docs](https://audiobookshelf.org/docs/documentation/libraries/book-library/directory-structure/) |
| **AudioBookShelf** (flat) | `audiobookshelf-flat` | `Edgar Rice Burroughs/A Princess of Mars/A Princess of Mars.m4b` | [docs](https://audiobookshelf.org/docs/documentation/libraries/book-library/directory-structure/) |
| **Readarr** (retired) | `readarr` | `Edgar Rice Burroughs/A Princess of Mars/…` (folder shape) | [code](https://github.com/Readarr/Readarr/blob/develop/src/NzbDrone.Core/Organizer/NamingConfig.cs) |
| **Plex** (community) | `plex-community` | `Edgar Rice Burroughs/Edgar Rice Burroughs - Barsoom - A Princess of Mars/…` | [guide](https://github.com/seanap/Plex-Audiobook-Guide) |
| **AudioBookShelf** (chaptered) | `audiobookshelf` | `Edgar Rice Burroughs/Barsoom/1 - A Princess of Mars/…` | [docs](https://audiobookshelf.org/docs/documentation/libraries/book-library/directory-structure/) |

```bash
# a library in Listenarr's own layout, from nothing:
python3 tools/generate_library.py --layout listenarr --out ./build/lib
```

Honest caveats: **AudioBookShelf** documents *several* shapes (series and flat), so it has no
single default — pick the one you use. **Plex** has no native audiobook type; the layout is a
community convention, and different guides disagree. **Readarr** doesn't rename by default, and
the harness models its *folder* shape, not its per-file naming. **Audnexus** is a metadata API
with no layout at all — the `plex-community` shape is often paired with it but not defined by it.

### Trickiest common format

`--layout audiobookshelf` combined with per-chapter files (`001 - Chapter 1.mp3`) is the one that
breaks tools in practice: the filename carries neither title nor author, so path heuristics find
nothing; folder + author-in-path triggers over-attribution (a scan of one book claims the author's
siblings); and only embedded tags can identify it — which disagree with the folder roughly one
book in six. It's the shape that forces a scanner to combine signals and cross-check them rather
than trust any single one. If you exercise one adversarial layout, exercise this.

## Test a specific Listenarr branch or PR

`vet-against.sh` builds any branch from source and runs the harness against it in one command:

```bash
./tools/vet-against.sh --branch bugfix/unix-folder-name-space --layout listenarr --no-basepath
```

It clones the branch, builds a container image tagged by commit (reused if already built), scans
a generated library against it, and drops the clone (the image is cached, the clone is not). Any
flag it doesn't recognise is forwarded to `benchmark_scan.sh` — `--layout`, `--scenario`,
`--books`, `--no-basepath`, `--keep`. Pass `--dry-run` to print the plan without touching anything,
or `--repo URL` to build a fork.

Under the hood it is just clone → `podman build` → `benchmark_scan.sh --image …`; run those by
hand if you prefer.

`--no-basepath` clears each book's BasePath so the scan root falls back to the library root —
the state that exercises discovery and attribution. The run reports per-book scan cost and flags
any BasePath that climbed past its own book folder, e.g.:

```
BasePath '/audiobooks/Arthur Conan Doyle' is shared by 2 books — it climbed past the book folder and swallowed a sibling
```

The library mounts read-write, matching a real deployment (Listenarr organizes files in its
roots); determinism comes from regenerating the library from a fixed `--seed`, not from an
immutable mount.

### Supported API versions

The harness drives two Listenarr API shapes from one code path — current `canary` and the
versioned API introduced by [Listenarr#717](https://github.com/Listenarrs/Listenarr/pull/717) —
with no version detection. Two differences, both settled empirically against real images rather
than assumed:

| | canary | #717 (versioned) |
|---|---|---|
| route base | `/api/v1` responds | `/api/v1` responds |
| root-folder create, read-only mount | 201 (no probe) | 400 unless `caseSensitivityMode` is sent |
| scan, read-only mount | 202 | **500** — its hardened scan needs write access |

Sending `caseSensitivityMode: "Sensitive"` on root-folder create satisfies #717 and is ignored by
canary; mounting the library read-write satisfies #717's scan and is fine for canary. So one
payload and one mount mode cover both — which is why this is a thin compatibility fix, not a
version-detecting adapter.

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

Generation is deterministic where it counts. The same `--seed` regenerates the same *shape* on any machine — identical folder names, file names, embedded tag values, and manifest — because all of that comes from seeded Python, not the environment. So a maintainer and a reporter running the same seed are looking at the same library in every respect a scan or a rename can observe. The one thing that is **not** guaranteed byte-for-byte across machines is the audio payload itself: it is silence synthesized by ffmpeg, and different ffmpeg builds emit slightly different encoder padding and metadata. If you need the media bytes to match too (rarely — the tags and paths are what scanners read), pin the ffmpeg version.

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

One caveat worth stating plainly, because a green audit is easy to over-read: **a clean run on Linux proves POSIX-safety, not Windows or macOS safety.** Several of these hazards only *manifest* on the filesystem they target. A case collision (`She` vs `she`) does not destroy anything on a case-sensitive ext4 — both files coexist — but silently overwrites on case-insensitive NTFS or APFS. A reserved name like `CON`, or a component that overflows `MAX_PATH`, is a perfectly legal filename on Linux and only fails on Windows. So the generator faithfully *writes* every hazard on any host, and the data-loss and traversal checks (which are filesystem-agnostic) are meaningful everywhere — but the case-collision and reserved-name outcomes are only exercised when the audit runs on a case-insensitive or Windows filesystem. Run it there too before calling a renamer safe for those platforms.

That last one deserves saying plainly: **embedded tags are attacker-controlled input.** A title of `../../../../etc` interpolated into a rename target escapes the library root. The generator writes exactly that string into a real tag, so you can find out what your renamer does with it.

## Layout

```
corpus/corpus.json          123 verified books, generated — do not hand-edit
corpus/cases.py             the six axes and fourteen scenarios. Start here.
tools/build_corpus.py       fetches and verifies every ASIN against live metadata
tools/generate_library.py   the generator
tools/verify_scan.py        expected vs observed; the rename audit; --json/--junit
tools/conformance_diff.py   A/B two --json reports: what a branch fixed and regressed
tests/                      the test suite
TESTING.md                  how the suite is organised; the verdict-contract convention
```

Requires Python 3.11+, ffmpeg and ffprobe on `PATH`, and `mutagen`. Development extras (`pip install -e '.[dev]'`) add pytest, ruff and mypy; `python -m pytest` runs the suite. This is a conformance harness, so a subset of the tests are adversarial against the tool's own verdict — see [TESTING.md](TESTING.md) and `pytest -m contract`.

## Provenance and licence

The code is MIT. The metadata — titles, authors, narrators, ASINs, series positions — is factual, and is fetched from Audnex rather than authored here. The books themselves are in the public domain, and their recordings are freely available from LibriVox.

The generated audio is one second of digital silence, synthesized on your machine at generation time. It is not a recording of anything.
