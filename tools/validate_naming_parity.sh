#!/usr/bin/env bash
#
# validate_naming_parity.sh — does one naming pattern produce one layout, whichever path applies it?
#
# `FolderNamingPattern` is applied from more than one place in Listenarr, and the places do not all
# supply the same tokens. At merged canary `ManualImportPathPlanner` contains no reference to
# `SeriesNumber` at all, while `RenameService.Helpers.cs:204` populates it. An unknown token does not
# raise: `FileNamingService` resolves it to an empty sentinel and then cleans up the separator that
# was next to it, so `{Author}/{Series}/{SeriesNumber} - {Title}` silently collapses to
# `{Author}/{Series}/{Title}` on the import path only.
#
# The effect is a setting that means two different things depending on how the file arrived. A book
# imported today sits in one folder; press Organize tomorrow and it moves, with nothing having
# changed but the code path.
#
# This puts ONE book and ONE pattern through both and compares:
#
#   import   the destination the manual-import planner computed
#   rename   the destination the rename preview planned
#
# It reports rather than judging which is correct. Whether the position belongs in the folder name is
# a product decision; the two paths disagreeing about it is not.
#
# The default pattern deliberately uses `{SeriesNumber}`, since that is the token known to differ.
# Pass `--pattern` to probe another one; any token supplied by one path and not the other shows up
# the same way.
#
#   ./tools/validate_naming_parity.sh --image ghcr.io/listenarrs/listenarr:canary
#
# Exit 0 the two agree, 1 they disagree, 2 nothing conclusive.
#
set -uo pipefail
unset TMOUT

IMAGE="ghcr.io/listenarrs/listenarr:canary"
ASIN="B002UUFXKU"          # The Valley of Fear, Sherlock Holmes #7, so the token is non-empty
SEED=1
PORT=4850
PATTERN='{Author}/{Series}/{SeriesNumber} - {Title}'
LABEL=""
JSON_OUT=""
KEEP=0
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${ROOT}/.venv/bin/python"
BUILD="${ROOT}/build/naming-parity"
CONTAINER="listenarr-parity-$$"

usage() {
    cat <<EOF
validate_naming_parity.sh — does one naming pattern mean one thing on every code path?

  --image REF      container image (default: ${IMAGE})
  --asin ASIN      book to use; MUST have a series position (default: ${ASIN})
  --pattern PAT    folder naming pattern to probe
                   (default: ${PATTERN})
  --seed N         generator seed (default: ${SEED})
  --port N         host port (default: ${PORT})
  --label TEXT     label for the report header
  --json PATH      also write the result as JSON
  --keep           leave the container running
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --image)   IMAGE="$2";   shift 2 ;;
        --asin)    ASIN="$2";    shift 2 ;;
        --pattern) PATTERN="$2"; shift 2 ;;
        --seed)    SEED="$2";    shift 2 ;;
        --port)    PORT="$2";    shift 2 ;;
        --label)   LABEL="$2";   shift 2 ;;
        --json)    JSON_OUT="$2"; shift 2 ;;
        --keep)    KEEP=1;       shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done
[ -n "$LABEL" ] || LABEL="$IMAGE"

log() { printf '%s [parity] %s\n' "$(date +%H:%M:%S)" "$*"; }
die() { printf '%s [parity] ERROR: %s\n' "$(date +%H:%M:%S)" "$*" >&2; exit 2; }

if command -v podman >/dev/null 2>&1; then RUNTIME=podman
elif docker info >/dev/null 2>&1; then RUNTIME=docker
else die "no usable container runtime"; fi
[ -x "$PY" ] || die "no venv — python3 -m venv .venv && .venv/bin/pip install -e ."

cleanup() {
    [ "$KEEP" -eq 1 ] && { log "leaving ${CONTAINER} on port ${PORT}"; return 0; }
    "$RUNTIME" rm -f "$CONTAINER" >/dev/null 2>&1
    return 0
}
trap cleanup EXIT

LIBRARY="${BUILD}/library"
CONFIG="${BUILD}/config"
RESULT="${BUILD}/result.json"
LIB_REMOTE="/audiobooks"

log "generating one book as a loose file"
rm -rf "$BUILD"; mkdir -p "$CONFIG"
"$PY" "${ROOT}/tools/generate_library.py" --layout loose --out "$LIBRARY" \
    --seed "$SEED" --only-asin "$ASIN" --force >/dev/null || die "generation failed"
[ "$(find "$LIBRARY" -maxdepth 1 -name '*.m4b' | wc -l)" -ge 1 ] \
    || die "${ASIN} produced no files; try another --asin"

"$PY" "${ROOT}/tools/ffprobe_provisioner.py" --config-dir "$CONFIG" >/dev/null \
    || die "could not provision ffprobe"

"$RUNTIME" rm -f "$CONTAINER" >/dev/null 2>&1
"$RUNTIME" run -d --name "$CONTAINER" -p "${PORT}:4545" -e LISTENARR_LOG_LEVEL=Debug \
    -v "${LIBRARY}:${LIB_REMOTE}" -v "${CONFIG}:/app/config" "$IMAGE" >/dev/null \
    || die "could not start ${IMAGE}"

API="http://localhost:${PORT}/api/v1"
for _ in $(seq 1 120); do curl -fsS "${API}/system/status" >/dev/null 2>&1 && break; sleep 2; done
curl -fsS "${API}/system/status" >/dev/null 2>&1 || die "API never came up"

KEY=$("$PY" -c "import json; print(json.load(open('${CONFIG}/config.json'))['ApiKey'])") \
    || die "no ApiKey"
curl -fsS -X POST "${API}/rootfolders" -H "X-Api-Key: ${KEY}" -H 'Content-Type: application/json' \
    -d "{\"name\":\"lib\",\"path\":\"${LIB_REMOTE}\",\"isDefault\":true,\"caseSensitivityMode\":\"Sensitive\"}" \
    >/dev/null || die "could not create the root folder"

echo "===== ${LABEL} ====="
API="$API" KEY="$KEY" ASIN="$ASIN" ROOT="$ROOT" LIB_REMOTE="$LIB_REMOTE" \
    SCAN_HOST="$LIBRARY" PATTERN="$PATTERN" RESULT_OUT="$RESULT" \
    "$PY" "${ROOT}/tools/naming_parity_probe.py"
RESULT_CODE=$?

if [ -n "$JSON_OUT" ] && [ -f "$RESULT" ]; then
    cp "$RESULT" "$JSON_OUT" && log "wrote ${JSON_OUT}"
fi

if [ "$RESULT_CODE" -eq 0 ] && [ -f "$RESULT" ]; then
    VERDICT=$("$PY" -c "import json,sys; print(json.load(open('$RESULT')).get('verdict',''))" 2>/dev/null)
    [ "$VERDICT" = "disagree" ] && exit 1
    [ "$VERDICT" = "agree" ] && exit 0
fi
exit 2
