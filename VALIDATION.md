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

## Scan cost (all images) — recorded separately

The library-scan wall-clock and ffprobe counts referenced for issue #765 are committed with the
benchmark itself: see the measured table in the header of `tools/benchmark_scan.sh` and commit
`e506552`. Summary: one book against a 98,400-file library is ~1 minute; cost rises to ~24k
files and then plateaus (sublinear), and the per-book scan path invokes ffprobe only for
matched files (peak 1), not once per candidate.
