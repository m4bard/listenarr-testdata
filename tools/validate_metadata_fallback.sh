#!/usr/bin/env bash
#
# validate_metadata_fallback.sh — can a scan still claim a file by its embedded tags?
#
# A scan attributes files by path first: an identifier in the path, a title-bearing folder with
# author context around it, or a filename matching the title. When none of those bite there is a
# second pass that opens each remaining candidate, reads its tags, and claims it if the tags name
# the book. For a correctly tagged file in a folder shape the path heuristics do not recognise,
# that second pass is the only route to being claimed at all.
#
# This runs two libraries through a real scan:
#
#   fallback   the book sits in a folder carrying its title and NOTHING else, so there is no
#              author context anywhere on the path and every path heuristic declines. Tags are
#              correct. Only the metadata pass can claim it.
#   control    the same book in a layout the path heuristics do handle. MUST be claimed.
#
# The control is not decoration. "Nothing was claimed" is also what a broken harness prints — a
# bad image, an unmounted library, a root folder that never took, a scan request that 404'd. A
# fallback failure is only a statement about the fallback if the control passed in the same run,
# so --mode both runs them in that order and refuses to report a fallback verdict if the control
# did not claim its file.
#
# Each mode gets its own container. The registration registry remembers files it has already
# registered, so reusing one container would let the first mode's ownership decide the second's.
#
#   ./tools/validate_metadata_fallback.sh --image ghcr.io/listenarrs/listenarr:canary
#   ./tools/validate_metadata_fallback.sh --mode control      # the working negative, alone
#
# Exit 0 the file was claimed in every mode run, 1 a mode that had to claim did not, 2 the run
# could not be judged (no ffprobe, no library, control failed).
#
set -uo pipefail
unset TMOUT

IMAGE="ghcr.io/listenarrs/listenarr:canary"
ASIN="B002UUFXKU"          # The Valley of Fear: single file, tagged with its real ASIN
SEED=1
PORT=4670
MODE="both"
LABEL=""
JSON_DIR=""
KEEP=0
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${ROOT}/.venv/bin/python"
FFMPEG_SOURCE="jellyfin"

usage() {
    cat <<EOF
validate_metadata_fallback.sh — does the embedded-metadata pass still claim files?

  --image REF     container image (default: ${IMAGE})
  --asin ASIN     book to generate and scan (default: ${ASIN})
  --mode MODE     fallback | control | both (default: ${MODE})
  --seed N        generator seed (default: ${SEED})
  --port N        host port (default: ${PORT})
  --label TEXT    label for the report header
  --json DIR      write <mode>.json into DIR
  --ffmpeg-source jellyfin|johnvansickle|system  (default: ${FFMPEG_SOURCE})
  --keep          leave containers running
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --image)  IMAGE="$2";  shift 2 ;;
        --asin)   ASIN="$2";   shift 2 ;;
        --mode)   MODE="$2";   shift 2 ;;
        --seed)   SEED="$2";   shift 2 ;;
        --port)   PORT="$2";   shift 2 ;;
        --label)  LABEL="$2";  shift 2 ;;
        --json)   JSON_DIR="$2"; shift 2 ;;
        --ffmpeg-source) FFMPEG_SOURCE="$2"; shift 2 ;;
        --keep)   KEEP=1;      shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done
[ -n "$LABEL" ] || LABEL="$IMAGE"

case "$MODE" in
    fallback|control|both) ;;
    *) echo "unknown --mode '$MODE' (fallback|control|both)" >&2; exit 2 ;;
esac

log() { printf '%s [fallback] %s\n' "$(date +%H:%M:%S)" "$*"; }
die() { printf '%s [fallback] ERROR: %s\n' "$(date +%H:%M:%S)" "$*" >&2; exit 2; }

if command -v podman >/dev/null 2>&1; then RUNTIME=podman
elif docker info >/dev/null 2>&1; then RUNTIME=docker
else die "no usable container runtime"; fi
[ -x "$PY" ] || die "no venv — python3 -m venv .venv && .venv/bin/pip install -e ."
command -v sqlite3 >/dev/null 2>&1 || die "sqlite3 is required"
[ -n "$JSON_DIR" ] && mkdir -p "$JSON_DIR"

CONTAINERS=()
cleanup() {
    if [ "$KEEP" -eq 1 ]; then
        log "leaving ${CONTAINERS[*]:-nothing} running"
        return 0
    fi
    for c in "${CONTAINERS[@]:-}"; do
        [ -n "$c" ] && "$RUNTIME" rm -f "$c" >/dev/null 2>&1
    done
    return 0
}
trap cleanup EXIT

# Layout per mode. `title-only` puts the title in the folder and no author anywhere, which is what
# starves every path heuristic; `listenarr-native` is the shape they were written for.
layout_for() {
    case "$1" in
        fallback) echo "title-only" ;;
        control)  echo "listenarr-native" ;;
    esac
}

run_mode() {
    local mode="$1" port="$2"
    local layout; layout="$(layout_for "$mode")"
    local library="${ROOT}/build/fallback-${mode}-library"
    local config="${ROOT}/build/fallback-${mode}-config"
    local container="listenarr-fallback-${mode}-$$"
    local logfile="${ROOT}/build/fallback-${mode}.log"
    CONTAINERS+=("$container")

    log "[${mode}] generating '${layout}' for ${ASIN}"
    rm -rf "$library" "$config"; mkdir -p "$config"
    "$PY" "${ROOT}/tools/generate_library.py" \
        --scenario tag-fallback-rescue --layout "$layout" --only-asin "$ASIN" \
        --out "$library" --seed "$SEED" --force \
        --ffmpeg-source "$FFMPEG_SOURCE" >/dev/null \
        || { log "[${mode}] generation failed"; return 2; }

    local files
    files=$("$PY" - "$library" "$ASIN" <<'COUNTEOF'
import json, os, sys
lib, asin = sys.argv[1], sys.argv[2]
man = json.load(open(os.path.join(lib, "manifest.json")))
print(sum(1 for e in man["entries"]
          if e.get("belongs_to_asin") == asin and e.get("kind") == "book"))
COUNTEOF
)
    [ "${files:-0}" -ge 1 ] || { log "[${mode}] ${ASIN} generated no files"; return 2; }

    # The metadata pass only means anything if ffprobe is actually present. Without it the server
    # returns null before reaching the probe and an unclaimed file would prove nothing.
    "$PY" "${ROOT}/tools/ffprobe_provisioner.py" --config-dir "$config" >/dev/null \
        || { log "[${mode}] could not provision ffprobe"; return 2; }

    "$RUNTIME" rm -f "$container" >/dev/null 2>&1
    "$RUNTIME" run -d --name "$container" -p "${port}:4545" -e LISTENARR_LOG_LEVEL=Debug \
        -v "${library}:/audiobooks" -v "${config}:/app/config" "$IMAGE" >/dev/null \
        || { log "[${mode}] could not start ${IMAGE}"; return 2; }

    local api="http://localhost:${port}/api/v1"
    local i
    for i in $(seq 1 120); do curl -fsS "${api}/system/status" >/dev/null 2>&1 && break; sleep 2; done
    curl -fsS "${api}/system/status" >/dev/null 2>&1 \
        || { log "[${mode}] API never came up"; return 2; }

    local key
    key=$("$PY" -c "import json; print(json.load(open('${config}/config.json'))['ApiKey'])") \
        || { log "[${mode}] no ApiKey"; return 2; }
    local auth=(-H "X-Api-Key: ${key}" -H 'Content-Type: application/json')

    curl -fsS -X POST "${api}/rootfolders" "${auth[@]}" \
        -d '{"name":"lib","path":"/audiobooks","isDefault":true,"caseSensitivityMode":"Sensitive"}' \
        >/dev/null || { log "[${mode}] could not create the root folder"; return 2; }

    local book_id
    book_id=$(ROOT="$ROOT" API="$api" KEY="$key" ASIN="$ASIN" "$PY" - <<'ADDEOF'
import json, os, urllib.request
books = json.load(open(os.path.join(os.environ["ROOT"], "corpus", "corpus.json")))["books"]
book = next(b for b in books if b["asin"] == os.environ["ASIN"])
payload = json.dumps({
    "metadata": {"asin": book["asin"], "title": book["title"], "authors": book["authors"],
                 "narrators": book["narrators"], "series": book["series"],
                 "seriesNumber": book["series_position"], "source": "Audible",
                 "region": book["region"]},
    "monitored": True, "autoSearch": False,
}).encode()
req = urllib.request.Request(f"{os.environ['API']}/library/add", data=payload, method="POST",
                             headers={"Content-Type": "application/json",
                                      "X-Api-Key": os.environ["KEY"]})
with urllib.request.urlopen(req, timeout=60) as r:
    body = json.loads(r.read().decode() or "{}")
print(body.get("id") or (body.get("audiobook") or {}).get("id") or "")
ADDEOF
) || { log "[${mode}] could not add ${ASIN}"; return 2; }
    [ -n "$book_id" ] || { log "[${mode}] add returned no id"; return 2; }
    log "[${mode}] added ${ASIN} as audiobook ${book_id}"

    # Clear BasePath so the scan walks the root the way it does for a book that has never been
    # matched, rather than being handed the answer.
    "$RUNTIME" stop "$container" >/dev/null 2>&1
    sqlite3 "${config}/database/listenarr.db" "UPDATE Audiobooks SET BasePath = NULL;"
    "$RUNTIME" start "$container" >/dev/null 2>&1
    for i in $(seq 1 120); do curl -fsS "${api}/system/status" >/dev/null 2>&1 && break; sleep 2; done

    log "[${mode}] scanning"
    curl -fsS -X POST "${api}/library/${book_id}/scan" "${auth[@]}" \
        -d '{"path":"/audiobooks"}' >/dev/null \
        || { log "[${mode}] scan request failed"; return 2; }
    for i in $(seq 1 45); do
        n=$(sqlite3 "${config}/database/listenarr.db" \
            "SELECT COUNT(*) FROM AudiobookFiles WHERE AudiobookId='${book_id}';" 2>/dev/null || echo 0)
        [ "$n" != "0" ] && break
        sleep 2
    done
    sleep 5
    "$RUNTIME" logs "$container" > "$logfile" 2>&1

    "$PY" "${ROOT}/tools/metadata_fallback_probe.py" \
        --manifest "${library}/manifest.json" --library "$library" \
        --db "${config}/database/listenarr.db" --book-id "$book_id" --asin "$ASIN" \
        --mode "$mode" --log "$logfile" --label "$LABEL" \
        ${JSON_DIR:+--json "${JSON_DIR}/${mode}.json"}
    return $?
}

CONTROL_RC=0
FALLBACK_RC=0

if [ "$MODE" = "control" ] || [ "$MODE" = "both" ]; then
    echo
    run_mode control "$PORT"; CONTROL_RC=$?
    echo
fi

if [ "$MODE" = "both" ] && [ "$CONTROL_RC" -ne 0 ]; then
    log "control did not claim its file, so a fallback result would say nothing about the"
    log "fallback. Fix the harness before reading any further."
    exit 2
fi

if [ "$MODE" = "fallback" ] || [ "$MODE" = "both" ]; then
    run_mode fallback "$((PORT + 1))"; FALLBACK_RC=$?
    echo
fi

if [ "$MODE" = "both" ]; then
    if [ "$CONTROL_RC" -eq 0 ] && [ "$FALLBACK_RC" -eq 1 ]; then
        log "control claimed its file, fallback did not: the metadata pass is the thing that failed"
    fi
    [ "$FALLBACK_RC" -ne 0 ] && exit "$FALLBACK_RC"
    exit "$CONTROL_RC"
fi

[ "$MODE" = "control" ] && exit "$CONTROL_RC"
exit "$FALLBACK_RC"
