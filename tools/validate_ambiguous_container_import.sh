#!/usr/bin/env bash
#
# validate_ambiguous_container_import.sh — prove what manual import does with a bare .mp4.
#
# Listenarr#890 reports that an audiobook delivered as .mp4 cannot be imported. #995 asks how to
# fix it. The extension alone cannot answer, because .mp4 carries audiobooks and films with equal
# right, so the answer taken here is a content probe on the one path that can afford one: manual
# import, which runs once per file the user selected rather than once per file in a scan walk.
#
# What this script measures is the whole matrix, because "the .mp4 imported" on its own is also
# what you would see if the extension check had simply been deleted. Six fixtures go through the
# real manual-import API against a real instance, and each one has a sibling that differs in one
# thing and has to come out the other way:
#
#   audio-only.mp4    admitted   one AAC stream, nothing else
#   with-cover.mp4    admitted   same, plus a still image carried as disposition.attached_pic=1.
#                                ffprobe reports cover art as codec_type=video, so if artwork
#                                counted as video this would refuse and so would most real
#                                audiobooks. This is the fixture the design turns on.
#   with-video.mp4    refused    same, plus a real h264 stream (attached_pic=0). The only
#                                difference from with-cover.mp4 is that one field.
#   video-only.mp4    refused    h264 and no audio stream at all
#   same-bytes.m4b    admitted   byte-for-byte identical to audio-only.mp4, renamed. #890's
#                                claim in one file: the bytes were never the problem. This is
#                                also the regression control, because an accepted extension
#                                must keep importing whatever any probe says.
#   audio-only.mkv    refused    audio-only content in an extension outside both tiers, which
#                                pins that the change widened admission to .mp4 and to nothing
#                                else.
#
# Each case is judged on three observables, not one: what the API replied, what is on disk at the
# destination and at the source, and whether the catalog actually holds a file row. An import that
# copies bytes and registers nothing looks successful from the filesystem alone.
#
# A seventh check runs a library scan after the admitted .mp4 is registered, because the scan walk
# is still extension-only by design and therefore does not see the file it just imported. The
# question that matters is whether reconciliation then treats it as missing and un-registers it.
#
# Production safety, which is not negotiable on this machine: never --network=host, never the
# production port, the port is proven free before it is bound and matched back to the container
# before any write, own network, own config directory, own name, and teardown is confirmed rather
# than asserted. See docs/safe-test-containers.md for why each of those is a rule.
#
#   ./tools/validate_ambiguous_container_import.sh localhost/listenarr-vet:d692d6d
#
set -uo pipefail
unset TMOUT

PRODUCTION_PORT=4545

IMAGE="${1:?usage: validate_ambiguous_container_import.sh <image> [--keep]}"
shift
KEEP=0
BASELINE=0
while [ $# -gt 0 ]; do
    case "$1" in
        --keep) KEEP=1; shift ;;
        # Run the same fixtures against a stock image and REPORT rather than assert. The stock
        # matrix is the before picture: it is what the change is measured against, so encoding
        # expectations for it would mean encoding the bug as the specification.
        --baseline) BASELINE=1; shift ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${ROOT}/.venv/bin/python"
RUNTIME=podman
NAME="mp4val-$$"

log()  { printf '%s [mp4] %s\n' "$(date +%H:%M:%S)" "$*" >&2; }
fail() { printf '%s [mp4] FAIL: %s\n' "$(date +%H:%M:%S)" "$*" >&2; }
die()  { fail "$*"; exit 2; }

command -v "$RUNTIME" >/dev/null 2>&1 || die "podman required"
[ -x "$PY" ] || die "no venv: python3 -m venv .venv && .venv/bin/pip install -e ."

# A port is picked only after proving it free. Not assumed free because we have not used it.
pick_port() {
    local p
    for p in $(seq 18800 18899); do
        [ "$p" = "$PRODUCTION_PORT" ] && continue
        ss -ltn 2>/dev/null | grep -q ":$p " && continue
        echo "$p"; return 0
    done
    die "no free port in 18800-18899"
}
PORT="$(pick_port)"
[ "$PORT" = "$PRODUCTION_PORT" ] && die "refusing to bind the production port"
log "port ${PORT} proven free; production port ${PRODUCTION_PORT} will not be touched"

WORK="$(mktemp -d)"
CFG="${WORK}/config"
SRC="${WORK}/src"
LIB="${WORK}/lib"
mkdir -p "$CFG" "$SRC" "$LIB"

cleanup() {
    if [ "$KEEP" -eq 1 ]; then
        log "--keep: leaving ${NAME} on port ${PORT} and ${WORK} in place"
        return
    fi
    "$RUNTIME" rm -f "$NAME" >/dev/null 2>&1 || true
    "$RUNTIME" network rm "$NAME" >/dev/null 2>&1 || true
    local prod ours
    prod=$(ss -ltn 2>/dev/null | grep -c ":${PRODUCTION_PORT} " || true)
    ours=$(ss -ltn 2>/dev/null | grep -c ":${PORT} " || true)
    log "teardown: production port ${PRODUCTION_PORT} still has ${prod} listener(s), not ours"
    log "teardown: our port ${PORT} now has ${ours} listener(s) (expected 0)"
    [ "$ours" -eq 0 ] || fail "port ${PORT} still has a listener after teardown"
    rm -rf "$WORK"
}
trap cleanup EXIT

# --- fixtures ------------------------------------------------------------------------------
# Built here rather than committed. The repo never carries audio files; a second of silence
# synthesized by a pinned ffmpeg is reproducible and costs nothing to clone.
FFDIR="$("$PY" -c "
import sys, pathlib
sys.path.insert(0, '${ROOT}/tools')
import ffmpeg_harness
print(pathlib.Path(ffmpeg_harness.provision('ffmpeg')).parent)
")" || die "could not provision ffmpeg"
FF="${FFDIR}/ffmpeg"
FP="${FFDIR}/ffprobe"
log "fixtures built with pinned ffmpeg at ${FF}"

build_fixtures() {
    set -e
    "$FF" -hide_banner -loglevel error -y -f lavfi -i anullsrc=r=44100:cl=stereo \
        -t 1 -c:a aac -b:a 64k "${SRC}/audio-only.mp4"
    "$FF" -hide_banner -loglevel error -y -f lavfi -i anullsrc=r=44100:cl=stereo \
        -f lavfi -i color=c=black:s=64x64:r=10 -t 1 \
        -c:a aac -b:a 64k -c:v libx264 -pix_fmt yuv420p "${SRC}/with-video.mp4"
    "$FF" -hide_banner -loglevel error -y -f lavfi -i color=c=blue:s=64x64 \
        -frames:v 1 "${WORK}/cover.png"
    "$FF" -hide_banner -loglevel error -y -i "${SRC}/audio-only.mp4" -i "${WORK}/cover.png" \
        -map 0:a -map 1:v -c:a copy -c:v mjpeg -disposition:v:0 attached_pic \
        "${SRC}/with-cover.mp4"
    "$FF" -hide_banner -loglevel error -y -f lavfi -i color=c=black:s=64x64:r=10 \
        -t 1 -c:v libx264 -pix_fmt yuv420p "${SRC}/video-only.mp4"
    # Identical bytes, accepted extension. cp rather than a re-encode, on purpose.
    cp "${SRC}/audio-only.mp4" "${SRC}/same-bytes.m4b"
    "$FF" -hide_banner -loglevel error -y -i "${SRC}/audio-only.mp4" -c:a copy \
        "${SRC}/audio-only.mkv"
    # A zero-byte .mp4: the fixture #890's characterization test uses. There is nothing for a
    # probe to read, so this measures what happens when the probe comes back with nothing,
    # which must be a refusal and not an admission by default.
    : > "${SRC}/empty.mp4"
    set +e
}
build_fixtures || die "fixture build failed"

# State what the probe actually sees, so the matrix below is read against measured stream
# contents rather than against what the filenames imply.
log "what ffprobe reports for each fixture (codec_type, attached_pic):"
for f in "${SRC}"/*; do
    case "$f" in *.png) continue ;; esac
    printf '        %-18s %s\n' "$(basename "$f")" \
        "$("$FP" -v error -show_entries stream=codec_type:stream_disposition=attached_pic \
            -of csv=p=0 "$f" | tr '\n' ' ')"
done
if [ "$(sha256sum "${SRC}/audio-only.mp4" | cut -d' ' -f1)" \
     != "$(sha256sum "${SRC}/same-bytes.m4b" | cut -d' ' -f1)" ]; then
    die "same-bytes.m4b is not byte-identical to audio-only.mp4; the control is void"
fi
log "audio-only.mp4 and same-bytes.m4b confirmed byte-identical"

# --- instance ------------------------------------------------------------------------------
# A pinned ffprobe goes in up front. Without it the app downloads one on first boot, and manual
# import hard-fails with "Failed to extract metadata from file" during that window, which would
# look exactly like the gate refusing the file.
"$PY" "${ROOT}/tools/ffprobe_provisioner.py" --config-dir "$CFG" >/dev/null \
    || die "could not provision ffprobe into the config dir"

# The library mount is /audiobooks and must never be /lib. Mounting an empty host directory over
# /lib replaces the container's system library directory, the dynamic loader goes with it, and the
# container dies with "exec /docker-entrypoint.sh: no such file or directory", which reads as a
# broken image rather than as a bad mount point.
"$RUNTIME" network create "$NAME" >/dev/null 2>&1 || true
"$RUNTIME" rm -f "$NAME" >/dev/null 2>&1 || true
# No --network=host, ever. Loopback-bound so nothing on the LAN reaches it either.
"$RUNTIME" run -d --name "$NAME" \
    --network "$NAME" \
    -p "127.0.0.1:${PORT}:4545" \
    -e LISTENARR_LOG_LEVEL=Debug \
    -v "${CFG}:/app/config" \
    -v "${SRC}:/src" \
    -v "${LIB}:/audiobooks" \
    "$IMAGE" >/dev/null || die "could not start ${NAME}"

# A port answering is evidence of nothing until it is matched to the container we started.
# The mapping is not reported the instant `podman run` returns, so this retries rather than
# concluding from one empty read. It still refuses to proceed if the match never appears: an
# unmatched port is exactly the case that must never receive a write.
OWNED=0
for _ in $(seq 1 20); do
    if "$RUNTIME" port "$NAME" 2>/dev/null | grep -q ":${PORT}\$"; then OWNED=1; break; fi
    sleep 1
done
if [ "$OWNED" -ne 1 ]; then
    "$RUNTIME" ps -a --filter "name=${NAME}" --format '{{.Names}} {{.Status}} {{.Ports}}' >&2
    "$RUNTIME" logs "$NAME" 2>&1 | tail -20 >&2
    die "${NAME} is not serving ${PORT}; refusing to write anything"
fi
log "ownership confirmed: ${NAME} serves 127.0.0.1:${PORT} ($("$RUNTIME" port "$NAME" | tr '\n' ' '))"

API="http://127.0.0.1:${PORT}/api/v1"
UP=0
for _ in $(seq 1 90); do
    curl -fsS "${API}/system/status" >/dev/null 2>&1 && { UP=1; break; }
    sleep 2
done
[ "$UP" -eq 1 ] || { "$RUNTIME" logs "$NAME" 2>&1 | tail -20; die "${NAME} never became healthy"; }

# Provenance. A build-metadata suffix means a patched build. That is intended here: this measures
# our change. It would NOT be a control for a claim about released software.
STAMP=$("$RUNTIME" exec "$NAME" grep -o 'Listenarr\.Api/[0-9][^"]*' \
    /app/Listenarr.Api.deps.json 2>/dev/null | head -1)
log "image ${IMAGE} reports ${STAMP}"

KEY=$("$PY" -c "import json;print(json.load(open('${CFG}/config.json'))['ApiKey'])" 2>/dev/null)
[ -n "$KEY" ] || die "no api key in ${CFG}/config.json"
AUTH=(-H "X-Api-Key: ${KEY}" -H 'Content-Type: application/json')

curl -sS -X POST "${API}/rootfolders" "${AUTH[@]}" \
    -d '{"name":"lib","path":"/audiobooks","isDefault":true,"caseSensitivityMode":"Sensitive"}' \
    >/dev/null || die "could not create the root folder"
log "root folder /audiobooks created"

# One audiobook per case, so destinations cannot collide and one case cannot register into
# another's folder. Every entry is from corpus/corpus.json, whose ASINs were each verified
# against live metadata by tools/build_corpus.py. Title and authors are sent alongside the ASIN
# because /library/add will not resolve from an ASIN alone on an instance with no metadata
# provider configured, and a case that cannot get an audiobook has no verdict to report.
BOOKS=(
  'B008DFUGCQ|A Princess of Mars|Edgar Rice Burroughs'
  'B01FKWL15A|Twenty Thousand Leagues Under the Sea|Jules Verne'
  'B076HSP1FT|20,000 Leagues Under the Sea|Jules Verne'
  'B007BR5KZA|The Wonderful Wizard of Oz|L. Frank Baum'
  'B002UZJF4U|The Three Musketeers|Alexandre Dumas'
  'B00BHPI2TS|The Marvelous Land of Oz|L. Frank Baum'
  'B004YWTD30|She: A History of Adventure|H. Rider Haggard'
)
ADD_REPLY=""
add_book() {
    local spec="$1"
    local body; body="$("$PY" - "$spec" <<'ADDEOF'
import json, sys
asin, title, author = sys.argv[1].split("|", 2)
print(json.dumps({"metadata": {"asin": asin, "title": title, "authors": [author]},
                  "monitored": True, "autoSearch": False}))
ADDEOF
)"
    ADD_REPLY="$(curl -sS -X POST "${API}/library/add" "${AUTH[@]}" -d "$body")"
    "$PY" -c "
import json,sys
try: d=json.loads(sys.stdin.read())
except Exception: print(''); raise SystemExit
print(d.get('id') or (d.get('audiobook') or {}).get('id') or '')" <<<"$ADD_REPLY"
}

RESULT=0
MATRIX=()
LAST_ID=""

# One case. Args: <fixture> <expect: admitted|refused> <asin>
run_case() {
    local fixture="$1" expect="$2" spec="$3"
    local asin="${spec%%|*}"
    local hsrc="${SRC}/${fixture}" csrc="/src/${fixture}"
    LAST_ID=""
    [ -f "$hsrc" ] || { fail "${fixture}: fixture missing"; RESULT=1; return 1; }

    local id; id="$(add_book "$spec")"
    if [ -z "$id" ]; then
        fail "${fixture}: could not add an audiobook for ${asin}: ${ADD_REPLY}"
        MATRIX+=("$(printf '%-18s %-10s %-10s could not add an audiobook for %s' \
            "$fixture" "$expect" "harness-error" "$asin")")
        RESULT=1
        return 1
    fi
    LAST_ID="$id"

    local req; req="$("$PY" - "$csrc" "$id" <<'REQEOF'
import json, os, sys
full = sys.argv[1]; aid = int(sys.argv[2])
print(json.dumps({"path": os.path.dirname(full), "action": "move", "items": [
    {"relativePath": os.path.basename(full), "fullPath": full, "matchedAudiobookId": aid}]}))
REQEOF
)"
    local reply; reply="$(curl -sS -X POST "${API}/library/manual-import" "${AUTH[@]}" -d "$req")"

    # Observable 1: what the API said.
    local api_ok api_err
    api_ok="$("$PY" -c "
import json,sys
d=json.loads(sys.stdin.read())
r=(d.get('results') or [{}])[0]
print('yes' if r.get('success') else 'no')" <<<"$reply" 2>/dev/null)"
    api_err="$("$PY" -c "
import json,sys
d=json.loads(sys.stdin.read())
r=(d.get('results') or [{}])[0]
print((r.get('error') or '').strip())" <<<"$reply" 2>/dev/null)"

    # Observable 2: the filesystem, on the host, outside the application's own reporting.
    local waited=0 dest=""
    while [ "$waited" -lt 40 ]; do
        dest="$(find "$LIB" -type f \! -name '*.json' \! -name '*.jpg' -print -quit 2>/dev/null)"
        [ -n "$dest" ] && break
        [ "$api_ok" = "no" ] && break
        sleep 2; waited=$((waited + 2))
    done
    sleep 1
    local dest_ext="none"
    [ -n "$dest" ] && dest_ext="$(basename "$dest")"
    local source_state="preserved"
    [ -e "$hsrc" ] || source_state="consumed"

    # Observable 3: the catalog. Bytes at a destination with no file row is not an import.
    local rows
    rows="$(curl -sS "${API}/library/${id}/files-debug" "${AUTH[@]}" | "$PY" -c "
import json,sys
try:
    d=json.load(sys.stdin)
except Exception:
    print('?'); raise SystemExit
for k in ('files','audiobookFiles','items'):
    if isinstance(d, dict) and isinstance(d.get(k), list):
        print(len(d[k])); raise SystemExit
print(len(d) if isinstance(d, list) else '?')" 2>/dev/null)"

    # Three outcomes, not two. A refusal that still left a file at the destination is not the
    # same event as a clean refusal: no row is created and no data is lost either way, but the
    # library folder has acquired a file nobody asked for and nothing tracks.
    local verdict
    if [ "$api_ok" = "yes" ] && [ -n "$dest" ] && [ "${rows:-0}" != "0" ] && [ "${rows:-?}" != "?" ]; then
        verdict=admitted
    elif [ "$api_ok" = "no" ] && [ -z "$dest" ] && [ "$source_state" = "preserved" ]; then
        verdict=refused
    elif [ "$api_ok" = "no" ] && [ -n "$dest" ] && [ "$source_state" = "preserved" ] \
         && [ "${rows:-0}" = "0" ]; then
        verdict=refusedStray
    else
        verdict="inconsistent(api=${api_ok},dest=${dest_ext},src=${source_state},rows=${rows:-?})"
    fi

    MATRIX+=("$(printf '%-18s %-10s %-10s api=%-4s dest=%-34s source=%-9s rows=%-3s %s' \
        "$fixture" "$expect" "$verdict" "$api_ok" "$dest_ext" "$source_state" "${rows:-?}" \
        "${api_err}")")

    if [ "$BASELINE" -eq 1 ]; then
        log "case ${fixture}: observed ${verdict} rows=${rows:-?} dest=${dest_ext} source=${source_state}"
    elif [ "$verdict" = "$expect" ]; then
        log "case ${fixture}: OK (${verdict}) rows=${rows:-?} dest=${dest_ext} source=${source_state}"
    else
        fail "case ${fixture}: expected ${expect}, observed ${verdict}. api error: ${api_err}"
        "$RUNTIME" logs "$NAME" 2>&1 | grep -iE 'ambiguous|probed container|non-audio|manual import' | tail -6
        RESULT=1
    fi

    # Clear the library folder between cases so a later case cannot read an earlier
    # destination as its own.
    find "$LIB" -mindepth 1 -delete 2>/dev/null || true
}

log "running the control matrix"
run_case audio-only.mp4 admitted "${BOOKS[0]}"
ADMITTED_ID="$LAST_ID"
run_case with-cover.mp4  admitted "${BOOKS[1]}"
run_case with-video.mp4  refused  "${BOOKS[2]}"
# A .mp4 carrying video and no audio at all. Refused, and the reason names the video, because
# that is the more specific of the two facts. The audio-free message has its own unit test.
run_case video-only.mp4  refused  "${BOOKS[3]}"
run_case same-bytes.m4b  admitted "${BOOKS[4]}"
# Nothing for the probe to read. Must refuse rather than fall through to an extension decision.
run_case empty.mp4       refused  "${BOOKS[5]}"
# refusedStray, not refused, and this is PRE-EXISTING rather than something the .mp4 gate did.
# A genuinely non-audio file is refused at registration, which is after the destination has been
# prepared, so a file is left in the library folder with no row pointing at it. Run with
# --baseline against a stock image and the same thing happens there. It is recorded here as the
# expectation so that a future fix has to change this line deliberately.
run_case audio-only.mkv  refusedStray "${BOOKS[6]}"

# --- the scan question ----------------------------------------------------------------------
# The scan walk is extension-only by design, so it does not see a registered .mp4. Reconciliation
# decides from a filesystem existence check rather than from discovery membership, which is a
# reading of the source and not a measurement until it is run. So run it: re-import the admitted
# fixture, scan, and count the file rows before and after.
log "checking that a library scan does not un-register an imported .mp4"
if [ "$BASELINE" -eq 1 ]; then
    # Nothing was admitted on a stock image, which is the defect, so there is no registered
    # .mp4 for a scan to threaten and nothing to measure here.
    log "baseline mode: no .mp4 was admitted, so the scan question does not apply"
elif [ -n "$ADMITTED_ID" ]; then
    build_fixtures >/dev/null 2>&1
    req="$("$PY" - /src/audio-only.mp4 "$ADMITTED_ID" <<'REQEOF'
import json, os, sys
full = sys.argv[1]; aid = int(sys.argv[2])
print(json.dumps({"path": os.path.dirname(full), "action": "move", "items": [
    {"relativePath": os.path.basename(full), "fullPath": full, "matchedAudiobookId": aid}]}))
REQEOF
)"
    curl -sS -X POST "${API}/library/manual-import" "${AUTH[@]}" -d "$req" >/dev/null
    sleep 3
    count_rows() {
        curl -sS "${API}/library/${ADMITTED_ID}/files-debug" "${AUTH[@]}" | "$PY" -c "
import json,sys
try: d=json.load(sys.stdin)
except Exception: print('?'); raise SystemExit
for k in ('files','audiobookFiles','items'):
    if isinstance(d, dict) and isinstance(d.get(k), list):
        print(len(d[k])); raise SystemExit
print(len(d) if isinstance(d, list) else '?')"
    }
    BEFORE="$(count_rows)"
    curl -sS -X POST "${API}/library/${ADMITTED_ID}/scan" "${AUTH[@]}" -d '{}' >/dev/null
    sleep 15
    AFTER="$(count_rows)"
    log "file rows before scan=${BEFORE}, after scan=${AFTER}"
    if [ "$BEFORE" = "$AFTER" ] && [ "${BEFORE:-0}" != "0" ] && [ "${BEFORE:-?}" != "?" ]; then
        log "scan did not un-register the imported .mp4"
        MATRIX+=("$(printf '%-18s %-10s %-10s rows before=%s after=%s' \
            "scan after import" "preserved" "preserved" "$BEFORE" "$AFTER")")
    else
        fail "a library scan changed the file rows for the imported .mp4 (${BEFORE} -> ${AFTER})"
        MATRIX+=("$(printf '%-18s %-10s %-10s rows before=%s after=%s' \
            "scan after import" "preserved" "CHANGED" "$BEFORE" "$AFTER")")
        RESULT=1
    fi
else
    fail "no admitted audiobook id; skipping the scan check"
    RESULT=1
fi

echo
echo "control matrix"
echo "fixture            expected   observed   details"
for row in "${MATRIX[@]}"; do echo "  $row"; done
echo

if [ "$BASELINE" -eq 1 ]; then
    log "BASELINE RECORDED against ${IMAGE}. No expectations asserted; the observed column is"
    log "                  the before picture the change is measured against."
elif [ "$RESULT" -eq 0 ]; then
    log "VALIDATION PASSED: an audio-only .mp4 and a cover-art .mp4 imported and registered;"
    log "                   a video-bearing .mp4, an audio-free .mp4 and an .mkv were refused"
    log "                   with the source left in place; identical bytes named .m4b still"
    log "                   imported; a scan did not un-register the result."
else
    fail "VALIDATION FAILED - see the cases above."
fi
exit "$RESULT"
