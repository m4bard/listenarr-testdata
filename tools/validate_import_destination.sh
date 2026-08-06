#!/usr/bin/env bash
#
# validate_import_destination.sh — does Library Import honour the destination root? (Listenarr#798)
#
# Reported bug: with two root folders configured, scanning one and importing into the other puts the
# organized Author/Title folders inside the SCANNED root, ignoring the destination that was picked.
#
# The destination never travels on the import call itself. ManualImportRequestDto carries only path,
# mode, action and a per-item matchedAudiobookId — there is no destination field on it. The choice
# reaches the backend earlier, when the book is added, and the frontend has two different ways of
# getting it there depending on whether the book is already known:
#
#   new         POST /library/add with destinationPath, which LibraryAddWorkflow stores as BasePath.
#   preexisting the add returns 409, so it falls back to PUT /library/{id} with basePath. That call
#               is wrapped in a try/catch that swallows failure ("Non-critical — import continues,
#               file may go to OutputPath fallback"), so this branch can fail silently.
#
# Both are exercised, separately, because they are different code paths that produce one symptom.
# Guessing which one a reporter hit is how you end up fixing the wrong branch.
#
# The library is generated with the 'loose' layout: files sit flat at the scan root, the way a drop
# folder from OpenAudible or a torrent client actually looks. The verdict then reads the filesystem
# rather than the API, because where the bytes ended up is the thing in dispute:
#
#   organized under the destination root   the import honoured the choice
#   organized under the scanned root       the choice was ignored — the reported bug
#
# A flat file still sitting at the scan root is the untouched source, not an import, so only files in
# a SUBDIRECTORY count as organized. A pinned ffprobe is provisioned up front so the metadata step
# does not lose the first-boot download race.
#
#   ./tools/validate_import_destination.sh --image ghcr.io/listenarrs/listenarr:canary
#
# Exit 0 both modes honoured the destination, 1 at least one organized into the scanned root,
# 2 nothing was imported so there is nothing to judge.
#
set -uo pipefail
unset TMOUT

IMAGE="ghcr.io/listenarrs/listenarr:canary"
ASIN="B002UUFXKU"          # The Valley of Fear: one file, so the destination is unambiguous
SEED=1
PORT=4655
MODE="both"
LABEL=""
JSON_OUT=""
KEEP=0
PRIME_LOCK_DIR=0
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${ROOT}/.venv/bin/python"
BUILD="${ROOT}/build/import-destination"
CONTAINER="listenarr-dest-$$"

usage() {
    cat <<EOF
validate_import_destination.sh — does Library Import organize into the destination root? (#798)

  --image REF     container image (default: ${IMAGE})
  --asin ASIN     book to import (default: ${ASIN})
  --mode MODE     new | preexisting | both (default: ${MODE})
  --seed N        generator seed (default: ${SEED})
  --port N        host port (default: ${PORT})
  --label TEXT    label for the report header
  --json PATH     also write the result as JSON
  --keep          leave the container running
  --prime-lock-dir
                  create \$HOME/.local/share in the container before importing.
                  Only needed on PR#717, where FileMover's new cross-process move
                  gate throws "A per-user application-data directory is required
                  for file-move locks" because .NET returns an empty
                  LocalApplicationData when that directory does not exist — which
                  it does not, in the image the Dockerfile builds. Every file
                  mutation is blocked until it exists, so without this flag the
                  run cannot get far enough to judge the destination at all.
                  Off by default: the blocker is worth seeing, not papering over.
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --image)  IMAGE="$2";    shift 2 ;;
        --asin)   ASIN="$2";     shift 2 ;;
        --mode)   MODE="$2";     shift 2 ;;
        --seed)   SEED="$2";     shift 2 ;;
        --port)   PORT="$2";     shift 2 ;;
        --label)  LABEL="$2";    shift 2 ;;
        --json)   JSON_OUT="$2"; shift 2 ;;
        --keep)   KEEP=1;        shift ;;
        --prime-lock-dir) PRIME_LOCK_DIR=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done
[ -n "$LABEL" ] || LABEL="$IMAGE"
case "$MODE" in
    new|preexisting|both) ;;
    *) echo "--mode must be new, preexisting or both" >&2; exit 2 ;;
esac

log() { printf '%s [dest] %s\n' "$(date +%H:%M:%S)" "$*"; }
die() { printf '%s [dest] ERROR: %s\n' "$(date +%H:%M:%S)" "$*" >&2; exit 2; }

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

# Each mode gets its OWN container and its own freshly generated library. Sharing one instance
# across modes looks tempting and is wrong: an earlier mode's import leaves a registered file behind,
# and #717's ownership registry remembers it even after the audiobook row is deleted. That leftover
# turned a later mode into a spurious "Failed to import file" that had nothing to do with the
# destination. Isolation is cheaper than explaining a result that was never real.
run_mode_in_fresh_container() {
    local mode="$1" port="$2" row_out="$3"

    rm -rf "$BUILD"; mkdir -p "$CONFIG" "$DEST_HOST"
    "$PY" "${ROOT}/tools/generate_library.py" --layout loose --out "$SCAN_HOST" \
        --seed "$SEED" --only-asin "$ASIN" --force >/dev/null || die "generation failed"

    local src_count
    src_count=$(find "$SCAN_HOST" -maxdepth 1 -name '*.m4b' | wc -l)
    [ "$src_count" -ge 1 ] || die "${ASIN} produced no loose files; try another --asin"

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

    if [ "$PRIME_LOCK_DIR" -eq 1 ]; then
        "$RUNTIME" exec "$CONTAINER" sh -c 'mkdir -p "${HOME:-/root}/.local/share"' \
            || die "could not create the lock directory"
    fi

    KEY=$("$PY" -c "import json; print(json.load(open('${CONFIG}/config.json'))['ApiKey'])") \
        || die "no ApiKey"
    local auth=(-H "X-Api-Key: ${KEY}" -H 'Content-Type: application/json')

    # The destination is the DEFAULT root, exactly as the report describes it. If the import fell back
    # to "the default root" for any reason it would land in the right place by accident, so the
    # scanned root is deliberately the non-default one: landing there can only mean the scan root won.
    curl -fsS -X POST "${API}/rootfolders" "${auth[@]}" \
        -d "{\"name\":\"books\",\"path\":\"${SCAN_ROOT}\",\"isDefault\":false,\"caseSensitivityMode\":\"Sensitive\"}" >/dev/null \
        || die "could not create the scanned root folder"
    curl -fsS -X POST "${API}/rootfolders" "${auth[@]}" \
        -d "{\"name\":\"library\",\"path\":\"${DEST_ROOT}\",\"isDefault\":true,\"caseSensitivityMode\":\"Sensitive\"}" >/dev/null \
        || die "could not create the destination root folder"

    MODE="$mode" ROW_OUT="$row_out" API="$API" KEY="$KEY" \
        "$PY" "${ROOT}/tools/import_destination_probe.py"
}

export ROOT ASIN SCAN_ROOT DEST_ROOT SCAN_HOST DEST_HOST LABEL

if [ "$MODE" = "both" ]; then MODES=(new preexisting unpatched); else MODES=("$MODE"); fi

echo "===== ${LABEL} ====="
ROWS=()
OFFSET=0
for m in "${MODES[@]}"; do
    ROW="${BUILD_ROWS:-${ROOT}/build}/row-${m}.json"
    mkdir -p "$(dirname "$ROW")"
    run_mode_in_fresh_container "$m" "$((PORT + OFFSET))" "$ROW"
    ROWS+=("$ROW")
    OFFSET=$((OFFSET + 1))
done

RESULT_JSON="${ROOT}/build/dest-result.json"
LABEL="$LABEL" RESULT_JSON="$RESULT_JSON" ROW_FILES="${ROWS[*]}" "$PY" <<'RUNEOF'
import json, os, sys

rows = []
for path in os.environ["ROW_FILES"].split():
    try:
        rows.append(json.load(open(path)))
    except (OSError, json.JSONDecodeError):
        rows.append({"mode": os.path.basename(path), "verdict": "inconclusive",
                     "reason": "probe produced no result"})

json.dump({"image": os.environ.get("LABEL", ""), "modes": rows},
          open(os.environ["RESULT_JSON"], "w"), indent=2)

print("\n===== VERDICT =====")
for r in rows:
    note = "  (control: SHOULD reproduce)" if r["mode"] == "unpatched" else ""
    print(f"  {r['mode']:<12} {r['verdict']}{note}")

control = next((r for r in rows if r["mode"] == "unpatched"), None)
judged = [r for r in rows if r["mode"] != "unpatched"]

# Every import failing looks like "no bug here" and is really "nothing was testable". Say so, and
# say why, because the cause is usually a blocked file mutation rather than a destination choice.
blocked = [r for r in rows if r.get("verdict") == "inconclusive" and r.get("errors")]
if blocked:
    print("\nImports failed rather than landing anywhere. Reported errors:")
    for r in blocked:
        for message in r.get("errors", []):
            print(f"  [{r['mode']}] {message[:200]}")
    print("On PR#717 this is usually the file-move gate: it needs $HOME/.local/share to exist and\n"
          "the image never creates it. Re-run with --prime-lock-dir to get past it.")

# The control has to fail, or nothing the other modes report means anything.
if control is not None and control["verdict"] != "reproduced":
    print("\nCHECK NOT SOUND: the control case did not land in the scanned root, so this tool "
          "cannot currently detect the bug it is looking for. Treat the other verdicts as unproven.")
    sys.exit(2)
if control is not None:
    print("\nControl reproduced as expected, so a real regression here would be caught.")

if any(r["verdict"] == "reproduced" for r in judged):
    sys.exit(1)
if judged and all(r["verdict"] == "pass" for r in judged):
    sys.exit(0)
sys.exit(2)
RUNEOF
RESULT=$?

if [ -n "$JSON_OUT" ] && [ -f "$RESULT_JSON" ]; then
    cp "$RESULT_JSON" "$JSON_OUT" && log "wrote ${JSON_OUT}"
fi
exit "$RESULT"
