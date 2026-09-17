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
# --folder-variant makes the on-disk folder disagree with the RECORD, which is the state a
# tolerant folder matcher exists for. The record and the embedded tags keep the canonical
# form; only the path moves. Scope it to one ASIN so its sibling stays in ordinary form, and
# the same run then measures both halves of the question — does the matcher reach the variant
# folder, and does it stop there:
#
#   ./tools/validate_scan_attribution.sh --image ghcr.io/listenarrs/listenarr:canary \
#       --asin B0036HXZCO --layout author-title \
#       --only-asin B0036HXZCO,B0036I51QQ \
#       --folder-variant drop-leading-article:B0036HXZCO
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
RECORD_AUTHOR_SUFFIX=""
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${ROOT}/.venv/bin/python"
# Per-run, for the same reason CONTAINER is. Two concurrent runs used to share one library and
# one config directory, and the second to start would rm -rf and regenerate the tree the first
# was still scanning. The symptom is "manifest describes no files", which reads as a generator
# fault and sends you looking at the generator. --keep leaves the tree behind for inspection.
WORK="${ROOT}/build/attrib-$$"
LIBRARY="${WORK}/library"
CONFIG="${WORK}/config"
CONTAINER="listenarr-attrib-$$"
VARIANT_ARGS=()   # forwarded verbatim to generate_library.py --folder-variant

usage() {
    cat <<EOF
validate_scan_attribution.sh — show which files a scan attributes to one audiobook.

  --image REF       container image to test (default: ${IMAGE})
  --asin ASIN       the ONE book to add and scan (required)
  --only-asin LIST  comma-separated ASINs to put on disk (default: the whole corpus)
  --layout KEY      on-disk layout (default: ${LAYOUT})
  --folder-variant SPEC
                    spell one book's FOLDER differently from its record while the tags and
                    the record keep the canonical form, as KEY or KEY:ASIN,ASIN. Repeatable.
                    Run 'generate_library.py --list-folder-variants' for the keys, which is
                    the same list this forwards to rather than a copy of it. This is how a
                    tolerant folder matcher gets something to be tolerant of, and scoping it
                    to one ASIN leaves the sibling in ordinary form so an over-reach shows up.
  --record-author-suffix TEXT
                    append TEXT to every author in the RECORD that gets added, leaving the
                    on-disk tree canonical. The mirror image of --folder-variant: that one
                    moves the folder and keeps the record, this one moves the record and
                    keeps the folder, and the two halves of an author tolerance are not the
                    same code. The stored record is read back afterwards and the run aborts
                    if the suffix did not survive, because a metadata lookup silently
                    replacing the authors would otherwise look exactly like a clean result.
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
        --folder-variant) VARIANT_ARGS+=(--folder-variant "$2"); shift 2 ;;
        --port)      PORT="$2";      shift 2 ;;
        --seed)      SEED="$2";      shift 2 ;;
        --label)     LABEL="$2";     shift 2 ;;
        --json)      JSON_OUT="$2";  shift 2 ;;
        --library)   USE_LIBRARY="$2"; shift 2 ;;
        --record-author-suffix) RECORD_AUTHOR_SUFFIX="$2"; shift 2 ;;
        --keep)      KEEP=1;         shift ;;
        -h|--help)   usage; exit 0 ;;
        *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done

log() { printf '%s [%s] %s\n' "$(date +%H:%M:%S)" "$1" "${*:2}"; }
die() { log ERROR "$*"; exit 1; }

[[ -n "$ASIN" ]] || die "--asin is required (see --help)"
[[ -n "$LABEL" ]] || LABEL="$IMAGE"

# The project venv by preference; fall back to python3 so the generation half of this script
# (and its tests) work anywhere the project is importable, e.g. a CI runner with `pip install -e .`.
if [[ ! -x "$PYTHON" ]]; then
    command -v python3 >/dev/null 2>&1 || die "no python3, and no venv at ${ROOT}/.venv"
    PYTHON="$(command -v python3)"
fi

cleanup() {
    if [[ "$KEEP" -eq 1 ]]; then
        log INFO "leaving ${CONTAINER} running on port ${PORT}"
        log INFO "leaving ${WORK} in place"
        return
    fi
    [[ -n "${RUNTIME:-}" ]] && "$RUNTIME" rm -f "$CONTAINER" >/dev/null 2>&1
    # Only ever the directory this run made. A prepared --library belongs to the caller.
    [[ -d "$WORK" ]] && rm -rf "$WORK"
    return 0
}
trap cleanup EXIT

# --- 1. generate (or reuse a prepared library) ------------------------------------------
if [[ -n "$USE_LIBRARY" ]]; then
    # A prepared tree, for shapes the generator does not express (e.g. an author folder
    # spelled as a variant). Its manifest must already describe the paths as they are.
    # Absolutise before anything else touches it. The container runtime treats a relative
    # source as a NAMED VOLUME, not a bind mount, so a relative --library fails later with
    # "names must match [a-zA-Z0-9]..." from volume create, which reads as a container
    # problem rather than a path one and sends you looking in the wrong place.
    # --folder-variant is an instruction to the generator, and this branch does not generate.
    # Silently ignoring it would scan an ordinary tree and report a clean pass for a case that
    # was never on disk, which is the worst way for these two flags to interact.
    [[ ${#VARIANT_ARGS[@]} -eq 0 ]] \
        || die "--folder-variant and --library are mutually exclusive: --library uses a tree that already exists, so there is nothing left to vary"
    [[ -d "$USE_LIBRARY" ]] || die "--library ${USE_LIBRARY} is not a directory"
    USE_LIBRARY="$(cd "$USE_LIBRARY" && pwd)"
    [[ -f "${USE_LIBRARY}/manifest.json" ]] || die "${USE_LIBRARY} has no manifest.json"
    LIBRARY="$USE_LIBRARY"
    rm -rf "$CONFIG"; mkdir -p "$CONFIG"
    log INFO "using prepared library ${LIBRARY}"
else
    log INFO "generating library (layout ${LAYOUT}, seed ${SEED})"
    rm -rf "$LIBRARY" "$CONFIG"; mkdir -p "$CONFIG"
    "$PYTHON" "${ROOT}/tools/generate_library.py" \
        --layout "$LAYOUT" --out "$LIBRARY" --seed "$SEED" --force \
        ${ONLY_ASIN:+--only-asin "$ONLY_ASIN"} \
        ${VARIANT_ARGS+"${VARIANT_ARGS[@]}"} >/dev/null \
        || die "generation failed"
fi
FILES=$(find "$LIBRARY" -type f ! -name manifest.json | wc -l)
log INFO "library: ${FILES} audio files"
# Fail fast and say why. The usual cause is a layout the chosen books cannot express: the
# default {author}/{series}/{title} skips anything with no series, so a set of standalone
# books yields an empty tree and there is nothing to attribute.
[[ "$FILES" -gt 0 ]] || die "the library is empty — the '${LAYOUT}' layout could not render any of the requested books (a layout with {series} skips books that have none; try --layout author-title)"

# Only now does this need a container: the inputs are known good and there is something to scan.
if command -v podman >/dev/null 2>&1; then RUNTIME=podman
elif docker info >/dev/null 2>&1; then RUNTIME=docker
else die "no usable container runtime (podman not installed, docker daemon unreachable)"; fi
command -v sqlite3 >/dev/null 2>&1 || die "sqlite3 is required"

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
export ROOT API API_KEY ASIN RECORD_AUTHOR_SUFFIX
BOOK_ID=$("$PYTHON" - <<'PY'
import json, os, urllib.request
books = json.load(open(os.path.join(os.environ["ROOT"], "corpus", "corpus.json")))["books"]
book = next(b for b in books if b["asin"] == os.environ["ASIN"])
suffix = os.environ.get("RECORD_AUTHOR_SUFFIX") or ""
authors = [f"{a}{suffix}" for a in book["authors"]]
payload = json.dumps({
    "metadata": {
        "asin": book["asin"], "title": book["title"], "authors": authors,
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
# The control for --record-author-suffix. The add path is free to reconcile the posted
# metadata against a provider lookup, and if it did, the record would hold the canonical
# authors and the run would measure nothing while reporting a perfectly ordinary result.
# Read the stored row back and refuse to continue unless the suffix is actually in it.
if [[ -n "$RECORD_AUTHOR_SUFFIX" ]]; then
    STORED_AUTHORS=$(sqlite3 "${CONFIG}/database/listenarr.db" \
        "SELECT Authors FROM Audiobooks WHERE Id='${BOOK_ID}';")
    [[ "$STORED_AUTHORS" == *"$RECORD_AUTHOR_SUFFIX"* ]] \
        || die "the record did not keep '${RECORD_AUTHOR_SUFFIX}' (stored: ${STORED_AUTHORS}) — the add path replaced the posted authors, so this run would have measured nothing"
    log INFO "record authors: ${STORED_AUTHORS}"
fi
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
