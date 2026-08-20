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

One thing to know before the first generation: the generator does **not** use the ffmpeg on `PATH`
by default. It provisions a pinned, sha256-verified build through `tools/ffmpeg_harness.py`
(source `jellyfin`), caching it under `build/ffmpeg-cache`, so the first run on a fresh clone
downloads once and every run after that is offline. That is the repo dogfooding its own
provisioner. If you would rather use the ffmpeg you already have, pass
`--ffmpeg-source system`; `--ffmpeg-source johnvansickle` provisions from Listenarr's current
Linux-only source instead.

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
- **`tools/install_ffbinary.py`** consumes a release the other way: it detects the host RID, looks up
  the asset's sha256 from GitHub's public Releases API (the `digest` field), downloads the matching
  `<program>-<rid>.zip` (a pinned `--tag` or `latest`), and **refuses it unless the download matches
  that digest** — an out-of-band integrity check, fetched separately from the bytes rather than read
  from the zip's own manifest. It then re-checks the extracted binary against the in-zip
  `manifest.json` (defense in depth) and drops it into `--dest`.

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
.venv/bin/python tools/generate_library.py --layout listenarr --out ./build/lib
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
flag it doesn't recognise is forwarded verbatim to whichever tool `--tool` selected, which by
default is `benchmark_scan.sh` — `--layout`, `--scenario`, `--books`, `--no-basepath`, `--keep`.
Pass `--dry-run` to print the plan without touching anything, or `--repo URL` to build a fork.

### Does every patch branch still build, and does its test project?

**`verify_patch_branches.sh`** is the odd one out in this repo: it generates no library and touches
no container. It takes a Listenarr checkout, walks its worktrees, and reports for each branch
whether the solution builds, whether the **test project** builds, and whether the suite passes,
plus `vue-tsc` and vitest for branches touching `fe/`.

```bash
./tools/verify_patch_branches.sh --repo /path/to/a/listenarr-clone
./tools/verify_patch_branches.sh --repo /path/to/clone --only bug12
```

The two builds are reported separately because they fail separately, and that is the whole reason
this exists. Adding a required parameter to a method with call sites under `tests/` produces an
image that builds, starts and passes every runtime check here, while `dotnet test` refuses to
compile. A container build publishes; it never compiles tests, so it cannot notice. That reached a
finished branch once, which is once more than it should have. The summary calls that case
`NOBUILD` rather than folding it in with an ordinary failure.

It runs a baseline commit through the same lanes, because a failure count means nothing without the
count without the patch, and a lane it cannot run reports `SKIP` and never a pass: an absent
`node_modules` is a missing prerequisite, not a green frontend.

A suite failure is re-run in isolation before it is called one. This suite contains wall-clock
guardrail tests, and one of them came in at 1021ms against a 1000ms threshold on a loaded machine
and passed at 272ms alone. Anything that passes when re-run by itself is reported as `flaky`, so a
number that moved because the box was busy does not read as a regression.

Under the hood it is just clone → `podman build` → `benchmark_scan.sh --image …`; run those by
hand if you prefer.

### Check what a scan actually claims

`--tool attribution` swaps the benchmark for `validate_scan_attribution.sh`, which answers a
different question: of the files a scan linked to a book, how many really belong to it?
`--tool grouping` runs `validate_chapter_grouping.sh` against the built image instead.

```bash
./tools/vet-against.sh \
    --repo https://github.com/<owner>/Listenarr.git --branch <your-branch> \
    --tool attribution \
    --asin B004FOLXEO --layout author-title \
    --only-asin B004FOLXEO,B01ATTZF38,B0C6FJ6L34
```

That builds the branch, generates a library holding those three books, adds only the first one,
clears its BasePath so the scan walks the whole library root, scans, and then maps every linked
file back to its true owner using the generator's manifest. Exit `0` means the scan claimed only
its own files, `1` means it claimed a file belonging to another book, and `2` means the result
could not be judged.

Those three ASINs are M. R. James' *Ghost Stories of an Antiquary*, Henry James' *The Turn of the
Screw*, and James M. Barrie's *Peter Pan in Kensington Gardens*: three different authors whose
names overlap, which is what makes them useful for testing author matching. Use `--layout
author-title` for standalone books, because the default `{author}/{series}/{title}` cannot render
a book with no series and will silently skip it.

Swap in any ASIN from `corpus/corpus.json`, or drop `--only-asin` to put the whole corpus on disk.

`--no-basepath` clears each book's BasePath so the scan root falls back to the library root —
the state that exercises discovery and attribution. The run reports per-book scan cost and flags
any BasePath that climbed past its own book folder, e.g.:

```
BasePath '/audiobooks/Arthur Conan Doyle' is shared by 2 books — it climbed past the book folder and swallowed a sibling
```

The library mounts read-write, matching a real deployment (Listenarr organizes files in its
roots); determinism comes from regenerating the library from a fixed `--seed`, not from an
immutable mount.

### The narrower runtime checks

`vet-against.sh` builds an image and hands it to one of two tools. The remaining runtime checks skip
the build and take an image directly, either a `listenarr-vet:<sha>` you already built or a
published tag. Each asserts a single behaviour end to end against a running container rather than
timing or scoring a whole scan, and each provisions a pinned ffprobe first so the metadata step does
not lose the first-boot download race.

To answer "what did this release change?" rather than one question at a time, **`regression_sweep.sh`**
runs a set of them against one image and tabulates the verdicts. Every check generates its own
library, provisions its own config, and starts its own container on its own port, so nothing outside
`build/` is touched and the run is repeatable by anyone with the repo.

It reports the exit codes rather than summing them, because they are deliberately not uniform: `0`
passed, `1` the behaviour under test is wrong, `2` the run could not be judged. **A `2` is not a
pass**, and folding it into one is how a sweep starts lying about coverage. For the same reason its
header names the checks it does *not* run and why, since one that quietly covers less than its name
suggests is worse than one that covers little and admits it.

```bash
./tools/regression_sweep.sh --image ghcr.io/listenarrs/listenarr:canary
./tools/regression_sweep.sh --image ghcr.io/listenarrs/listenarr:canary --only companion-import
```

```bash
./tools/validate_reported_size.sh --image ghcr.io/listenarrs/listenarr:canary
./tools/validate_import_action.sh localhost/listenarr-vet:abc1234 --action symlink
./tools/validate_asin_tag_embed.sh --image ghcr.io/listenarrs/listenarr:canary
./tools/validate_sidecar_rename.sh localhost/listenarr-vet:abc1234
./tools/validate_companion_import.sh --image ghcr.io/listenarrs/listenarr:canary
./tools/check_duplicate_detection.sh ghcr.io/listenarrs/listenarr:canary
./tools/validate_import_destination.sh --image ghcr.io/listenarrs/listenarr:canary
./tools/validate_author_cache_variants.sh --all
./tools/validate_import_relisting.sh --image ghcr.io/listenarrs/listenarr:canary
./tools/validate_root_folder_delete.sh --image ghcr.io/listenarrs/listenarr:canary
./tools/validate_rename_hazards.sh --image ghcr.io/listenarrs/listenarr:canary
./tools/validate_metadata_fallback.sh --image ghcr.io/listenarrs/listenarr:canary
./tools/validate_chapter_grouping.sh --image ghcr.io/listenarrs/listenarr:canary
./tools/validate_queue_poll_resilience.sh --image ghcr.io/listenarrs/listenarr:canary
```

- **`validate_chapter_grouping.sh`** asks whether a chapter-per-file book comes back from Library
  Import as one scan item or as one item per file. It writes the same book into nine folders that
  differ in only two ways, the filename convention and what the embedded title tags say, then runs
  `scan-unmatched` and counts the items per folder. `Title - Part 01.mp3` is the control: it is
  already handled, so it has to group, and if it does not then the run is measuring "multi-file
  books never group here" and the verdict is inconclusive rather than a pass. Coverage is checked
  before grouping for the same reason, since a scan that indexed nothing looks exactly like a scan
  that grouped nothing when all you have is an item count. The tag axis is there because it decides
  the answer: filenames are grouped first and embedded tags are only consulted when that produced
  more than one group, and the value compared is the `album` tag rather than `title`. Exit `0` every
  folder grouped, `1` one did not, `2` inconclusive.
- **`validate_reported_size.sh`** (Listenarr#542) generates a book that has many files, scans it,
  and compares three numbers that are easy to conflate: the real bytes on disk summed from the
  manifest, the per-file sizes the library recorded, and the single total it shows for the book.
  Keeping them apart separates a summary derived wrongly from a scan that simply missed files. The
  book's BasePath is cleared first, so the scan walks the library root the way it does for a book
  that has not been matched yet. Exit `0` the reported size matches, `1` it is wrong or missing,
  `2` nothing linked so there was nothing to judge. `--asin` picks the multi-file book (it defaults
  to *The Three Musketeers*, whose 40 chapter files make a per-file value obvious), and `--json`
  writes the result for a machine.
- **`validate_import_action.sh`** (Listenarr#598, Listenarr#771) drives a completed-file import
  through the manual-import API and then inspects the result on the host, classifying it as a
  hardlink, a symlink or a copy. That distinction is the point: link, symlink and copy are
  indistinguishable by content, so a unit test asserting both files exist with equal content passes
  for all three and never exercises the cross-device fallback. Same mount should produce a shared
  inode with a link count of two or more; separate mounts must fall back to a copy, because `link()`
  returns `EXDEV`. `--action symlink` tests the symlink action instead, which is expected to work
  across mounts as well, and `--action move` tests relocation, where `rename()` returns `EXDEV`
  across a mount boundary exactly as `link()` does.

  The assertion about the source is the one thing that is **not** uniform, and inverting it by
  accident would turn the check inside out. hardlink, symlink and copy must all leave the source
  in place, because an import that removes it is data loss. A move must remove it, because that is
  what the word means: a move that leaves the source behind has silently done a copy and doubled
  the library, and a move that removes the source without producing a destination has destroyed the
  file. Move needs a third verdict too, since a cross-device move with no fallback does not fail,
  it simply never completes, so that is reported as `stalled` rather than as an ordinary failure.
- **`validate_asin_tag_embed.sh`** asks whether an imported file ends up carrying its ASIN in its
  own embedded tags. Listenarr attempts to write the identifier into the file after a successful
  import so the file keeps it wherever it goes next, and that step is deliberately non-fatal: it
  catches, logs a warning, and the import reports success either way. A step that cannot fail the
  operation it belongs to is a step nobody notices has stopped working, and nothing upstream
  exercises it against a real file. The controller tests mock the writer away entirely, and the
  writer's own test file holds a single case, for the scan-only lease, which asserts that it
  returns early **without** opening a stream. So the one branch under test is the one where it is
  supposed to do nothing. The tool imports a generated file
  that carries no ASIN and then reads the destination's tags on the host, looking in the three
  places the writer puts one. Two gates run before the import and it refuses a verdict without
  both, because the reader has to be shown capable of both answers in the same run: a copy of the
  same file with the atom stamped on by hand must read `tagged`, and the file as it goes in must
  read `untagged`. The server's own log lines are read as corroboration, and if the writer logged
  neither success nor failure the enrichment step never ran and the result is inconclusive rather
  than a finding. Exit `0` the ASIN was embedded, `1` it was not, `2` the run could not be judged.
- **`validate_sidecar_rename.sh`** (Listenarr#577) imports a book's audio, drops `cover.jpg` and
  `metadata.json` beside it, changes the naming pattern so the book must relocate, renames, and then
  looks at the filesystem to see whether the companions followed the audio. It asserts the correct
  behaviour rather than the current one, so it fails today and becomes a regression guard once the
  rename sweeps companion files. Takes an image and an optional port.
- **`validate_companion_import.sh`** asks the neighbouring question on the import path: a manual
  import with `includeCompanionFiles` set is supposed to bring the non-audio files sitting beside the
  audio along with it. The tool drops `.nfo`, `.opf`, `.txt` and `metadata.json` next to the source
  audio, imports, and reads the destination folder the API itself named. `.nfo` and `.opf` carry the
  verdict because Listenarr generates neither, so their absence cannot be explained away by a
  regenerated replacement.

  Three named controls, since a companion check is unusually easy to write so that it cannot fail.
  The audio file **must arrive**, or the tool is looking in the wrong folder and reports nothing. A
  blacklisted `decoy.bak` **must not arrive**, or the inspection is not telling files apart at all.
  And `metadata.json` is judged by provenance rather than existence: every file the tool writes
  carries a per-run sentinel string, so the run computes both the naive verdict ("is there a
  `metadata.json` at the destination?") and the provenance verdict ("is it the one we wrote?") and
  prints them side by side. The bug report this was written against claimed Listenarr regenerates its
  own `metadata.json`, which would make the naive verdict pass on a build that imported nothing.
  Canary contains no `metadata.json` writer at all, so that masking has never actually occurred, and
  the run says so in as many words instead of implying a control fired that did not. It is kept
  because it costs one `stat` and it is what stops the check quietly starting to pass if a writer is
  added later.

  `--case` picks whether the source folder sits outside every configured root folder or inside one,
  and it runs both by default, because the symptom alone cannot say whether the source's placement
  is what decides the outcome. Exit `0` the companions came across, `1` they were dropped, `2` a
  control did not hold.
- **`check_duplicate_detection.sh`** puts both sides of all four twin-ASIN pairs into a running
  instance and asks the duplicate endpoint what it sees. It **reports, it does not judge**: what
  counts as a duplicate is a product decision, and the reason to run it is to get an answer against
  real catalogue data instead of invented rows. Set `ENDPOINT` to point it somewhere else if the
  route moves.
- **`validate_import_destination.sh`** (Listenarr#798) configures two root folders, drops unorganized
  files into one, imports with the *other* selected as the destination, and reads the filesystem to
  see which root the organized folders were created under. The destination never travels on the
  import call itself, so the tool exercises the two paths that actually carry it: `new`, where it
  rides along on `/library/add`, and `preexisting`, where the add returns 409 and the frontend
  falls back to patching `basePath` afterwards. Those are different code paths behind one symptom.

  A third mode, `unpatched`, is a **control that is supposed to fail**: the book is left pointing at
  the scanned root and the frontend's patch is skipped, which is exactly what happens when that
  `PUT` throws, since the frontend catches and continues. The run refuses to report a verdict unless
  the control reproduces, because two passes and a check that cannot fail look identical from the
  outside. Each mode gets its own container and a freshly generated library: sharing one instance
  leaves a registered file behind, and #717's ownership registry remembers it even after the
  audiobook row is deleted.

  `--prime-lock-dir` creates `$HOME/.local/share` in the container first. Only #717 needs it, and
  the reason is worth knowing: its new cross-process move gate resolves a lock directory through
  .NET's `LocalApplicationData`, which comes back empty when `$HOME/.local/share` does not exist,
  and the image never creates it. Every file mutation is refused until it does. The flag is off by
  default so the blocker shows up rather than being papered over.

- **`validate_author_cache_variants.sh`** (Listenarr#672) asks whether one author credited two ways
  becomes two rows in `AuthorCacheEntries`. Issue #672 is mostly about duplicate author cards, which
  PR#673 fixes entirely in the frontend; the issue also claims a backend effect that the PR does not
  touch, and this is aimed at that claim. The backend normaliser keeps only letters, digits and
  whitespace, so punctuation is deleted rather than replaced (`"H. G. Wells"` and `"H.G. Wells"`
  become `h g wells` and `hg wells`), and the table has a unique index on
  `(AuthorNameNormalized, Region)`, so two rows are schema-legal. Whether a second row actually
  appears cannot be read from the source, because the upsert matches on author ASIN before it
  compares names. It **reports, it does not judge**: one row or two is a product decision, and the
  output is the observed rows with their normalized keys, ASINs, and whether an image survived.

  The variant spelling is derived, never invented. It takes an author `corpus/corpus.json` already
  credits with spaced initials and closes the spaces, so both spellings describe a real author in the
  corpus. `--all` sweeps every such author, one container each, because a single name settles very
  little: whether the spellings split turns on what Audible returns for that particular name.

  Unlike the other runtime checks this one needs the container to reach Audible and Audnexus, since
  the whole question is what they answer. A run that cannot reach them exits `2` rather than
  reporting, because an empty table would otherwise read as "no duplicate".
- **`validate_import_relisting.sh`** (Listenarr#616) imports a file and then asks whether that same
  file is still offered as an unmatched candidate. Most of #616 is a product argument about whether
  books that already have files belong in the list, and this stays out of it; there is a narrower
  question underneath that is not a matter of taste. `UnmatchedScanBackgroundService` already filters
  the walk against a set of tracked paths built from `AudiobookFiles` and `Audiobook.FilePath`, under
  a comment reading "so that files already in the library are not reported as unmatched". That filter
  compares paths, which is exact for a move but not for an import whose source survives.

  Each action runs in its own container, because they differ in the way that matters: `copy` and
  `hardlink/copy` leave the original file where it was while the database records the destination,
  and `move` removes the original path entirely. `move` is included as the honest comparison rather
  than as a pass: when it reports nothing afterwards that is absence, not filtering, and the run says
  so instead of counting it.

  Two guards, because "nothing was listed" is what both a working filter and a blind check produce.
  The drop folder's file must be reported *before* the import or the run is inconclusive, and the
  library root's own tracked file is scanned as a control: if that comes back as unmatched, the path
  filter is broken generally and every other verdict in the run is unproven.

- **`validate_root_folder_delete.sh`** (Listenarr#602) asks whether a root folder that owns no books
  can be deleted. Deletion is gated on `HasAudiobooksUnderPathAsync`, which matches any `BasePath`
  equal to the root or starting with it plus a separator, and `RootFolderService.CreateAsync` rejects
  only an exact duplicate path. Nothing stops a root being created inside another root, and an outer
  root then matches every book belonging to the inner one.

  Three scenarios, each in its own container. `sibling` is the control and must pass: two roots that
  do not nest, delete the one owning nothing. If that fails, deletion is broken for some wider reason
  and the run refuses a verdict rather than crediting nesting. `nested-delete` is the reported case.
  `nested-reassign` exercises the documented escape hatch, `?reassignTo=`, where the affected set
  includes books already under the destination.

  Be careful reading the reassign result. It rewrites the stored `BasePath` to a path that does not
  exist, but the files stay on disk, stay tracked, and the book's status does not change, so nothing
  is visibly broken at the time. The tool says so explicitly rather than calling it data loss.

- **`validate_metadata_fallback.sh`** (Listenarr#818) asks whether a scan can still claim a file by
  its embedded tags. A scan attributes by path first: an identifier in the path, a title-bearing
  folder with author context around it, or a filename matching the title. When none of those bite,
  a second pass opens each remaining candidate, reads its tags, and claims it if they name the
  book. For a correctly tagged file in a folder shape the path heuristics do not recognise, that
  pass is the only route to being claimed at all.

  Two modes, each in its own container, because the registration registry remembers files it has
  already registered. `fallback` puts the book in a folder carrying its title and nothing else, so
  no author context exists anywhere on the path and every path heuristic declines. `control` is the
  same book in a layout the heuristics do handle, and it **must** be claimed: "nothing was claimed"
  is also what a bad image, an unmounted library or a scan request that never landed produces, so
  `--mode both` refuses to report a fallback verdict unless the control claimed its file first.

  The report keeps the claim and the recorded size apart. A row can exist and still carry a length
  read from somewhere other than the file, and folding the two together would let either hide the
  other. When the probe refuses a target the tool prints the name it refused: a bare integer there
  is a descriptor number rather than a filename, which says the two halves of the metadata source
  were collapsed onto one path. It also reports whether the refusal reached the scan as a
  diagnostic, since a refusal swallowed below the scan leaves a job that completes with no files
  and no recorded issue.

  `--mode matrix` answers a different question: *which* construction starves path attribution in
  the first place. Attribution has several ways to bite, so this breaks one agreement at a time
  and measures each, using `tools/mismatch_mutate.py` to rename an already-generated library and
  rewrite the manifest to match. Leaving the manifest describing the old layout would turn a
  rename into a phantom scan shortfall.

  ```
  construction                                     canary f27c7989
  control, everything agrees                       claimed
  filename no longer matches the title             claimed
  folder no longer matches the record's title      claimed
  neither folder nor filename matches              UNCLAIMED, reached the tag pass
  no author anywhere on the path                   UNCLAIMED, reached the tag pass
  ```

  The useful result is the pair of "claimed" rows in the middle. Breaking either agreement alone
  is not enough, because the boundary search reads ancestor directories while a separate check
  reads the filename, and each rescues the other. Two doors were found rather than one: every path
  heuristic starved together, and no author context for any of them to stand on. Worth knowing
  that the title-plus-series-number variant the scanner builds joins with a space, so it expects
  `Title 7` and a folder reading `Title, Book 7` misses it on the literal word.

- **`validate_queue_poll_resilience.sh`** asks what one unreadable field in a download client's
  queue response costs. Every other check here drives the library side, where the input is files we
  generate; this one is triggered by a *response*, so what has to be generated is the client.
  `tools/qbittorrent_stub.py` serves the handful of qBittorrent WebUI routes the adapter actually
  calls, with one torrent at a chosen position carrying `downloaded` in a JSON token form the
  mapper's typed accessors do not expect.

  Three outcomes are worth telling apart, and they are very different to operate. The whole poll can
  fail, which marks the client unavailable and is at least visible. One torrent can be skipped,
  which is the tolerable answer. Or everything from the bad torrent onward can vanish while the poll
  still reports itself live and healthy, which is invisible to everything downstream. The check
  reads both the adapter's own count and the queue API's health flags so it can name which of the
  three happened rather than only noticing that a number got smaller.

  `allWellFormed` is the control and it **must** report all N. It serves the same torrents with
  nothing wrong with them, so if Listenarr does not return all of them the check is measuring the
  display filter or the settings write instead, and it exits `3` calling itself broken rather than
  reporting a bug. The malformed cases assert the *fixed* behaviour, so they fail on a build with
  the defect. Both token forms are covered: a fractional number raises `FormatException`, a quoted
  one raises `InvalidOperationException`, and a fix handling only the first would pass half the run.

- **`validate_abs_layout.sh`** asks whether a Listenarr-shaped library survives **Audiobookshelf's
  own parser**. Listenarr can already write the shape ABS reads best with no code change, because
  `{Asin}` is an existing naming token, which makes "point Listenarr at Audiobookshelf" a
  documentation task. Documentation nobody checks rots, so this generates real libraries in the
  recommended shapes and runs ABS's real `getBookDataFromDir` over them, comparing against the
  manifest. Needs `--abs-repo` pointing at an Audiobookshelf checkout and `node` on `PATH`.

  ```bash
  git clone --depth 1 https://github.com/advplyr/audiobookshelf ../audiobookshelf
  ./tools/validate_abs_layout.sh --abs-repo ../audiobookshelf
  ```

  Nothing here reimplements a regex. `abs_parse_bridge.js` requires ABS's real modules out of that
  checkout, so when ABS changes its rules this check changes its answer instead of agreeing with a
  stale copy. Any bare package that will not resolve is stubbed with an empty object and reported,
  which keeps you from having to install ABS's whole dependency tree; relative requires are never
  stubbed, so the code under test is genuinely theirs.

  Three cases, and the control is the point. `series` and `flat` are the recommendation and must
  survive; `control` is Listenarr's own default layout, which carries no ASIN, and it **must
  fail**. A conformance check that passes everything is indistinguishable from one that checks
  nothing, so the run refuses a verdict if the control passes.

  Both ways this arrangement breaks in the wild are silent, which is why it is worth checking at
  all. An ASIN that is not exactly ten uppercase alphanumerics is not ignored, it stays glued to
  the title, so the book ends up titled `Some Book [b0015t963c]`. A non-numeric series position
  does the same and loses the sequence, and that is ordinary data rather than anyone's mistake.
  `--ignore` narrows the comparison for a layout that genuinely does not encode a field, stated per
  run so a layout cannot quietly excuse itself; a test asserts no shipped case ignores the ASIN.

  `--sidecar LABEL=PATH` also runs a `metadata.json` through ABS's schema validator. That matters
  for one specific reason: ABS's folder parser accepts only digits for a sequence while its sidecar
  parser accepts any non-whitespace token, so the sidecar is the only channel that can carry a
  position the folder cannot express. It also catches the trap in that file, which is that a
  structured `{"name":…,"sequence":…}` object is **accepted and then silently discarded**, where
  the string form `"Name #1a"` survives.

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

Each book carries the failure-mode tags it is useful for, and the generator can select on them:
`--tag title-collision,author-collision` puts only the books that exercise those modes on disk, the
same way `--only-asin` selects by identity and `--limit` just takes the first N. Useful when you are
chasing one matching rule and do not want the other 47 modes in the way.

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
| **Layouts** (9) | Folder conventions: the native `{Year} - {Title}` with and without a series folder, Audnex/Plex, author/series/title, AudioBookShelf, flat, author/title, loose files, title-only |
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

### One fixture set that is not a scenario

`tools/make_tag_fixtures.py` sits outside the matrix on purpose. Every scenario writes the same
metadata to every file of a book, so no scenario can produce a book whose files **disagree** with
each other. That is fine for the `tag-dialects` question, which is whether an extractor can read
every spelling ffprobe might surface an ASIN under. It is useless for the other question, which is
what a unanimity guard should do when the files of one book do not agree.

```bash
.venv/bin/python tools/make_tag_fixtures.py --out ./build/tag-adoption
```

Six directories, one book each, tagged file by file: `agree-same-dialect`, `agree-mixed-dialect`
(one ASIN, spelled three ways across m4b, mp3 and flac), `disagree` (two files, two different and
equally real ASINs), `partial-one-tagged`, `partial-lone-file`, and `untagged`. `partial-lone-file`
is the one worth having. A single tagged file is trivially unanimous, so a guard that adopts on
agreement will adopt from it, and there is no second file to disagree later: "the files agree" and
"there was only ever one file" are not the same state. A `manifest.json` records which ASIN was
written to which file under which keys, so a test asserts against the manifest rather than against
hardcoded expectations. Every ASIN is a real, verified one from the corpus, and the audio is the
usual generated silence. This one calls `ffmpeg` from `PATH` (override with `--ffmpeg`) rather than
going through the provisioner.

Generation is deterministic where it counts. The same `--seed` regenerates the same *shape* on any machine — identical folder names, file names, embedded tag values, and manifest — because all of that comes from seeded Python, not the environment. So a maintainer and a reporter running the same seed are looking at the same library in every respect a scan or a rename can observe. The one thing that is **not** guaranteed byte-for-byte across machines is the audio payload itself: it is silence synthesized by ffmpeg, and different ffmpeg builds emit slightly different encoder padding and metadata. If you need the media bytes to match too (rarely — the tags and paths are what scanners read), pin the ffmpeg version.

## The destructive axis

Most scan bugs leave a file unlinked, which is annoying. A rename bug destroys data, and the two are often one line apart in the same code path. So the hazard cases are generated for real and the rename is audited:

```bash
.venv/bin/python tools/generate_library.py --scenario rename-hazards --out ./build/hz --seed 1
.venv/bin/python tools/verify_scan.py --manifest ./build/hz/manifest.json --snapshot before.json

# ... now run Listenarr's rename against ./build/hz ...

.venv/bin/python tools/verify_scan.py --manifest ./build/hz/manifest.json --audit before.json
```

**`tools/validate_rename_hazards.sh` drives that whole sequence in one command** against a real
container, so the manual step in the middle is no longer manual: it snapshots, adds the books, scans
so the hazard files are tracked, forces a relocation, **executes** the rename rather than previewing
it, and audits. It reports how much it actually exercised alongside the verdict, because a clean
audit is only worth the fraction that really moved, and two images can both come back clean while one
relocated most of the corpus and the other a small slice of it.

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
tools/make_tag_fixtures.py  per-file tag agreement/disagreement fixtures
tools/verify_scan.py        expected vs observed; the rename audit; --json/--junit
tools/conformance_diff.py   A/B two --json reports: what a branch fixed and regressed
tools/vet-against.sh        build a branch and run the harness against it
tools/benchmark_scan.sh     time a scan at several library sizes
tools/validate_*.sh         the narrower runtime checks, one behaviour each
tools/check_duplicate_*.sh  both ASINs of each twin pair, against a live instance
tools/*_ffbinary.py         package and install pinned ffmpeg-family binaries
tools/ffmpeg_harness.py     the one verified-extract path everything above shares
tests/                      the test suite
TESTING.md                  how the suite is organised; the verdict-contract convention
```

`tools/attribution_report.py` and `tools/size_report.py` are the judging halves of
`validate_scan_attribution.sh` and `validate_reported_size.sh`: the shell script produces the
observation, the module reads it against the manifest and decides. They are documented here because
their exit codes are the ones the shell scripts return, and both follow `verify_scan`: `0` pass,
`1` fail, `2` inconclusive. Inconclusive is deliberately not a pass, because a checker that cannot
see the answer key must not report success. `tools/fetch_ffprobe.py` is a CI helper, used so every
runner in the cross-platform workflow fetches its platform's ffprobe with one command regardless of
shell.

Requires Python 3.11+ and `mutagen`. ffmpeg on `PATH` is needed if you pass `--ffmpeg-source system`
or run `make_tag_fixtures.py`, since the generator otherwise provisions its own pinned build;
ffprobe on `PATH` is what the test suite reads generated tags back with, and the audio tests skip
themselves without it. Development extras (`pip install -e '.[dev]'`) add pytest, ruff and mypy; `python -m pytest` runs the suite. This is a conformance harness, so a subset of the tests are adversarial against the tool's own verdict — see [TESTING.md](TESTING.md) and `pytest -m contract`.

## Provenance and licence

The code is MIT. The metadata — titles, authors, narrators, ASINs, series positions — is factual, and is fetched from Audnex rather than authored here. The books themselves are in the public domain, and their recordings are freely available from LibriVox.

The generated audio is one second of digital silence, synthesized on your machine at generation time. It is not a recording of anything.
