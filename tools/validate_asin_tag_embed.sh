#!/usr/bin/env bash
#
# validate_asin_tag_embed.sh — after an import, does the ASIN actually land in the file's tags?
#
# Listenarr enriches a file it has just imported by writing the book's ASIN into the file's own
# embedded tags, so the file carries its identifier wherever it goes afterwards. That step is
# deliberately non-fatal: it is wrapped in a catch, it logs a warning, and the import reports
# success either way. A step that cannot fail the operation it belongs to is a step nobody
# notices has stopped working, and no unit test can see it — the writer is mocked in the
# controller tests, so what the real one does to a real file on a real filesystem is untested.
#
# So this drives a real file through the manual-import API and then reads the DESTINATION file's
# tags on the host, looking for the three places the writer puts an ASIN
# (`----:com.apple.iTunes:ASIN`, a `TXXX:ASIN` frame, or a Xiph `ASIN` field).
#
# Two gates run before the import, and the tool refuses a verdict unless both land, because the
# reader has to be shown capable of BOTH answers in the same run or neither answer means anything:
#
#   control   a copy of the same generated file with the ASIN atom stamped onto it by hand — the
#             exact atom the writer targets. The reader MUST call this `tagged`. If a file that
#             demonstrably carries the tag reads as untagged, the reader is broken and every
#             `untagged` verdict it has ever printed is worthless.
#   subject   the generated file as it goes in, which MUST read `untagged`. A generator that
#             embedded an ASIN of its own would make "an ASIN is present afterwards" true without
#             Listenarr having written anything, and the check would pass on a broken build.
#
# The server's own logs are then read as corroboration, the way a scan check reads its `Blocked`
# line: the writer logs either "Wrote ASIN tag" or "Failed to write ASIN tag". If NEITHER appears
# the enrichment step never ran at all — an unmatched book, a blank ASIN, an import that did not
# publish — and that is reported as inconclusive rather than as a finding.
#
# A pinned ffprobe is provisioned up front (tools/ffprobe_provisioner.py) so the import's own
# metadata step does not hard-fail on the first-boot download race.
#
#   ./tools/validate_asin_tag_embed.sh --image ghcr.io/listenarrs/listenarr:canary
#
# Exit 0 the ASIN was embedded, 1 it was not, 2 the run could not be judged (a gate failed, the
# import never completed, or the enrichment step never ran).
#
set -uo pipefail
unset TMOUT

IMAGE="ghcr.io/listenarrs/listenarr:canary"
ASIN="B002UUFXKU"          # The Valley of Fear: one file, and the generator tags it with no ASIN
TITLE="The Valley of Fear"
AUTHOR="Arthur Conan Doyle"
SCENARIO="existing-library-adoption"
SEED=1
PORT=4680
SETTLE=60                  # seconds to wait for a destination to appear before giving up
LABEL=""
JSON_DIR=""
KEEP=0
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${ROOT}/.venv/bin/python"
SRCDIR="${ROOT}/build/asintag-src"
LIBDIR="${ROOT}/build/asintag-library"
CONFIG="${ROOT}/build/asintag-config"
CONTAINER="listenarr-asintag-$$"

usage() {
    cat <<EOF
validate_asin_tag_embed.sh — does an imported file end up carrying its ASIN in its own tags?

  --image REF     container image (default: ${IMAGE})
  --asin ASIN     book to add and import (default: ${ASIN})
  --scenario KEY  scenario to generate the source file from (default: ${SCENARIO})
  --seed N        generator seed (default: ${SEED})
  --port N        host port (default: ${PORT})
  --settle N      seconds to wait for the destination (default: ${SETTLE})
  --label TEXT    label for the report header
  --json DIR      write control.json / subject.json / imported.json into DIR
  --keep          leave the container running
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --image)    IMAGE="$2";    shift 2 ;;
        --asin)     ASIN="$2";     shift 2 ;;
        --scenario) SCENARIO="$2"; shift 2 ;;
        --seed)     SEED="$2";     shift 2 ;;
        --port)     PORT="$2";     shift 2 ;;
        --settle)   SETTLE="$2";   shift 2 ;;
        --label)    LABEL="$2";    shift 2 ;;
        --json)     JSON_DIR="$2"; shift 2 ;;
        --keep)     KEEP=1;        shift ;;
        -h|--help)  usage; exit 0 ;;
        *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done
[ -n "$LABEL" ] || LABEL="$IMAGE"

log()  { printf '%s [asintag] %s\n' "$(date +%H:%M:%S)" "$*"; }
die()  { printf '%s [asintag] ERROR: %s\n' "$(date +%H:%M:%S)" "$*" >&2; exit 2; }
fail() { printf '%s [asintag] FAIL: %s\n' "$(date +%H:%M:%S)" "$*"; }

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

jsonarg() { [ -n "$JSON_DIR" ] && printf -- '--json\n%s/%s.json' "$JSON_DIR" "$1"; }

log "image ${LABEL}"
log "generating one source file from '${SCENARIO}'"
rm -rf "$SRCDIR" "$LIBDIR" "$CONFIG"
mkdir -p "$SRCDIR" "$LIBDIR" "$CONFIG"
"$PY" "${ROOT}/tools/generate_library.py" --scenario "$SCENARIO" --out "$SRCDIR" \
    --seed "$SEED" --only-asin "$ASIN" --force >/dev/null || die "generation failed"

SRC="$(find "$SRCDIR" -type f -name '*.m4b' | head -1)"
[ -n "$SRC" ] || die "the generator produced no .m4b for ${ASIN}"
log "source file: ${SRC#"$ROOT"/}"

"$PY" "${ROOT}/tools/ffprobe_provisioner.py" --config-dir "$CONFIG" >/dev/null \
    || die "could not provision ffprobe"
FFPROBE="${CONFIG}/ffmpeg/ffprobe"

# --- Gate 1: the reader can see an ASIN that IS there ----------------------------------
CONTROL="${ROOT}/build/asintag-control.m4b"
cp "$SRC" "$CONTROL"
"$PY" "${ROOT}/tools/asin_tag_probe.py" stamp "$CONTROL" --asin "$ASIN" >/dev/null \
    || die "could not stamp the control file"
"$PY" "${ROOT}/tools/asin_tag_probe.py" read "$CONTROL" --label control \
    --expect-asin "$ASIN" --ffprobe "$FFPROBE" $(jsonarg control)
if [ $? -ne 0 ]; then
    fail "THE CONTROL READ AS UNTAGGED. It carries the exact atom the writer targets, so the"
    fail "        reader is broken; ignore everything below it."
    exit 2
fi

# --- Gate 2: the reader can see that an ASIN is NOT there ------------------------------
"$PY" "${ROOT}/tools/asin_tag_probe.py" read "$SRC" --label subject \
    --ffprobe "$FFPROBE" $(jsonarg subject)
if [ $? -eq 0 ]; then
    fail "THE SOURCE FILE ALREADY CARRIES AN ASIN. An ASIN found after the import would prove"
    fail "        nothing about who wrote it. Generate from a scenario whose tag state is"
    fail "        correct-no-asin, or pick another --asin."
    exit 2
fi

# --- The import ------------------------------------------------------------------------
"$RUNTIME" rm -f "$CONTAINER" >/dev/null 2>&1
"$RUNTIME" run -d --name "$CONTAINER" -p "${PORT}:4545" -e LISTENARR_LOG_LEVEL=Debug \
    -v "${SRCDIR}:/src" -v "${LIBDIR}:/audiobooks" -v "${CONFIG}:/app/config" \
    "$IMAGE" >/dev/null || die "could not start ${IMAGE}"

API="http://localhost:${PORT}/api/v1"
for _ in $(seq 1 120); do curl -fsS "${API}/system/status" >/dev/null 2>&1 && break; sleep 2; done
curl -fsS "${API}/system/status" >/dev/null 2>&1 || {
    "$RUNTIME" logs "$CONTAINER" 2>&1 | tail -15; die "API never came up"; }

KEY=$("$PY" -c "import json; print(json.load(open('${CONFIG}/config.json'))['ApiKey'])") || die "no ApiKey"
AUTH=(-H "X-Api-Key: ${KEY}" -H 'Content-Type: application/json')

curl -fsS -X POST "${API}/rootfolders" "${AUTH[@]}" \
    -d '{"name":"lib","path":"/audiobooks","isDefault":true,"caseSensitivityMode":"Sensitive"}' \
    >/dev/null || die "could not create the root folder"

BOOK_ID=$(curl -s -X POST "${API}/library/add" "${AUTH[@]}" \
    -d "{\"metadata\":{\"asin\":\"${ASIN}\",\"title\":\"${TITLE}\",\"authors\":[\"${AUTHOR}\"]},\"monitored\":true,\"autoSearch\":false}" \
    | "$PY" -c "import json,sys; d=json.load(sys.stdin); print(d.get('id') or (d.get('audiobook') or {}).get('id') or '')")
[ -n "$BOOK_ID" ] || die "could not add ${ASIN}"
log "added ${ASIN} as audiobook ${BOOK_ID}"

# The book has to carry the ASIN in the database or the writer is never called at all — the
# controller only enriches when audiobook.Asin is non-blank. Checking it here keeps "no tag was
# written" from being read as a bug when the real answer is that nothing asked for one.
STORED=$(curl -fsS "${API}/library/${BOOK_ID}" "${AUTH[@]}" \
    | "$PY" -c "import json,sys; d=json.load(sys.stdin); print((d.get('audiobook') or d).get('asin') or '')" 2>/dev/null)
[ -n "$STORED" ] || die "the added book carries no ASIN, so the enrichment step would never run"
log "the library record carries ASIN ${STORED}"

CSRC="/src${SRC#"$SRCDIR"}"
REQ=$("$PY" - "$CSRC" "$BOOK_ID" <<'REQEOF'
import json, os, sys
full, aid = sys.argv[1], int(sys.argv[2])
print(json.dumps({"path": os.path.dirname(full), "action": "hardlink/copy", "items": [
    {"relativePath": os.path.basename(full), "fullPath": full, "matchedAudiobookId": aid}]}))
REQEOF
)
log "importing"
curl -s -X POST "${API}/library/manual-import" "${AUTH[@]}" -d "$REQ" >/dev/null

WAITED=0
while [ "$WAITED" -lt "$SETTLE" ]; do
    [ -n "$(find "$LIBDIR" -type f -name '*.m4b' -print -quit 2>/dev/null)" ] && break
    sleep 3; WAITED=$((WAITED + 3))
done
sleep 3   # let the enrichment step run after the file appears
DEST="$(find "$LIBDIR" -type f -name '*.m4b' | head -1)"
[ -n "$DEST" ] || {
    "$RUNTIME" logs "$CONTAINER" 2>&1 | tail -20
    die "no destination file after ${WAITED}s — the import never completed, so there is nothing to judge"
}
log "destination appeared after ${WAITED}s: ${DEST#"$ROOT"/}"

# --- Did the enrichment step run at all? -----------------------------------------------
LOGS="$("$RUNTIME" logs "$CONTAINER" 2>&1)"
WROTE=$(printf '%s' "$LOGS" | grep -c "Wrote ASIN tag")
FAILED=$(printf '%s' "$LOGS" | grep -c "Failed to write ASIN tag")
log "server log: ${WROTE} 'Wrote ASIN tag', ${FAILED} 'Failed to write ASIN tag'"
if [ "$WROTE" -eq 0 ] && [ "$FAILED" -eq 0 ]; then
    fail "the tag writer logged neither success nor failure, so the enrichment step never ran."
    fail "        Nothing here is a statement about the writer."
    exit 2
fi
if [ "$FAILED" -gt 0 ]; then
    printf '%s' "$LOGS" | grep -A12 "Failed to write ASIN tag" | head -20
fi

# --- The verdict -----------------------------------------------------------------------
"$PY" "${ROOT}/tools/asin_tag_probe.py" read "$DEST" --label imported \
    --expect-asin "$ASIN" --ffprobe "$FFPROBE" $(jsonarg imported)
VERDICT=$?

echo
if [ "$VERDICT" -eq 0 ]; then
    log "PASSED: the imported file carries ${ASIN} in its own tags."
    log "        The control read tagged and the source read untagged in the same run, so the"
    log "        reader was shown capable of both answers."
else
    fail "the imported file carries no ASIN of its own."
    fail "        The reader called the stamped control 'tagged' moments earlier, on a copy of"
    fail "        this same file, so the tag is genuinely absent rather than unreadable."
    fail "        The import itself reported success and the file is intact; only the"
    fail "        enrichment step silently did nothing."
fi
exit "$VERDICT"
