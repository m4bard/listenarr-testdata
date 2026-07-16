#!/usr/bin/env bash
#
# benchmark_scan.sh — time a Listenarr library scan against a generated library.
#
# WHAT IS BEING MEASURED, and why it is shaped like this.
#
# The scan fans out per audiobook. ScanJobProcessor calls FindMatchingAudioFiles(scanRoot,
# audiobook), which calls CollectCandidates(scanRoot) — EVERY audio file under the scan root
# — once for EACH audiobook scanned. When an audiobook has no BasePath, the scan root falls
# back to the library root, so the work is O(books x files in the whole library), not
# O(files in this book's folder).
#
# So the honest measurement is the cost of scanning ONE book as a function of how big the
# library is. Time that at several library sizes and the shape shows itself: if per-book scan
# time grows with the size of the library rather than with the size of the book, the fan-out
# is expensive; if it flattens, the re-walk is real but cheap.
#
# MEASURED against ghcr.io/listenarrs/listenarr:canary, one book, BasePath cleared
# (--books 1 --no-basepath), ffprobe sampled inside the container:
#
#     library files    per-book scan    peak ffprobe
#          4,000            28.8s             1
#         24,000            57.2s             1
#         48,000            57.2s             1
#         98,400            61.3s             1
#
# The cost RISES from 4k to ~24k and then PLATEAUS: 24x the files buys ~2x the time. The
# re-walk is real (BasePath empty => scan root is the whole library) but it is I/O-bound
# directory enumeration that saturates, not a blow-up — scanning one book against a 98k-file
# library is ~1 minute, not the hours a naive O(books x files) reading would predict. The
# per-book path DOES invoke ffprobe, but only for files that match the book (peak 1 here),
# not once per candidate.
#
# An earlier draft of this file reported a superlinear curve and extrapolated to hours; that
# was an artifact of comparing runs taken under different --books counts and a host-side
# ffprobe sampler that could not see into the container. The table above is a clean sweep
# under identical conditions and supersedes it.
#
# Nothing here builds Listenarr. It runs the PUBLISHED image, so a maintainer gets the same
# number from the same artifact, and no local checkout is touched.
#
#   ./tools/benchmark_scan.sh --limit 5 --books 3     # a quick end-to-end check
#   ./tools/benchmark_scan.sh --limit 123 --books 3   # the full ~98k-file library
#
set -euo pipefail
unset TMOUT

SCENARIO="scale"
LAYOUT=""
SEED=1
LIMIT=""
BOOKS=3
IMAGE="ghcr.io/listenarrs/listenarr:canary"
PORT=4545
KEEP=0
NO_BASEPATH=0
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${ROOT}/.venv/bin/python"

usage() {
    cat <<EOF
benchmark_scan.sh — time a Listenarr scan against a generated library.

Options:
  --scenario KEY   scenario to generate (default: ${SCENARIO})
  --layout KEY     force a single on-disk layout (or alias, e.g. 'listenarr')
  --seed N         generator seed (default: ${SEED})
  --limit N        use only the first N corpus books, i.e. shrink the library (default: all)
  --books N        how many audiobooks to add and scan (default: ${BOOKS})
  --image REF      container image (default: ${IMAGE})
  --port N         host port to bind (default: ${PORT})
  --no-basepath    clear BasePath before scanning (the only state in which the
                   scan root falls back to the library root — see below)
  --keep           leave the container running for inspection
  -h, --help       this
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --scenario) SCENARIO="$2"; shift 2 ;;
        --layout)   LAYOUT="$2";   shift 2 ;;
        --seed)     SEED="$2";     shift 2 ;;
        --limit)    LIMIT="$2";    shift 2 ;;
        --books)    BOOKS="$2";    shift 2 ;;
        --image)    IMAGE="$2";    shift 2 ;;
        --port)     PORT="$2";     shift 2 ;;
        --keep)     KEEP=1;        shift ;;
        --no-basepath) NO_BASEPATH=1; shift ;;
        -h|--help)  usage; exit 0 ;;
        *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done

log() { printf '%s [%s] %s\n' "$(date +%H:%M:%S)" "$1" "${*:2}"; }
die() { log ERROR "$*"; exit 1; }

# Rootless by preference: it needs no docker group, and nothing here wants privilege.
if command -v podman >/dev/null 2>&1; then
    RUNTIME=podman
elif docker info >/dev/null 2>&1; then
    RUNTIME=docker
else
    die "no usable container runtime (podman not installed, docker daemon unreachable)"
fi

[[ -x "$PYTHON" ]] || die "no venv — run: python3 -m venv .venv && .venv/bin/pip install -e ."
command -v curl   >/dev/null 2>&1 || die "curl is required (Ubuntu: sudo apt install curl)"
command -v ffmpeg >/dev/null 2>&1 || die "ffmpeg is required to synthesize audio (Ubuntu: sudo apt install ffmpeg)"

LIBRARY="${ROOT}/build/bench-lib"
CONFIG="${ROOT}/build/bench-config"
CONTAINER="listenarr-bench-$$"
API="http://localhost:${PORT}/api/v1"

cleanup() {
    if [[ "$KEEP" -eq 1 ]]; then
        log INFO "leaving ${CONTAINER} on port ${PORT} (--keep)"
        return
    fi
    "$RUNTIME" rm -f "$CONTAINER" >/dev/null 2>&1 || true
}
trap cleanup EXIT

log INFO "runtime ${RUNTIME}, image ${IMAGE}"

# --- 1. generate ----------------------------------------------------------------------
log INFO "generating '${SCENARIO}' (seed ${SEED}${LIMIT:+, limit ${LIMIT}})"
rm -rf "$LIBRARY" "$CONFIG"; mkdir -p "$CONFIG"
"$PYTHON" "${ROOT}/tools/generate_library.py" \
    --scenario "$SCENARIO" ${LAYOUT:+--layout "$LAYOUT"} \
    --out "$LIBRARY" --seed "$SEED" ${LIMIT:+--limit "$LIMIT"} --force \
    >/dev/null

FILES=$(find "$LIBRARY" -type f ! -name manifest.json | wc -l)
SIZE=$(du -sh "$LIBRARY" | cut -f1)
log INFO "library: ${FILES} files, ${SIZE}"

# --- 2. start Listenarr ---------------------------------------------------------------
"$RUNTIME" rm -f "$CONTAINER" >/dev/null 2>&1 || true
"$RUNTIME" run -d --name "$CONTAINER" \
    -p "${PORT}:4545" \
    -e LISTENARR_LOG_LEVEL=Debug \
    -v "${LIBRARY}:/audiobooks" \
    -v "${CONFIG}:/app/config" \
    "$IMAGE" >/dev/null || die "could not start the container"

log INFO "waiting for the API"
for _ in $(seq 1 90); do
    curl -fsS "${API}/rootfolders" >/dev/null 2>&1 && break
    sleep 2
done
curl -fsS "${API}/rootfolders" >/dev/null 2>&1 \
    || die "API never came up. Logs: ${RUNTIME} logs ${CONTAINER}"

# Mutating calls are CSRF-protected and a machine client carries no session. An API-key
# request is treated as authenticated and skips antiforgery. The key is generated into the
# config directory on first run; this one is created fresh per run and thrown away with it.
API_KEY=$("$PYTHON" -c "import json; print(json.load(open('${CONFIG}/config.json'))['ApiKey'])") \
    || die "no ApiKey in ${CONFIG}/config.json"
AUTH=(-H "X-Api-Key: ${API_KEY}" -H 'Content-Type: application/json')
log INFO "API up, authenticated"

# --- 3. register the library ----------------------------------------------------------
FOLDER_ID=$(curl -fsS -X POST "${API}/rootfolders" "${AUTH[@]}" \
    -d '{"name":"bench","path":"/audiobooks","isDefault":true}' \
    | "$PYTHON" -c "import json,sys; print(json.load(sys.stdin)['id'])") \
    || die "could not create the root folder"
log INFO "root folder ${FOLDER_ID} -> /audiobooks"

# --- 4. add the audiobooks to scan ----------------------------------------------------
# Straight from the corpus: real ASINs, real titles, real authors. No BasePath, because
# nothing has been matched yet — which is exactly the state that makes the scan root fall
# back to the library root.
log INFO "adding ${BOOKS} audiobooks"
export ROOT API API_KEY
IDS=$("$PYTHON" - "$BOOKS" <<'PY'
import json, os, sys, urllib.request

books = json.load(open(os.path.join(os.environ["ROOT"], "corpus", "corpus.json")))["books"]
api, key = os.environ["API"], os.environ["API_KEY"]
ids = []
for book in sorted(books, key=lambda b: b["asin"])[: int(sys.argv[1])]:
    payload = json.dumps({
        "metadata": {
            "asin": book["asin"],
            "title": book["title"],
            "authors": book["authors"],
            "narrators": book["narrators"],
            "series": book["series"],
            "seriesNumber": book["series_position"],
            "publishYear": (book["release_date"] or "")[:4] or None,
            "source": "Audible",
            "region": book["region"],
        },
        "monitored": True,
        "autoSearch": False,
    }).encode()
    request = urllib.request.Request(
        f"{api}/library/add", data=payload, method="POST",
        headers={"Content-Type": "application/json", "X-Api-Key": key},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            body = json.loads(response.read().decode() or "{}")
        book_id = body.get("id") or (body.get("audiobook") or {}).get("id")
        if book_id:
            ids.append(str(book_id))
    except Exception as exc:  # noqa: BLE001 — a book that will not add is not fatal
        print(f"could not add {book['asin']}: {exc}", file=sys.stderr)
print(" ".join(ids))
PY
) || die "could not add any audiobooks"
export IDS
[[ -n "$IDS" ]] || die "no audiobooks were added — nothing to scan"
log INFO "added: $(echo "$IDS" | wc -w) audiobooks"

# --- 4b. optionally clear BasePath ----------------------------------------------------
# LibraryScanPathResolver checks BasePath FIRST and returns immediately, so an audiobook that
# has one never consults the requested scan path and never walks the library. Adding a book
# synthesizes a BasePath from its metadata, so out of the box the fan-out cannot happen — the
# scan dead-ends on a folder that does not exist instead (which is its own bug, and the one
# `existing-library-adoption` is about).
#
# --no-basepath clears it, which is the state a record is in before anything has been matched
# to it. That is the ONLY state in which the scan root falls back to the library root, so it
# is the only state in which the fan-out this benchmark exists to measure is reachable.
if [[ "$NO_BASEPATH" -eq 1 ]]; then
    command -v sqlite3 >/dev/null || die "--no-basepath needs sqlite3"
    "$RUNTIME" stop "$CONTAINER" >/dev/null 2>&1 || true
    sqlite3 "${CONFIG}/database/listenarr.db" "UPDATE Audiobooks SET BasePath = NULL;"
    log INFO "cleared BasePath on every audiobook"
    "$RUNTIME" start "$CONTAINER" >/dev/null 2>&1 || die "could not restart the container"
    for _ in $(seq 1 90); do
        curl -fsS "${API}/rootfolders" >/dev/null 2>&1 && break
        sleep 2
    done
fi

# --- 5. scan each one, and time it ----------------------------------------------------
# Sample the ffprobe process count on the host. Rootless podman shares the kernel, so the
# container's ffprobe children are visible here.
PEAK_FILE=$(mktemp); echo 0 > "$PEAK_FILE"
TOTAL_FILE=$(mktemp); echo 0 > "$TOTAL_FILE"
(
    peak=0; seen=0
    while :; do
        n=$("$RUNTIME" top "$CONTAINER" 2>/dev/null | grep -c ffprobe) || n=0
        (( n > peak )) && { peak=$n; echo "$peak" > "$PEAK_FILE"; }
        (( n > 0 )) && { seen=$((seen + n)); echo "$seen" > "$TOTAL_FILE"; }
        sleep 0.05
    done
) & SAMPLER=$!
kill_sampler() { kill "$SAMPLER" 2>/dev/null || true; }
trap 'kill_sampler; cleanup' EXIT

echo
printf '  %-8s %-34s %10s\n' "book" "title" "scan (s)"
printf '  %s\n' "------------------------------------------------------------"

RUN_START=$(date +%s.%N)
for id in $IDS; do
    TITLE=$(curl -fsS "${API}/library/${id}" "${AUTH[@]}" \
        | "$PYTHON" -c "import json,sys; print((json.load(sys.stdin).get('title') or '?')[:32])" \
        2>/dev/null || echo "?")

    START=$(date +%s.%N)
    # Scan the WHOLE library root for this one book. That is the fan-out condition:
    # CollectCandidates walks every audio file under the scan root, once per audiobook.
    JOB=$(curl -fsS -X POST "${API}/library/${id}/scan" "${AUTH[@]}" -d '{"path":"/audiobooks"}' \
        | "$PYTHON" -c "
import json,sys
d = json.load(sys.stdin)
print(d.get('jobId') or d.get('id') or '')" 2>/dev/null || echo "")

    if [[ -n "$JOB" ]]; then
        STATUS="Pending"
        LAST_BEAT=0
        while [[ "$STATUS" != "Completed" && "$STATUS" != "Failed" && "$STATUS" != "?" ]]; do
            sleep 2
            STATUS=$(curl -fsS "${API}/library/scan/${JOB}" "${AUTH[@]}" \
                | "$PYTHON" -c "
import json,sys
d = json.load(sys.stdin)
print(d.get('status') or d.get('state') or '?')" 2>/dev/null || echo "?")
            # A timestamped heartbeat every ~30s: a background log accumulates readable
            # progress instead of a single carriage-returned line nobody can tail.
            NOW=$(date +%s.%N)
            ELAPSED_INT=$(printf '%.0f' "$(echo "$NOW - $START" | bc)")
            if (( ELAPSED_INT - LAST_BEAT >= 30 )); then
                PEAK_NOW=$(cat "$PEAK_FILE")
                log SCAN "book ${id} (${TITLE}): status=${STATUS} elapsed=${ELAPSED_INT}s peak_ffprobe=${PEAK_NOW}"
                LAST_BEAT=$ELAPSED_INT
            fi
        done
    fi
    END=$(date +%s.%N)
    SECS=$(echo "$END - $START" | bc)
    log SCAN "book ${id} (${TITLE}): ${STATUS} in ${SECS}s"
    printf '  %-8s %-34s %10.1f\n' "$id" "$TITLE" "$SECS"
done
RUN_END=$(date +%s.%N)

kill_sampler
ELAPSED=$(echo "$RUN_END - $RUN_START" | bc)
PEAK=$(cat "$PEAK_FILE"); rm -f "$PEAK_FILE" "$TOTAL_FILE"
SCANNED=$(echo "$IDS" | wc -w)
PER_BOOK=$(echo "scale=2; $ELAPSED / $SCANNED" | bc)

cat <<EOF

────────────────────────────────────────────────────────────
  library            ${FILES} files (${SIZE})
  audiobooks scanned ${SCANNED}
  total wall clock   ${ELAPSED}s
  per book           ${PER_BOOK}s
  peak ffprobe       ${PEAK} concurrent
────────────────────────────────────────────────────────────
  Per-book cost is the number that matters. If it grows with the
  size of the LIBRARY rather than the size of the BOOK, the scan
  is re-walking the whole library once per audiobook.
────────────────────────────────────────────────────────────

Conformance:
EOF

# verify_scan.py prepends the /api/v1 path segment itself, so it takes the un-prefixed host
# base (passing ${API} here would double the prefix). See listenarr-testdata#1.
"$PYTHON" "${ROOT}/tools/verify_scan.py" \
    --manifest "${LIBRARY}/manifest.json" \
    --api "http://localhost:${PORT}" --api-key "${API_KEY}" \
    --root-map "/audiobooks=${LIBRARY}"
