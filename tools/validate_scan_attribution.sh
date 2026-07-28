#!/usr/bin/env bash
#
# validate_scan_attribution.sh — prove, at runtime, which files a scan attributes to a book.
#
# THE QUESTION THIS ANSWERS.
#
# ScanFileDiscovery decides whether a candidate file belongs to the audiobook being scanned.
# Loosening that predicate is cheap to do and expensive to get wrong: a file attributed to the
# wrong book is silent — nothing errors, the library just quietly says a book owns audio that
# belongs to a different book (and the resulting common parent becomes its BasePath).
#
# So: add ONE audiobook, clear its BasePath so the scan root falls back to the library root
# (the only state in which the whole library is walked), scan, then read what the scan linked
# and map every linked file back to its TRUE owner using the generator's manifest. Any linked
# file whose true owner is a different book is a misattribution, named and counted.
#
# Only the target book is added. Every other book exists on disk but has no record, so a
# correct scanner links the target's own files and nothing else.
#
#   ./tools/validate_scan_attribution.sh --image ghcr.io/listenarrs/listenarr:canary \
#       --asin B004FOLXEO --layout author-title \
#       --only-asin B004FOLXEO,B01ATTZF38,B0C6FJ6L34
#
# NOTE the layout. The default {author}/{series}/{title} cannot render a book with no series
# and silently skips it, so a set of standalone books generates an EMPTY library. Use
# author-title for those.
#
# Exit 0 = no misattribution. Exit 1 = the scan claimed files belonging to another book.
#
set -euo pipefail
unset TMOUT

IMAGE="ghcr.io/listenarrs/listenarr:canary"
ASIN=""
ONLY_ASIN=""
LAYOUT="listenarr"
PORT=4548
SEED=1
KEEP=0
LABEL=""
JSON_OUT=""
USE_LIBRARY=""
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${ROOT}/.venv/bin/python"
LIBRARY="${ROOT}/build/attrib-library"
CONFIG="${ROOT}/build/attrib-config"
CONTAINER="listenarr-attrib-$$"

usage() {
    cat <<EOF
validate_scan_attribution.sh — show which files a scan attributes to one audiobook.

  --image REF       container image to test (default: ${IMAGE})
  --asin ASIN       the ONE book to add and scan (required)
  --only-asin LIST  comma-separated ASINs to put on disk (default: the whole corpus)
  --layout KEY      on-disk layout (default: ${LAYOUT})
  --port N          host port (default: ${PORT})
  --seed N          generator seed (default: ${SEED})
  --label TEXT      label for the report header (default: the image ref)
  --json PATH       also write the result as JSON
  --library DIR     use a prepared library (with manifest.json) instead of generating
  --keep            leave the container running for inspection
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --image)     IMAGE="$2";     shift 2 ;;
        --asin)      ASIN="$2";      shift 2 ;;
        --only-asin) ONLY_ASIN="$2"; shift 2 ;;
        --layout)    LAYOUT="$2";    shift 2 ;;
        --port)      PORT="$2";      shift 2 ;;
        --seed)      SEED="$2";      shift 2 ;;
        --label)     LABEL="$2";     shift 2 ;;
        --json)      JSON_OUT="$2";  shift 2 ;;
        --library)   USE_LIBRARY="$2"; shift 2 ;;
        --keep)      KEEP=1;         shift ;;
        -h|--help)   usage; exit 0 ;;
        *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done

log() { printf '%s [%s] %s\n' "$(date +%H:%M:%S)" "$1" "${*:2}"; }
die() { log ERROR "$*"; exit 1; }

[[ -n "$ASIN" ]] || die "--asin is required (see --help)"
[[ -n "$LABEL" ]] || LABEL="$IMAGE"

if command -v podman >/dev/null 2>&1; then RUNTIME=podman
elif docker info >/dev/null 2>&1; then RUNTIME=docker
else die "no usable container runtime"; fi

[[ -x "$PYTHON" ]] || die "no venv — run: python3 -m venv .venv && .venv/bin/pip install -e ."
command -v sqlite3 >/dev/null 2>&1 || die "sqlite3 is required"

cleanup() {
    if [[ "$KEEP" -eq 1 ]]; then
        log INFO "leaving ${CONTAINER} running on port ${PORT}"
        return
    fi
    "$RUNTIME" rm -f "$CONTAINER" >/dev/null 2>&1 || true
}
trap cleanup EXIT

# --- 1. generate (or reuse a prepared library) ------------------------------------------
if [[ -n "$USE_LIBRARY" ]]; then
    # A prepared tree, for shapes the generator does not express (e.g. an author folder
    # spelled as a variant). Its manifest must already describe the paths as they are.
    [[ -f "${USE_LIBRARY}/manifest.json" ]] || die "${USE_LIBRARY} has no manifest.json"
    LIBRARY="$USE_LIBRARY"
    rm -rf "$CONFIG"; mkdir -p "$CONFIG"
    log INFO "using prepared library ${LIBRARY}"
else
    log INFO "generating library (layout ${LAYOUT}, seed ${SEED})"
    rm -rf "$LIBRARY" "$CONFIG"; mkdir -p "$CONFIG"
    "$PYTHON" "${ROOT}/tools/generate_library.py" \
        --layout "$LAYOUT" --out "$LIBRARY" --seed "$SEED" --force \
        ${ONLY_ASIN:+--only-asin "$ONLY_ASIN"} >/dev/null \
        || die "generation failed"
fi
FILES=$(find "$LIBRARY" -type f ! -name manifest.json | wc -l)
log INFO "library: ${FILES} audio files"
# Fail fast and say why. The usual cause is a layout the chosen books cannot express: the
# default {author}/{series}/{title} skips anything with no series, so a set of standalone
# books yields an empty tree and there is nothing to attribute.
[[ "$FILES" -gt 0 ]] || die "the library is empty — the '${LAYOUT}' layout could not render any of the requested books (a layout with {series} skips books that have none; try --layout author-title)"

log INFO "provisioning pinned ffprobe"
"$PYTHON" "${ROOT}/tools/ffprobe_provisioner.py" --config-dir "$CONFIG" >/dev/null \
    || die "could not provision ffprobe"

# --- 2. start --------------------------------------------------------------------------
"$RUNTIME" rm -f "$CONTAINER" >/dev/null 2>&1 || true
# Port 4545 inside the container, and the library mounted READ-WRITE: newer builds 500 the
# scan endpoint on a read-only library mount, older ones do not care, so rw works for both.
"$RUNTIME" run -d --name "$CONTAINER" \
    -p "${PORT}:4545" \
    -e LISTENARR_LOG_LEVEL=Debug \
    -v "${LIBRARY}:/audiobooks" \
    -v "${CONFIG}:/app/config" \
    "$IMAGE" >/dev/null || die "could not start ${IMAGE}"

API="http://localhost:${PORT}/api/v1"
log INFO "waiting for ${IMAGE} to answer"
for _ in $(seq 1 120); do
    curl -fsS "${API}/rootfolders" >/dev/null 2>&1 && break
    sleep 2
done
curl -fsS "${API}/rootfolders" >/dev/null 2>&1 || die "API never came up"

# Mutating calls are CSRF-protected and a machine client carries no session; a valid API key
# is the documented exemption. The key is written to config.json on first boot, not to the DB.
API_KEY=$("$PYTHON" -c "import json; print(json.load(open('${CONFIG}/config.json'))['ApiKey'])") \
    || die "no ApiKey in ${CONFIG}/config.json"
AUTH=(-H "X-Api-Key: ${API_KEY}" -H 'Content-Type: application/json')

# caseSensitivityMode is required by newer builds and ignored by older ones — sending it
# always keeps this one code path working against both.
FOLDER_ID=$(curl -fsS -X POST "${API}/rootfolders" "${AUTH[@]}" \
    -d '{"name":"attrib","path":"/audiobooks","isDefault":true,"caseSensitivityMode":"Sensitive"}' \
    | "$PYTHON" -c "import json,sys; print(json.load(sys.stdin)['id'])") \
    || die "could not create the root folder"
log INFO "root folder ${FOLDER_ID} -> /audiobooks"

# --- 3. add exactly one book -----------------------------------------------------------
export ROOT API API_KEY ASIN
BOOK_ID=$("$PYTHON" - <<'PY'
import json, os, urllib.request
books = json.load(open(os.path.join(os.environ["ROOT"], "corpus", "corpus.json")))["books"]
book = next(b for b in books if b["asin"] == os.environ["ASIN"])
payload = json.dumps({
    "metadata": {
        "asin": book["asin"], "title": book["title"], "authors": book["authors"],
        "narrators": book["narrators"], "series": book["series"],
        "seriesNumber": book["series_position"],
        "publishYear": (book["release_date"] or "")[:4] or None,
        "source": "Audible", "region": book["region"],
    },
    "monitored": True, "autoSearch": False,
}).encode()
req = urllib.request.Request(f"{os.environ['API']}/library/add", data=payload, method="POST",
                             headers={"Content-Type": "application/json",
                                      "X-Api-Key": os.environ["API_KEY"]})
with urllib.request.urlopen(req, timeout=60) as r:
    body = json.loads(r.read().decode() or "{}")
print(body.get("id") or (body.get("audiobook") or {}).get("id") or "")
PY
) || die "could not add ${ASIN}"
[[ -n "$BOOK_ID" ]] || die "add returned no id for ${ASIN}"
log INFO "added ${ASIN} as audiobook ${BOOK_ID}"

# --- 4. clear BasePath -----------------------------------------------------------------
# Adding a book synthesizes a BasePath from its metadata; LibraryScanPathResolver checks it
# FIRST and returns, so the library is never walked. Clearing it is the pre-match state, and
# the only one in which the scan root falls back to the library root.
"$RUNTIME" stop "$CONTAINER" >/dev/null 2>&1 || true
sqlite3 "${CONFIG}/database/listenarr.db" "UPDATE Audiobooks SET BasePath = NULL;"
"$RUNTIME" start "$CONTAINER" >/dev/null 2>&1 || die "could not restart"
for _ in $(seq 1 120); do
    curl -fsS "${API}/rootfolders" >/dev/null 2>&1 && break
    sleep 2
done
log INFO "cleared BasePath; scanning"

# --- 5. scan ---------------------------------------------------------------------------
curl -fsS -X POST "${API}/library/${BOOK_ID}/scan" "${AUTH[@]}" \
    -d '{"path":"/audiobooks"}' >/dev/null || die "scan request failed"

for _ in $(seq 1 60); do
    n=$(sqlite3 "${CONFIG}/database/listenarr.db" \
        "SELECT COUNT(*) FROM AudiobookFiles WHERE AudiobookId='${BOOK_ID}';" 2>/dev/null || echo 0)
    [[ "$n" != "0" ]] && break
    sleep 2
done
sleep 3   # let the job settle before reading

# --- 6. classify every linked file by its TRUE owner ------------------------------------
# The judgement lives in tools/attribution_report.py so it can be contract-tested without a
# container (see tests/test_attribution_report.py). Exit: 0 pass, 1 misattribution, 2 inconclusive.
set +e
"$PYTHON" "${ROOT}/tools/attribution_report.py" \
    --manifest "${LIBRARY}/manifest.json" \
    --db "${CONFIG}/database/listenarr.db" \
    --book-id "$BOOK_ID" \
    --label "$LABEL" \
    ${JSON_OUT:+--json "$JSON_OUT"}
VERDICT=$?
set -e
exit "$VERDICT"
