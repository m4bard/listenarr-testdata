#!/usr/bin/env bash
#
# validate_rename_hazards.sh — does a real rename lose a file or escape the library root?
#
# This is the destructive axis, and the only check here where a failure means data is gone rather
# than a book merely being unlinked. The generator writes the hazards for real: path-illegal
# characters, the 255-BYTE component limit (bytes, not characters, so a Cyrillic or CJK title
# overflows at about a third of the character count you would expect), Windows MAX_PATH, reserved
# device names like CON, NFC/NFD normalization, case collisions, and path traversal written straight
# into an embedded tag. That last one matters most: tags are attacker-controlled input, and a title
# of `../../../../etc` interpolated into a rename target escapes the library root.
#
# The pieces for this already existed and nothing joined them up. `generate_library.py --scenario
# rename-hazards` writes the corpus and `verify_scan.py --snapshot` / `--audit` asserts the two
# properties that matter, but the documented flow had a manual "now run Listenarr's rename" step in
# the middle, so the highest-stakes assertion in the repo had never been run automatically.
#
#   before   inventory every file by CONTENT hash
#   drive    add the books, scan so the files are tracked, force a relocation, EXECUTE the rename
#   audit    assert no content was lost and nothing escaped the root
#
# Files are tracked by content rather than by name because a rename is precisely a change of name.
# The question is not whether a given path still exists but whether every byte that was there still
# exists somewhere under the root. The audit counts copies per hash, so clobbering one of two
# byte-identical files is caught rather than hidden by the survivor.
#
# It EXECUTES the rename rather than reading the preview. `validate_sidecar_rename.sh` asserts on the
# plan, which is the right call for the question it asks, but a plan that reads correctly is not
# evidence about what ends up on disk.
#
# WHAT A CLEAN RUN DOES AND DOES NOT PROVE. A clean run on Linux proves POSIX-safety, not Windows or
# macOS safety. Several hazards only MANIFEST on the filesystem they target: a case collision does
# not destroy anything on case-sensitive ext4 because both files coexist, but silently overwrites on
# NTFS or APFS, and a reserved name or a MAX_PATH overflow is a perfectly legal filename on Linux.
# The data-loss and traversal assertions are filesystem-agnostic and meaningful everywhere. The
# case-collision and reserved-name outcomes are only exercised on a case-insensitive or Windows host.
# The run prints this rather than leaving a green result to be over-read.
#
#   ./tools/validate_rename_hazards.sh --image ghcr.io/listenarrs/listenarr:canary
#
# Exit 0 nothing lost and nothing escaped, 1 something was lost or escaped, 2 the run could not be
# judged (including a rename that moved nothing, which is untested rather than clean).
#
set -uo pipefail
unset TMOUT

IMAGE="ghcr.io/listenarrs/listenarr:canary"
SEED=1
LIMIT=""
PORT=4800
PATTERN='Sorted/{Author}/{Title}'
LABEL=""
JSON_OUT=""
KEEP=0
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${ROOT}/.venv/bin/python"
BUILD="${ROOT}/build/rename-hazards"
CONTAINER="listenarr-hazards-$$"

usage() {
    cat <<EOF
validate_rename_hazards.sh — does a real rename lose a file or escape the root?

  --image REF     container image (default: ${IMAGE})
  --limit N       use only the first N corpus books (default: all hazard books)
  --seed N        generator seed (default: ${SEED})
  --pattern PAT   folder naming pattern that forces the relocation
                  (default: ${PATTERN})
  --port N        host port (default: ${PORT})
  --label TEXT    label for the report header
  --json PATH     also write the result as JSON
  --keep          leave the container running
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --image)   IMAGE="$2";   shift 2 ;;
        --limit)   LIMIT="$2";   shift 2 ;;
        --seed)    SEED="$2";    shift 2 ;;
        --pattern) PATTERN="$2"; shift 2 ;;
        --port)    PORT="$2";    shift 2 ;;
        --label)   LABEL="$2";   shift 2 ;;
        --json)    JSON_OUT="$2"; shift 2 ;;
        --keep)    KEEP=1;       shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done
[ -n "$LABEL" ] || LABEL="$IMAGE"

log() { printf '%s [hazards] %s\n' "$(date +%H:%M:%S)" "$*"; }
die() { printf '%s [hazards] ERROR: %s\n' "$(date +%H:%M:%S)" "$*" >&2; exit 2; }

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
BEFORE="${BUILD}/before.json"
PROBE_OUT="${BUILD}/probe.json"
LIB_REMOTE="/audiobooks"

log "generating the path-hazard corpus"
rm -rf "$BUILD"; mkdir -p "$CONFIG"
GEN_ARGS=(--scenario rename-hazards --out "$LIBRARY" --seed "$SEED" --force)
[ -n "$LIMIT" ] && GEN_ARGS+=(--limit "$LIMIT")
"$PY" "${ROOT}/tools/generate_library.py" "${GEN_ARGS[@]}" >/dev/null || die "generation failed"

FILE_COUNT=$(find "$LIBRARY" -type f ! -name manifest.json | wc -l)
[ "$FILE_COUNT" -gt 0 ] || die "the generator produced no files"
log "generated ${FILE_COUNT} file(s) carrying hazards"

log "snapshotting every file by content hash (the before state)"
"$PY" "${ROOT}/tools/verify_scan.py" --manifest "${LIBRARY}/manifest.json" \
    --snapshot "$BEFORE" >/dev/null || die "snapshot failed"

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
AUTH=(-H "X-Api-Key: ${KEY}" -H 'Content-Type: application/json')

curl -fsS -X POST "${API}/rootfolders" "${AUTH[@]}" \
    -d "{\"name\":\"lib\",\"path\":\"${LIB_REMOTE}\",\"isDefault\":true,\"caseSensitivityMode\":\"Sensitive\"}" \
    >/dev/null || die "could not create the root folder"

echo "===== ${LABEL} ====="
API="$API" KEY="$KEY" ROOT="$ROOT" LIB_REMOTE="$LIB_REMOTE" \
    MANIFEST="${LIBRARY}/manifest.json" RESULT_OUT="$PROBE_OUT" PATTERN="$PATTERN" \
    "$PY" "${ROOT}/tools/rename_hazards_probe.py"
PROBE_STATUS=$?

if [ "$PROBE_STATUS" -eq 3 ]; then
    echo
    echo "INCONCLUSIVE: the rename moved nothing, so the audit below would be vacuous. That is an"
    echo "untested run rather than a clean one."
    exit 2
fi
[ "$PROBE_STATUS" -eq 0 ] || die "could not drive the rename (probe exit ${PROBE_STATUS})"

echo
log "auditing: no content lost, nothing escaped the root"
"$PY" "${ROOT}/tools/verify_scan.py" --manifest "${LIBRARY}/manifest.json" --audit "$BEFORE"
AUDIT=$?

echo
echo "===== WHAT THIS RUN DOES AND DOES NOT COVER ====="
if [ -f "$PROBE_OUT" ]; then
    "$PY" - "$PROBE_OUT" <<'COVEOF'
import json, sys
with open(sys.argv[1]) as handle:
    r = json.load(handle)
print(f"  exercised: {r.get('tracked_files')}/{r.get('total_files')} hazard files "
      f"({r.get('coverage_pct')}%), {r.get('plans_with_changes')} of {r.get('plans')} "
      f"plans relocated")
print("  A clean audit is only as strong as the fraction actually moved. Compare this number")
print("  between two images before comparing their verdicts.")
COVEOF
fi
cat <<'CAVEAT'
  Data loss and path traversal are filesystem-agnostic, so those results mean the same anywhere.
  Case collisions and reserved device names are written faithfully but only MANIFEST on a
  case-insensitive or Windows filesystem: on case-sensitive ext4 both members of a collision simply
  coexist, and CON is an ordinary filename. A clean run here is evidence about POSIX behaviour, not
  about Windows or macOS. Run it on those hosts too before calling a renamer safe there.
CAVEAT

if [ -n "$JSON_OUT" ] && [ -f "$PROBE_OUT" ]; then
    cp "$PROBE_OUT" "$JSON_OUT" && log "wrote ${JSON_OUT}"
fi
exit "$AUDIT"
