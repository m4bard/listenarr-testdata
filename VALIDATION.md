# Validation log

Before/after evidence produced by pointing this repo's harness at a running Listenarr.
Each entry is a paired run — the published `:canary` image (the bug) against an image built
from the fix branch (the fix) — measured with `tools/verify_scan.py` against the manifest's
recorded true ownership. Numbers are from a local run; regenerate with the commands shown.

Method for every entry: `tools/benchmark_scan.sh` generates a deterministic library, starts
the image under a rootless container, adds books from the corpus, clears `BasePath`
(`--no-basepath`, the state that makes the scan fall back to the library root), scans, and
compares what linked against `manifest.json`. Nothing here builds or touches any local
Listenarr checkout: the fix image is built from a fresh clone of the public fork branch.

---

## PR #766 / issue #765 — scan attributes one author's books to whichever is scanned

**Claim under test:** `ScanFileDiscovery.Matches` treated "the path contains the author" as
enough to attribute a file to a specific book, so scanning one book by an author claimed every
file under that author's folder. PR #766 drops the author-path clause.

**Harness:** `existing-library-adoption` / `scale` layout (`{Author}/{Author} - {Series} -
{Title}`), two books by two different authors, `--no-basepath`.

**Result** — files linked to each scanned book, classified by their *true* owner in the
manifest:

| scan of | canary (`:canary`) | fix (`fix/765-scan-author-overmatch`) |
|---|---|---|
| The Valley of Fear | 1,600 files spanning **2 books** — claimed a sibling | 800 files, **1 book** — only its own |
| Stalky and Co. | 1,554 files spanning **2 books** | 745 files, **1 book** — only its own |

Canary's per-book scan vacuums up a sibling book's files; the fix scan claims only the book's
own. **The overmatch is present on canary and resolved on the fix branch.**

Note, not a regression in the fix: the fix links 745 of Stalky and Co.'s own 800 files — a
slight under-match on that period-titled book. It is a matching-completeness question distinct
from the overmatch this PR targets, and worth a separate look; the fix did not introduce it.

Note on `BasePath`: the stored `BasePath` string is the author folder in *both* runs, so the
string alone does not distinguish them. The bug and its fix are visible only in *which files
are linked*, which is why this is measured against the manifest's true ownership rather than by
reading `BasePath`.

**Reproduce:**

```bash
# before
./tools/benchmark_scan.sh --limit 20 --books 2 --no-basepath \
    --image ghcr.io/listenarrs/listenarr:canary
# after: build the fix branch from the public fork, then re-run with --image
git clone --depth 1 --branch fix/765-scan-author-overmatch \
    https://github.com/m4bard/Listenarr.git /tmp/vet-src
podman build --network=host -t listenarr-vet:pr766 /tmp/vet-src
./tools/benchmark_scan.sh --limit 20 --books 2 --no-basepath \
    --image localhost/listenarr-vet:pr766
```

---

## PR #763 / issue #764 — a non-numeric series position lost in naming

**Claim under test:** a series position from Audnexus is a string that is not always a number
("1-4" for an omnibus, "0" for a prequel, "1.5" for a novella). Squeezing it through
`decimal.TryParse` lost it, and the loss reached the filename — it fell through to the track
number. PR #763 threads the raw string (`SeriesPositionRaw`) through naming.

**Validated — data level:** the exact positions the PR cites are real and machine-verified
against live Audnexus in this repo's corpus: `B0F84DFZ66` → "1-4", `B002V1PLZK` → "1-2",
`B00CQ5WAXW` → "0". See `corpus/corpus.json` and `tools/build_corpus.py --check`.

**Validated — unit level (the PR's own tests):** `SeriesPositionReproTests` asserts that
`FileNamingService`, given `SeriesPositionRaw = "1-4"`, writes "1-4" into the name and NOT the
track number ("7"). That is the fix's mechanism, and it is covered.

**Not validated — runtime before/after:** NOT achieved, and here is the honest reason. The fix
lives in the **download-import** path (`DownloadImportService` populates `SeriesPositionRaw`
from the audiobook record, then `FileNamingService.Helpers` prefers it). Driving that path
end-to-end needs a download client and a completed download — materially more setup than was
in scope here.

A first attempt drove the **manual-import** path instead (`POST /library/manual-import`) and
found the position lost on BOTH canary and the fix image. That is NOT a gap in the fix: manual
import names the file from the FILE's embedded tags (`ExtractFileMetadataAsync`), not from the
matched audiobook record, and the test file carried no series-position tag — so the position
was empty for a reason unrelated to #763. The attempt was retired rather than reported as a
finding. (Lesson: manual-import naming ≠ download-import naming; only the latter is #763's
surface.)

**One unconfirmed lead, for the PR author, not asserted as a bug:** the file-extraction path
(`MetadataService.ExtractFileMetadataAsync`) that manual-import relies on does not populate
`SeriesPositionRaw` — the fix did not touch it. So a file carrying a non-numeric
series-position *tag* imported manually might still lose it. Whether extraction even reads
that tag is unchecked; worth a glance, not a claim.

---

## PR #768 / issue #767 — series ASIN dropped at metadata conversion

**Claim under test:** `ConvertAudnexusToMetadata` mapped the series name and position but
dropped `AudnexusSeries.Asin`, so `AudiobookSeriesMembership.SeriesAsin` was never populated —
even though `(SeriesAsin, position)` is the only stable work key (a book ASIN is
per-marketplace and per-narrator; a series name is free text). PR #768 maps the ASIN through.

**Validated — data level:** this repo's whole reason for existing is that this work key is
real and reproducible. The corpus proves it two ways, both re-verifiable against live Audnexus:
four public-domain works each carry two distinct book ASINs under one `series_asin` + position
(the Barsoom / Verne / Oz / Musketeers pairs), and the Grimm region-lock proof shows one work
under two ASINs, each invisible from the other's marketplace. See `corpus/corpus.json`
(`region_lock_proof`) and `tools/build_corpus.py --check`.

**Validated — unit level (the PR's own tests):** `MetadataConvertersSeriesAsinTests` asserts
the converter now copies the primary and secondary series ASINs.

**Not validated — runtime before/after:** the converter only runs on a live Audnexus fetch, so
a runtime before/after would depend on the container reaching `api.audnex.us` and a fetch
trigger. Left at the data + unit level above rather than built on an external live dependency.

---

## Scan cost (all images) — recorded separately

The library-scan wall-clock and ffprobe counts referenced for issue #765 are committed with the
benchmark itself: see the measured table in the header of `tools/benchmark_scan.sh` and commit
`e506552`. Summary: one book against a 98,400-file library is ~1 minute; cost rises to ~24k
files and then plateaus (sublinear), and the per-book scan path invokes ffprobe only for
matched files (peak 1), not once per candidate.
