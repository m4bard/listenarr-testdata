#!/usr/bin/env bash
#
# validate_root_folder_delete.sh — can a root folder that owns nothing be deleted? (Listenarr#602)
#
# #602 reports a root folder that refuses to delete. It has sat for three months with a hypothesis
# and no confirmation: T4g1 guessed that "the current algorithm checks that the audiobook base path
# starts with the root folder... it will indeed have troubles with root folder that are contained
# within each others", and the reporter gave up and renamed the folder instead.
#
# The hypothesis is readable in the source. `EfRootFolderRepository.HasAudiobooksUnderPathAsync`
# gates deletion on
#
#     a.BasePath == rootPath || a.BasePath.StartsWith(rootPath + Path.DirectorySeparatorChar)
#
# and `RootFolderService.CreateAsync` rejects only an EXACT duplicate path, so nothing stops a root
# being created inside another root. An outer root that owns no books of its own then matches every
# book under the inner one, and can never be deleted.
#
# Three scenarios, each in its own container because root rows and BasePaths are the subject:
#
#   sibling          CONTROL. Two roots that do not nest; delete the one owning nothing. This must
#                    succeed. If it fails, the gate is broken more generally than nesting and the
#                    nested result below says nothing specific, so the run refuses a verdict.
#   nested-delete    The reported case. Outer root contains inner root, book lives under inner,
#                    delete the outer.
#   nested-reassign  The documented escape hatch, `DELETE /rootfolders/{id}?reassignTo={other}`.
#                    With nested roots the affected set includes books already under the
#                    destination, so this asks what their stored path becomes and whether it still
#                    points at anything real.
#
# Exit 0 nothing wrong found, 1 the outer root could not be deleted or a path was corrupted,
# 2 nothing conclusive (including a failed control).
#
set -uo pipefail
unset TMOUT

IMAGE="ghcr.io/listenarrs/listenarr:canary"
ASIN="B002UUFXKU"
SEED=1
PORT=4780
SCENARIOS="sibling,nested-delete,nested-reassign"
LABEL=""
JSON_OUT=""
KEEP=0
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${ROOT}/.venv/bin/python"
BUILD="${ROOT}/build/root-delete"
CONTAINER="listenarr-rootdel-$$"

usage() {
    cat <<EOF
validate_root_folder_delete.sh — can a root folder that owns nothing be deleted? (#602)

  --image REF        container image (default: ${IMAGE})
  --asin ASIN        book to place (default: ${ASIN})
  --scenarios LIST   comma-separated (default: ${SCENARIOS})
  --seed N           generator seed (default: ${SEED})
  --port N           host port (default: ${PORT})
  --label TEXT       label for the report header
  --json PATH        also write the result as JSON
  --keep             leave the last container running
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --image)     IMAGE="$2";     shift 2 ;;
        --asin)      ASIN="$2";      shift 2 ;;
        --scenarios) SCENARIOS="$2"; shift 2 ;;
        --seed)      SEED="$2";      shift 2 ;;
        --port)      PORT="$2";      shift 2 ;;
        --label)     LABEL="$2";     shift 2 ;;
        --json)      JSON_OUT="$2";  shift 2 ;;
        --keep)      KEEP=1;         shift ;;
        -h|--help)   usage; exit 0 ;;
        *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done
[ -n "$LABEL" ] || LABEL="$IMAGE"

log() { printf '%s [rootdel] %s\n' "$(date +%H:%M:%S)" "$*"; }
die() { printf '%s [rootdel] ERROR: %s\n' "$(date +%H:%M:%S)" "$*" >&2; exit 2; }

if command -v podman >/dev/null 2>&1; then RUNTIME=podman
elif docker info >/dev/null 2>&1; then RUNTIME=docker
else die "no usable container runtime"; fi
[ -x "$PY" ] || die "no venv — python3 -m venv .venv && .venv/bin/pip install -e ."

cleanup() {
    [ "$KEEP" -eq 1 ] && { log "leaving ${CONTAINER}"; return 0; }
    "$RUNTIME" rm -f "$CONTAINER" >/dev/null 2>&1
    return 0
}
trap cleanup EXIT

DATA="${BUILD}/data"
CONFIG="${BUILD}/config"

run_scenario() {
    local scenario="$1" port="$2" row_out="$3"

    rm -rf "$BUILD"; mkdir -p "$CONFIG" "$DATA/alpha" "$DATA/beta"
    # The book is generated into the INNER directory so its files really live under the nested root
    # rather than the arrangement being faked in the database.
    "$PY" "${ROOT}/tools/generate_library.py" --layout loose --out "${DATA}/inner" \
        --seed "$SEED" --only-asin "$ASIN" --force >/dev/null || die "generation failed"

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

    SCENARIO="$scenario" ROW_OUT="$row_out" API="$API" KEY="$KEY" DATA_HOST="$DATA" \
        "$PY" "${ROOT}/tools/root_folder_delete_probe.py"
}

export ROOT ASIN

echo "===== ${LABEL} ====="
ROWS=()
OFFSET=0
IFS=',' read -r -a SCENARIO_LIST <<< "$SCENARIOS"
for scenario in "${SCENARIO_LIST[@]}"; do
    [ -n "$scenario" ] || continue
    ROW="${ROOT}/build/rootdel-row-${scenario}.json"
    mkdir -p "$(dirname "$ROW")"
    run_scenario "$scenario" "$((PORT + OFFSET))" "$ROW"
    ROWS+=("$ROW")
    OFFSET=$((OFFSET + 1))
done

RESULT_JSON="${ROOT}/build/rootdel-result.json"
LABEL="$LABEL" RESULT_JSON="$RESULT_JSON" ROW_FILES="${ROWS[*]}" "$PY" <<'RUNEOF'
import json, os, sys

rows = []
for path in os.environ["ROW_FILES"].split():
    try:
        with open(path) as handle:
            rows.append(json.load(handle))
    except (OSError, json.JSONDecodeError):
        rows.append({"scenario": os.path.basename(path), "verdict": "inconclusive",
                     "reason": "probe produced no result"})

with open(os.environ["RESULT_JSON"], "w") as handle:
    json.dump({"image": os.environ.get("LABEL", ""), "scenarios": rows}, handle, indent=2)

EXPLAIN = {
    "control-ok":      "an unrelated empty root deletes cleanly",
    "control-failed":  "even a non-nested empty root refuses to delete",
    "reproduced":      "outer root owns nothing yet cannot be deleted",
    "deleted":         "outer root deleted despite containing the inner root",
    "corrupted":       "reassign rewrote the path to somewhere that does not exist",
    "unchanged":       "reassign left the stored path alone",
    "rewritten-valid": "reassign changed the path and it still resolves",
    "inconclusive":    "nothing to judge",
}

print("\n===== VERDICT =====")
for row in rows:
    print(f"  {row['scenario']:<17} {row['verdict']:<16} {EXPLAIN.get(row['verdict'], '')}")

control = next((r for r in rows if r["scenario"] == "sibling"), None)
if control is not None and control["verdict"] != "control-ok":
    print("\nCHECK NOT SOUND: the control did not pass, so deletion is failing for some reason")
    print("wider than nesting. Treat the other verdicts as unproven.")
    sys.exit(2)
if control is not None:
    print("\nControl passed, so deletion works in general and the nested results are about nesting.")

if any(r.get("nesting_accepted") for r in rows):
    print("Worth noting on its own: the API accepted a root folder nested inside another root.")

problems = [r for r in rows if r["verdict"] in ("reproduced", "corrupted")]
if problems:
    for row in problems:
        if row["verdict"] == "reproduced":
            print("\n#602 reproduced. The outer root owns no books of its own, yet deletion is")
            print("refused because the gate matches any BasePath starting with its path.")
        if row["verdict"] == "corrupted":
            print(f"\nReassign corrupted the stored path: {row.get('base_path_before')!r}")
            print(f"became {row.get('base_path_after')!r}, which does not exist on disk.")
    sys.exit(1)
if all(r["verdict"] in ("control-ok", "deleted", "unchanged", "rewritten-valid") for r in rows):
    print("\nNothing wrong found.")
    sys.exit(0)
sys.exit(2)
RUNEOF
RESULT=$?

if [ -n "$JSON_OUT" ] && [ -f "$RESULT_JSON" ]; then
    cp "$RESULT_JSON" "$JSON_OUT" && log "wrote ${JSON_OUT}"
fi
exit "$RESULT"
