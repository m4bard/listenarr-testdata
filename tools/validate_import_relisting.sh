#!/usr/bin/env bash
#
# validate_import_relisting.sh — is an already-imported file offered again for import? (Listenarr#616)
#
# #616 asks for Library Import to stop listing things already in the library. Most of that thread is
# a product argument and this tool deliberately stays out of it: whether a book that already has
# files belongs in the list is a UX decision, and s3ntin3l8 makes a reasonable case in the thread
# that Sonarr and Radarr show everything and solve it with a search box instead.
#
# There is a narrower question underneath that is not a matter of taste. After you import a file, is
# that same file offered again as an unmatched candidate?
#
# The backend already intends to say no. `UnmatchedScanBackgroundService` builds a set of tracked
# paths from `AudiobookFiles` plus `Audiobook.FilePath` and filters the walk against it, under a
# comment reading "so that files already in the library are not reported as unmatched". The filter
# compares PATHS. That is exact for a move, where the source path stops existing, but a copy or a
# hardlink leaves the original file where it was while the database records the destination, so the
# original is not in the tracked set.
#
# Each action is run separately because they differ in exactly the way that matters:
#
#   copy      source survives at its old path, destination tracked
#   hardlink  source survives at its old path, same inode, destination tracked
#   move      source path stops existing
#
# `move` is included as the honest comparison. If it reports nothing afterwards that is absence
# rather than filtering, and the run says so rather than counting it as a pass.
#
# Two soundness guards, because "nothing was listed" is the answer both a working filter and a blind
# check produce:
#
#   baseline  the drop folder's file MUST be reported before the import, or the check cannot see
#             the thing it is watching and the run is inconclusive.
#   control   the library root's own file is tracked, so a correct filter drops it. If it is
#             reported, the path filter is broken generally and the drop-folder result proves little.
#
#   ./tools/validate_import_relisting.sh --image ghcr.io/listenarrs/listenarr:canary
#
# Exit 0 no surviving source was re-listed, 1 at least one was, 2 nothing conclusive.
#
set -uo pipefail
unset TMOUT

IMAGE="ghcr.io/listenarrs/listenarr:canary"
ASIN="B002UUFXKU"          # The Valley of Fear: one file, so the candidate list is unambiguous
SEED=1
PORT=4750
# The hardlink action is spelled "hardlink/copy" in FileAction, not "hardlink". Sending the wrong
# spelling does not error: the import just returns no results, which reads like a broken check.
ACTIONS="copy,hardlink/copy,move"
LABEL=""
JSON_OUT=""
KEEP=0
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${ROOT}/.venv/bin/python"
BUILD="${ROOT}/build/import-relist"
CONTAINER="listenarr-relist-$$"

usage() {
    cat <<EOF
validate_import_relisting.sh — is an imported file offered again as a candidate? (#616)

  --image REF     container image (default: ${IMAGE})
  --asin ASIN     book to import (default: ${ASIN})
  --actions LIST  comma-separated import actions (default: ${ACTIONS})
  --seed N        generator seed (default: ${SEED})
  --port N        host port (default: ${PORT})
  --label TEXT    label for the report header
  --json PATH     also write the result as JSON
  --keep          leave the last container running
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --image)   IMAGE="$2";   shift 2 ;;
        --asin)    ASIN="$2";    shift 2 ;;
        --actions) ACTIONS="$2"; shift 2 ;;
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

log() { printf '%s [relist] %s\n' "$(date +%H:%M:%S)" "$*"; }
die() { printf '%s [relist] ERROR: %s\n' "$(date +%H:%M:%S)" "$*" >&2; exit 2; }

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

DATA="${BUILD}/data"
CONFIG="${BUILD}/config"
SCAN_HOST="${DATA}/books"
DEST_HOST="${DATA}/library"
SCAN_ROOT="/audiobooks/books"
DEST_ROOT="/audiobooks/library"

# Each action gets its own container and library. Sharing one instance would carry tracked rows from
# the previous action into the next, and the tracked set is the entire subject of this check.
run_action() {
    local action="$1" port="$2" row_out="$3"

    rm -rf "$BUILD"; mkdir -p "$CONFIG" "$DEST_HOST"
    "$PY" "${ROOT}/tools/generate_library.py" --layout loose --out "$SCAN_HOST" \
        --seed "$SEED" --only-asin "$ASIN" --force >/dev/null || die "generation failed"
    [ "$(find "$SCAN_HOST" -maxdepth 1 -name '*.m4b' | wc -l)" -ge 1 ] \
        || die "${ASIN} produced no loose files; try another --asin"

    "$PY" "${ROOT}/tools/ffprobe_provisioner.py" --config-dir "$CONFIG" >/dev/null \
        || die "could not provision ffprobe"

    "$RUNTIME" rm -f "$CONTAINER" >/dev/null 2>&1
    "$RUNTIME" run -d --name "$CONTAINER" -p "${port}:4545" -e LISTENARR_LOG_LEVEL=Debug \
        -v "${DATA}:/audiobooks" -v "${CONFIG}:/app/config" "$IMAGE" >/dev/null \
        || die "could not start ${IMAGE}"

    API="http://localhost:${port}/api/v1"
    local _
    for _ in $(seq 1 120); do curl -fsS "${API}/system/status" >/dev/null 2>&1 && break; sleep 2; done
    curl -fsS "${API}/system/status" >/dev/null 2>&1 || die "API never came up"

    KEY=$("$PY" -c "import json; print(json.load(open('${CONFIG}/config.json'))['ApiKey'])") \
        || die "no ApiKey"
    local auth=(-H "X-Api-Key: ${KEY}" -H 'Content-Type: application/json')

    SCAN_ROOT_ID=$(curl -fsS -X POST "${API}/rootfolders" "${auth[@]}" \
        -d "{\"name\":\"books\",\"path\":\"${SCAN_ROOT}\",\"isDefault\":false,\"caseSensitivityMode\":\"Sensitive\"}" \
        | "$PY" -c "import json,sys; d=json.load(sys.stdin); print(d.get('id') or (d.get('rootFolder') or {}).get('id') or '')")
    DEST_ROOT_ID=$(curl -fsS -X POST "${API}/rootfolders" "${auth[@]}" \
        -d "{\"name\":\"library\",\"path\":\"${DEST_ROOT}\",\"isDefault\":true,\"caseSensitivityMode\":\"Sensitive\"}" \
        | "$PY" -c "import json,sys; d=json.load(sys.stdin); print(d.get('id') or (d.get('rootFolder') or {}).get('id') or '')")
    [ -n "$SCAN_ROOT_ID" ] && [ -n "$DEST_ROOT_ID" ] || die "could not create the root folders"

    ACTION="$action" ROW_OUT="$row_out" API="$API" KEY="$KEY" \
        SCAN_ROOT_ID="$SCAN_ROOT_ID" DEST_ROOT_ID="$DEST_ROOT_ID" \
        "$PY" "${ROOT}/tools/import_relist_probe.py"
}

export ROOT ASIN SCAN_ROOT DEST_ROOT SCAN_HOST

echo "===== ${LABEL} ====="
ROWS=()
OFFSET=0
IFS=',' read -r -a ACTION_LIST <<< "$ACTIONS"
for action in "${ACTION_LIST[@]}"; do
    [ -n "$action" ] || continue
    ROW="${ROOT}/build/relist-row-${action//\//-}.json"     # "hardlink/copy" is not a filename
    mkdir -p "$(dirname "$ROW")"
    run_action "$action" "$((PORT + OFFSET))" "$ROW"
    ROWS+=("$ROW")
    OFFSET=$((OFFSET + 1))
done

RESULT_JSON="${ROOT}/build/relist-result.json"
LABEL="$LABEL" RESULT_JSON="$RESULT_JSON" ROW_FILES="${ROWS[*]}" "$PY" <<'RUNEOF'
import json, os, sys

rows = []
for path in os.environ["ROW_FILES"].split():
    try:
        with open(path) as handle:
            rows.append(json.load(handle))
    except (OSError, json.JSONDecodeError):
        rows.append({"action": os.path.basename(path), "verdict": "inconclusive",
                     "reason": "probe produced no result"})

with open(os.environ["RESULT_JSON"], "w") as handle:
    json.dump({"image": os.environ.get("LABEL", ""), "actions": rows}, handle, indent=2)

EXPLAIN = {
    "relisted":     "source survived and is still offered",
    "filtered":     "source survived and is no longer offered",
    "moot":         "source gone, so nothing to offer (not a test of the filter)",
    "unsound":      "control failed: tracked file reported as unmatched",
    "inconclusive": "nothing to judge",
}

print("\n===== VERDICT =====")
for row in rows:
    print(f"  {row['action']:<14} {row['verdict']:<13} {EXPLAIN.get(row['verdict'], '')}")

if any(r["verdict"] == "unsound" for r in rows):
    print("\nCHECK NOT SOUND: at least one control scan reported a tracked file, so the path filter")
    print("is not working generally. Treat every other verdict in this run as unproven.")
    sys.exit(2)

relisted = [r for r in rows if r["verdict"] == "relisted"]
if relisted:
    print("\nA file that survives its own import is still offered as an unmatched candidate for:")
    print("  " + ", ".join(r["action"] for r in relisted))
    print("The tracked-path set records the destination, and a surviving source sits at a different")
    print("path, so it does not match. This is narrower than what #616 asks for, and unlike the rest")
    print("of that thread it does not depend on a view about what the list should show.")
    sys.exit(1)

if all(r["verdict"] in ("filtered", "moot") for r in rows) and any(r["verdict"] == "filtered" for r in rows):
    print("\nEvery surviving source was filtered out after import.")
    sys.exit(0)
print("\nNothing conclusive: no action left a surviving source to judge.")
sys.exit(2)
RUNEOF
RESULT=$?

if [ -n "$JSON_OUT" ] && [ -f "$RESULT_JSON" ]; then
    cp "$RESULT_JSON" "$JSON_OUT" && log "wrote ${JSON_OUT}"
fi
exit "$RESULT"
