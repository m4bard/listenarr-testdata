#!/usr/bin/env bash
#
# validate_chapter_grouping.sh — does Library Import group a chapter-per-file book into ONE item?
#
# THE QUESTION THIS ANSWERS.
#
# A book stored as forty chapter files is still one book, and the unmatched scan is supposed to
# say so: one scan item, forty source files, one search, one decision. When it does not, the
# operator is asked to identify the same book once per file. That is not a cosmetic failure —
# it is the difference between adding a book and adding it forty times.
#
# UnmatchedScanProcessor.ExtractTitleStem decides which filenames belong together. It strips a
# leading track number and a trailing Part/CD/Disc/Chapter/pt number, and it deliberately keeps a
# bare "(N)" suffix, with a comment saying so: plain numeric parens are treated as distinguishing
# separate books in a series. Nothing strips an "N of M" suffix at all. So this runs the SAME
# book, in the SAME layout, with the SAME tags, through several filename conventions and reads
# back how many scan items each produced:
#
#   multi-part     "Title - Part 01.mp3"     the CONTROL. Already handled by the trailing-part
#                                            strip, so it must come back as one item.
#   paren-index    "Title (1).mp3"           the bare-parens convention the comment keeps.
#   n-of-m         "Title 001 of 006.mp3"    an index that carries its own total.
#
# THE CONTROL IS THE POINT. If every case explodes, including multi-part, then this machine is
# reproducing "multi-file books never group" and has said nothing about any one convention. The
# report calls that INCONCLUSIVE rather than a finding. A check whose cases all agree no matter
# what the code does cannot fail, and cannot pass either.
#
# The tag axis is measured too, because it changes the answer. BuildGroupedFilesForFolder groups
# on filenames first and only reads embedded tags when that produced more than one group, so a
# folder whose files all carry the same title tag can be rescued by the metadata pass even when
# the filenames defeated the stem. Each convention therefore runs under all three title-tag
# states a real library contains, because only one of them can rescue anything:
#
#   none          no title tag. Nothing for the metadata pass to compare.
#   book title    every file tagged with the book's title. The metadata pass can see they match.
#   per-chapter   every file tagged with its own chapter name, which is how most chapter splits
#                 are actually tagged. The tags are present and correct and say nothing about
#                 which files belong together.
#
# Running only the middle one would report the rescue and miss the bug; running only the first
# would report the bug and invite "then tag your files" as the answer.
#
# Nothing is added to the library: the unmatched scan reports what is NOT in it, so an empty
# library and a populated root folder is the whole setup. A pinned ffprobe is provisioned first
# so the metadata pass is not lost to the first-boot download race — without it the tagged cases
# would silently degrade into the untagged ones.
#
#   ./tools/validate_chapter_grouping.sh --image ghcr.io/listenarrs/listenarr:canary
#
# Exit 0 every case grouped, 1 a case did not, 2 inconclusive (nothing indexed, or the control
# did not group).
#
set -uo pipefail
unset TMOUT

IMAGE="ghcr.io/listenarrs/listenarr:canary"
ASIN="B081B7JM9F"          # Little Women — a title with no digits and no " of " to confuse a stem
LAYOUT="author-title"      # {author}/{title}: renders for a standalone book and for a series one
SEED=1
PORT=4655
SETTLE=180                 # seconds to wait for the scan job to reach a terminal status
LABEL=""
JSON_OUT=""
KEEP=0
FFMPEG_SOURCE="jellyfin"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${ROOT}/.venv/bin/python"
LIBRARY="${ROOT}/build/grouping-library"
CONFIG="${ROOT}/build/grouping-config"
CONTAINER="listenarr-grouping-$$"

# "case name:structure:tag state:per-chapter titles". Each tag block leads with its control.
CASES=(
    "notags-multi-part:multi-part:no-tags:0"
    "notags-paren-index:paren-index:no-tags:0"
    "notags-n-of-m:n-of-m:no-tags:0"
    "booktag-multi-part:multi-part:correct-no-asin:0"
    "booktag-paren-index:paren-index:correct-no-asin:0"
    "booktag-n-of-m:n-of-m:correct-no-asin:0"
    "chaptertag-multi-part:multi-part:correct-no-asin:1"
    "chaptertag-paren-index:paren-index:correct-no-asin:1"
    "chaptertag-n-of-m:n-of-m:correct-no-asin:1"
)
CONTROLS=(notags-multi-part booktag-multi-part chaptertag-multi-part)

usage() {
    cat <<EOF
validate_chapter_grouping.sh — count the scan items a chapter-per-file book produces.

  --image REF       container image to test (default: ${IMAGE})
  --asin ASIN       corpus book to lay out in every convention (default: ${ASIN})
  --layout KEY      on-disk layout (default: ${LAYOUT})
  --port N          host port (default: ${PORT})
  --seed N          generator seed (default: ${SEED})
  --settle N        seconds to wait for the scan job (default: ${SETTLE})
  --ffmpeg-source S jellyfin | johnvansickle | system (default: ${FFMPEG_SOURCE})
  --label TEXT      label for the report header (default: the image ref)
  --json PATH       also write the result as JSON
  --keep            leave the container running for inspection
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --image)         IMAGE="$2";         shift 2 ;;
        --asin)          ASIN="$2";          shift 2 ;;
        --layout)        LAYOUT="$2";        shift 2 ;;
        --port)          PORT="$2";          shift 2 ;;
        --seed)          SEED="$2";          shift 2 ;;
        --settle)        SETTLE="$2";        shift 2 ;;
        --ffmpeg-source) FFMPEG_SOURCE="$2"; shift 2 ;;
        --label)         LABEL="$2";         shift 2 ;;
        --json)          JSON_OUT="$2";      shift 2 ;;
        --keep)          KEEP=1;             shift ;;
        -h|--help)       usage; exit 0 ;;
        *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done
[ -n "$LABEL" ] || LABEL="$IMAGE"

log() { printf '%s [grouping] %s\n' "$(date +%H:%M:%S)" "$*"; }
die() { printf '%s [grouping] ERROR: %s\n' "$(date +%H:%M:%S)" "$*" >&2; exit 2; }

if command -v podman >/dev/null 2>&1; then RUNTIME=podman
elif docker info >/dev/null 2>&1; then RUNTIME=docker
else die "no usable container runtime (podman not installed, docker daemon unreachable)"; fi
[ -x "$PY" ] || die "no venv — python3 -m venv .venv && .venv/bin/pip install -e ."

cleanup() {
    [ "$KEEP" -eq 1 ] && { log "leaving ${CONTAINER} on port ${PORT}"; return 0; }
    "$RUNTIME" rm -f "$CONTAINER" >/dev/null 2>&1
    return 0
}
trap cleanup EXIT

# --- 1. generate one subtree per case -------------------------------------------------
# Same book, same layout, one axis changed at a time. Each case gets its own top-level
# directory so the report can attribute a scan item to a case by its path alone.
rm -rf "$LIBRARY" "$CONFIG"; mkdir -p "$LIBRARY" "$CONFIG"
for spec in "${CASES[@]}"; do
    IFS=: read -r name structure tagstate chaptered <<<"$spec"
    CHAPTER_ARG=()
    [ "$chaptered" = "1" ] && CHAPTER_ARG=(--chapter-titles)
    "$PY" "${ROOT}/tools/generate_library.py" \
        --layout "$LAYOUT" --structure "$structure" --tag-state "$tagstate" \
        "${CHAPTER_ARG[@]}" \
        --only-asin "$ASIN" --out "${LIBRARY}/${name}" --seed "$SEED" \
        --ffmpeg-source "$FFMPEG_SOURCE" --force >/dev/null \
        || die "generation failed for ${name} (${structure}, ${tagstate})"
    n=$(find "${LIBRARY}/${name}" -type f ! -name manifest.json | wc -l)
    [ "$n" -gt 1 ] || die "${name} produced ${n} audio file(s); this needs a MULTI-file structure"
    log "generated ${name}: ${n} files (${structure}, ${tagstate}, chapter-titles=${chaptered})"
done

log "provisioning pinned ffprobe"
"$PY" "${ROOT}/tools/ffprobe_provisioner.py" --config-dir "$CONFIG" >/dev/null \
    || die "could not provision ffprobe"

# --- 2. start -------------------------------------------------------------------------
"$RUNTIME" rm -f "$CONTAINER" >/dev/null 2>&1
"$RUNTIME" run -d --name "$CONTAINER" -p "${PORT}:4545" -e LISTENARR_LOG_LEVEL=Debug \
    -v "${LIBRARY}:/audiobooks" -v "${CONFIG}:/app/config" "$IMAGE" >/dev/null \
    || die "could not start ${IMAGE}"

API="http://localhost:${PORT}/api/v1"
log "waiting for ${IMAGE} to answer"
for _ in $(seq 1 120); do curl -fsS "${API}/system/status" >/dev/null 2>&1 && break; sleep 2; done
curl -fsS "${API}/system/status" >/dev/null 2>&1 || {
    "$RUNTIME" logs "$CONTAINER" 2>&1 | tail -20; die "API never came up"; }

KEY=$("$PY" -c "import json; print(json.load(open('${CONFIG}/config.json'))['ApiKey'])") \
    || die "no ApiKey in ${CONFIG}/config.json"
AUTH=(-H "X-Api-Key: ${KEY}" -H 'Content-Type: application/json')

FOLDER_ID=$(curl -fsS -X POST "${API}/rootfolders" "${AUTH[@]}" \
    -d '{"name":"grouping","path":"/audiobooks","isDefault":true,"caseSensitivityMode":"Sensitive"}' \
    | "$PY" -c "import json,sys; print(json.load(sys.stdin)['id'])") \
    || die "could not create the root folder"
log "root folder ${FOLDER_ID} -> /audiobooks"

# --- 3. scan for unmatched files ------------------------------------------------------
# Nothing was added to the library, so every generated file is unmatched and the scan has to
# decide, per folder, how many books it is looking at. That decision is the whole subject.
JOB=$(curl -fsS -X POST "${API}/rootfolders/${FOLDER_ID}/scan-unmatched" "${AUTH[@]}" \
    | "$PY" -c "import json,sys; print(json.load(sys.stdin)['jobId'])") \
    || die "scan-unmatched request failed"
log "unmatched scan job ${JOB}"

ITEMS="${ROOT}/build/grouping-items.json"
STATUS="unknown"
waited=0
while [ "$waited" -lt "$SETTLE" ]; do
    curl -fsS "${API}/rootfolders/unmatched-results/${JOB}" "${AUTH[@]}" -o "$ITEMS" 2>/dev/null
    STATUS=$("$PY" -c "import json;print(json.load(open('${ITEMS}')).get('status','unknown'))" 2>/dev/null || echo unknown)
    case "$STATUS" in
        Completed|Failed) break ;;
    esac
    sleep 3; waited=$((waited + 3))
done
log "job status ${STATUS} after ${waited}s"
if [ "$STATUS" = "Failed" ]; then
    "$RUNTIME" logs "$CONTAINER" 2>&1 | grep -iE "unmatched|scan" | tail -10
    die "the unmatched scan reported Failed — no grouping to judge"
fi
[ "$STATUS" = "Completed" ] || {
    "$RUNTIME" logs "$CONTAINER" 2>&1 | tail -15
    die "the unmatched scan never completed within ${SETTLE}s"; }

# --- 4. judge -------------------------------------------------------------------------
CONTROL_ARGS=()
for c in "${CONTROLS[@]}"; do CONTROL_ARGS+=(--control "$c"); done

set +e
"$PY" "${ROOT}/tools/chapter_grouping_report.py" \
    --library "$LIBRARY" --items "$ITEMS" --container-root /audiobooks \
    "${CONTROL_ARGS[@]}" --label "$LABEL" ${JSON_OUT:+--json "$JSON_OUT"}
VERDICT=$?
set -e
exit "$VERDICT"
