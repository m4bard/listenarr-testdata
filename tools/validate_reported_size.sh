#!/usr/bin/env bash
#
# validate_reported_size.sh — does the size shown for an audiobook match the files it owns?
#
# Listenarr#542 reports two things: the total size shown for a book is not the sum of its files, and
# some books show no total at all. Both are invisible to a unit test that checks one file, because
# the fault is in how the per-book value relates to the file set rather than in any single record.
#
# So this generates a book that HAS many files, scans it for real, and then compares three numbers
# that are easy to conflate:
#
#   on disk        the real bytes, summed from the generated files. Ground truth.
#   linked by scan what the library recorded per file, summed. Says whether the scan saw them all.
#   reported       the single value shown for the book.
#
# Keeping them apart matters: if the linked sum is right and the reported value is not, the summary
# is derived wrongly, and if the linked sum is short then the scan missed files and the summary is
# only wrong further downstream.
#
# The book's BasePath is cleared before scanning, so the scan walks the library root the way it does
# for a book that has not been matched yet. A pinned ffprobe is provisioned up front so the metadata
# step does not lose the first-boot download race.
#
#   ./tools/validate_reported_size.sh --image ghcr.io/listenarrs/listenarr:canary
#
# Exit 0 the reported size matches, 1 it is wrong or missing, 2 nothing linked so nothing to judge.
#
set -uo pipefail
unset TMOUT

IMAGE="ghcr.io/listenarrs/listenarr:canary"
SCENARIO="multi-file-books"
ASIN="B002UZJF4U"          # The Three Musketeers: 40 chapter files, so a per-file value is obvious
SEED=1
LIMIT=3
PORT=4650
LABEL=""
JSON_OUT=""
KEEP=0
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${ROOT}/.venv/bin/python"
LIBRARY="${ROOT}/build/size-library"
CONFIG="${ROOT}/build/size-config"
CONTAINER="listenarr-size-$$"

usage() {
    cat <<EOF
validate_reported_size.sh — compare a book's reported size with the files it owns.

  --image REF     container image (default: ${IMAGE})
  --scenario KEY  scenario to generate (default: ${SCENARIO})
  --asin ASIN     the multi-file book to add and scan (default: ${ASIN})
  --limit N       corpus books to generate (default: ${LIMIT})
  --seed N        generator seed (default: ${SEED})
  --port N        host port (default: ${PORT})
  --label TEXT    label for the report header
  --json PATH     also write the result as JSON
  --keep          leave the container running
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --image)    IMAGE="$2";    shift 2 ;;
        --scenario) SCENARIO="$2"; shift 2 ;;
        --asin)     ASIN="$2";     shift 2 ;;
        --limit)    LIMIT="$2";    shift 2 ;;
        --seed)     SEED="$2";     shift 2 ;;
        --port)     PORT="$2";     shift 2 ;;
        --label)    LABEL="$2";    shift 2 ;;
        --json)     JSON_OUT="$2"; shift 2 ;;
        --keep)     KEEP=1;        shift ;;
        -h|--help)  usage; exit 0 ;;
        *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done
[ -n "$LABEL" ] || LABEL="$IMAGE"

log() { printf '%s [size] %s\n' "$(date +%H:%M:%S)" "$*"; }
die() { printf '%s [size] ERROR: %s\n' "$(date +%H:%M:%S)" "$*" >&2; exit 2; }

if command -v podman >/dev/null 2>&1; then RUNTIME=podman
elif docker info >/dev/null 2>&1; then RUNTIME=docker
else die "no usable container runtime"; fi
[ -x "$PY" ] || die "no venv — python3 -m venv .venv && .venv/bin/pip install -e ."
command -v sqlite3 >/dev/null 2>&1 || die "sqlite3 is required"

cleanup() {
    [ "$KEEP" -eq 1 ] && { log "leaving ${CONTAINER} on port ${PORT}"; return 0; }
    "$RUNTIME" rm -f "$CONTAINER" >/dev/null 2>&1
    return 0
}
trap cleanup EXIT

log "generating '${SCENARIO}'"
rm -rf "$LIBRARY" "$CONFIG"; mkdir -p "$CONFIG"
"$PY" "${ROOT}/tools/generate_library.py" --scenario "$SCENARIO" --out "$LIBRARY" \
    --seed "$SEED" --limit "$LIMIT" --force >/dev/null || die "generation failed"

FILES=$("$PY" - "$LIBRARY" "$ASIN" <<'COUNTEOF'
import json, os, sys
lib, asin = sys.argv[1], sys.argv[2]
man = json.load(open(os.path.join(lib, "manifest.json")))
print(sum(1 for e in man["entries"] if e.get("belongs_to_asin") == asin))
COUNTEOF
)
[ "${FILES:-0}" -gt 1 ] || die "${ASIN} has ${FILES:-0} generated file(s); this needs a MULTI-file book, try another --asin or --scenario"
log "target book has ${FILES} files on disk"

"$PY" "${ROOT}/tools/ffprobe_provisioner.py" --config-dir "$CONFIG" >/dev/null || die "could not provision ffprobe"

"$RUNTIME" rm -f "$CONTAINER" >/dev/null 2>&1
"$RUNTIME" run -d --name "$CONTAINER" -p "${PORT}:4545" -e LISTENARR_LOG_LEVEL=Debug \
    -v "${LIBRARY}:/audiobooks" -v "${CONFIG}:/app/config" "$IMAGE" >/dev/null || die "could not start ${IMAGE}"

API="http://localhost:${PORT}/api/v1"
for _ in $(seq 1 120); do curl -fsS "${API}/system/status" >/dev/null 2>&1 && break; sleep 2; done
curl -fsS "${API}/system/status" >/dev/null 2>&1 || die "API never came up"

KEY=$("$PY" -c "import json; print(json.load(open('${CONFIG}/config.json'))['ApiKey'])") || die "no ApiKey"
AUTH=(-H "X-Api-Key: ${KEY}" -H 'Content-Type: application/json')

curl -fsS -X POST "${API}/rootfolders" "${AUTH[@]}" \
    -d '{"name":"lib","path":"/audiobooks","isDefault":true,"caseSensitivityMode":"Sensitive"}' >/dev/null \
    || die "could not create the root folder"

export ROOT API KEY ASIN
BOOK_ID=$("$PY" - <<'ADDEOF'
import json, os, urllib.request
books = json.load(open(os.path.join(os.environ["ROOT"], "corpus", "corpus.json")))["books"]
book = next(b for b in books if b["asin"] == os.environ["ASIN"])
payload = json.dumps({
    "metadata": {"asin": book["asin"], "title": book["title"], "authors": book["authors"],
                 "narrators": book["narrators"], "series": book["series"],
                 "seriesNumber": book["series_position"], "source": "Audible", "region": book["region"]},
    "monitored": True, "autoSearch": False,
}).encode()
req = urllib.request.Request(f"{os.environ['API']}/library/add", data=payload, method="POST",
                             headers={"Content-Type": "application/json", "X-Api-Key": os.environ["KEY"]})
with urllib.request.urlopen(req, timeout=60) as r:
    body = json.loads(r.read().decode() or "{}")
print(body.get("id") or (body.get("audiobook") or {}).get("id") or "")
ADDEOF
) || die "could not add ${ASIN}"
[ -n "$BOOK_ID" ] || die "add returned no id"
log "added ${ASIN} as audiobook ${BOOK_ID}"

"$RUNTIME" stop "$CONTAINER" >/dev/null 2>&1
sqlite3 "${CONFIG}/database/listenarr.db" "UPDATE Audiobooks SET BasePath = NULL;"
"$RUNTIME" start "$CONTAINER" >/dev/null 2>&1
for _ in $(seq 1 120); do curl -fsS "${API}/system/status" >/dev/null 2>&1 && break; sleep 2; done

log "scanning"
curl -fsS -X POST "${API}/library/${BOOK_ID}/scan" "${AUTH[@]}" -d '{"path":"/audiobooks"}' >/dev/null \
    || die "scan request failed"
for _ in $(seq 1 60); do
    n=$(sqlite3 "${CONFIG}/database/listenarr.db" \
        "SELECT COUNT(*) FROM AudiobookFiles WHERE AudiobookId='${BOOK_ID}';" 2>/dev/null || echo 0)
    [ "$n" != "0" ] && break
    sleep 2
done
sleep 4

set +e
"$PY" "${ROOT}/tools/size_report.py" \
    --manifest "${LIBRARY}/manifest.json" --library "$LIBRARY" \
    --db "${CONFIG}/database/listenarr.db" --book-id "$BOOK_ID" --asin "$ASIN" \
    --label "$LABEL" ${JSON_OUT:+--json "$JSON_OUT"}
VERDICT=$?
set -e
exit "$VERDICT"
