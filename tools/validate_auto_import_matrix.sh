#!/usr/bin/env bash
#
# validate_auto_import_matrix.sh — does the AUTOMATIC post-download import refuse what the
# MANUAL import accepts, and if so, what changes it?
#
# Listenarr#890 (deLbe321) and a later comment on it (thegogz) describe the same shape: a grab
# reaches Completed in Activity, flips to Import Blocked, the library item stays Missing, and
# the log says nothing useful. Both reporters got the same file in through Wanted > Manual
# Import. So the interesting asymmetry is not the format, it is that two code paths disagree
# about the same bytes on disk. One reporter blamed "mp4 format" and converting to mp3 helped
# them; the other hit it with an mp3, so that story does not cover both.
#
# This drives the automatic path end to end on a throwaway instance: a Torznab stub answers the
# search, a qBittorrent stub accepts the grab and then reports the torrent complete with files
# that are really on disk, and the download monitor imports it — or does not. Each row changes
# exactly one thing.
#
# THE CONTROLS COME FIRST AND THEY DECIDE WHETHER ANYTHING ELSE MEANS ANYTHING.
#
#   autoControl    single .mp3 on local storage, automatic path. MUST IMPORT. If it does not,
#                  this script is measuring its own plumbing and every row below it is noise;
#                  it says BROKEN CHECK and stops rather than printing a verdict.
#   manualControl  the same file, the same instance settings, through manual import. Included
#                  because if automatic and manual both succeed on everything, the asymmetry
#                  the issue describes has not been reproduced, and that is the finding.
#
# Then one axis at a time: container format (.mp3, .m4b, .m4a, .flac and a literal .mp4), file
# count, subdirectory depth, whether the client still holds the files open, and the filesystem
# the download directory lives on.
#
# WHY THE FILESYSTEM IS AN AXIS. The pre-flight capability check captures a durable physical
# identity for the source file before publishing it, and its outer catch swallows IOException
# and returns one fixed string with the exception discarded. A filesystem that cannot answer
# the identity probes is therefore a candidate cause that does not depend on audio format at
# all, and both reporters are on network mounts. `--weak-fs` runs the download directory
# through fuse-overlayfs, which is the weakest filesystem reachable here without root.
#
# SAFETY. A real Listenarr runs on this machine on port 4545. Every container here gets its own
# podman network, its own name and its own config directory, binds 127.0.0.1 only on a port
# proven free first, and is matched back to its own `podman port` output before any write is
# issued. Teardown is verified and printed rather than asserted. There are also stale
# qBittorrent stubs from earlier sessions listening on this machine, so the stub port is proven
# free AND the thing that answers is confirmed to be the stub this script started, by reading
# back a file list only a reflecting stub can produce.
#
#   ./tools/validate_auto_import_matrix.sh
#   ./tools/validate_auto_import_matrix.sh --only autoControl --log-level Trace
#   ./tools/validate_auto_import_matrix.sh --only mp4Only --keep
#
set -uo pipefail
unset TMOUT

PRODUCTION_PORT=4545
IMAGE="ghcr.io/listenarrs/listenarr:canary"
LOG_LEVEL=Debug
SETTLE=180          # seconds to wait for a download to reach a terminal state
ONLY=""
KEEP=0
WEAK_FS=0

# Ports known to be held by stale stubs from earlier sessions, or unattributable. Skipped
# outright: a stale stub answers, authenticates and serves a queue that has nothing to do with
# this run, which looks exactly like a working harness.
STALE_PORTS="18098 18099 18101 18872 19118"

while [ $# -gt 0 ]; do
    case "$1" in
        --image)     IMAGE="$2";     shift 2 ;;
        --log-level) LOG_LEVEL="$2"; shift 2 ;;
        --settle)    SETTLE="$2";    shift 2 ;;
        --only)      ONLY="$2";      shift 2 ;;
        --keep)      KEEP=1;         shift ;;
        --weak-fs)   WEAK_FS=1;      shift ;;
        -h|--help)   sed -n '2,50p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${ROOT}/.venv/bin/python"
RUNTIME=podman
RELEASE_TITLE="The Valley of Fear - Arthur Conan Doyle"
BOOK_ASIN="B002UUFXKU"
BOOK_TITLE="The Valley of Fear"
BOOK_AUTHOR="Arthur Conan Doyle"

log()  { printf '%s [matrix] %s\n' "$(date +%H:%M:%S)" "$*"; }
fail() { printf '%s [matrix] FAIL: %s\n' "$(date +%H:%M:%S)" "$*"; }

command -v "$RUNTIME" >/dev/null 2>&1 || { echo "podman required"; exit 2; }
[ -x "$PY" ] || { echo "no venv — python3 -m venv .venv && .venv/bin/pip install -e ."; exit 2; }

# ---------------------------------------------------------------------------------------------
# Ports. Proven free, never the production port, never a port a stale stub is sitting on.
# ---------------------------------------------------------------------------------------------
port_is_free() {
    local candidate="$1"
    [ "$candidate" = "$PRODUCTION_PORT" ] && return 1
    for stale in $STALE_PORTS; do [ "$candidate" = "$stale" ] && return 1; done
    ss -ltn 2>/dev/null | grep -q ":${candidate} " && return 1
    return 0
}

pick_port() {
    local first="$1" last="$2" candidate
    for candidate in $(seq "$first" "$last"); do
        port_is_free "$candidate" && { echo "$candidate"; return 0; }
    done
    fail "no free port in ${first}-${last}"
    return 1
}

# ---------------------------------------------------------------------------------------------
# The Torznab stub is stateless and answers every query with the same release, so one serves
# the whole run. The qBittorrent stub is per case, because it reflects that case's files.
# ---------------------------------------------------------------------------------------------
TORZNAB_PORT=""
TORZNAB_PID=""
TORZNAB_LOG=""

start_torznab() {
    TORZNAB_PORT="$(pick_port 19600 19699)" || return 1
    TORZNAB_LOG="$(mktemp -t torznab-XXXXXX.log)"
    "$PY" "${ROOT}/tools/torznab_stub.py" --port "$TORZNAB_PORT" \
        --box-set "$RELEASE_TITLE" --audio-evidence "John Bolen" \
        >"$TORZNAB_LOG" 2>&1 &
    TORZNAB_PID=$!
    local _
    for _ in $(seq 1 20); do
        curl -fsS "http://127.0.0.1:${TORZNAB_PORT}/api?t=caps" >/dev/null 2>&1 && break
        sleep 1
    done
    # Confirm what answers is OUR stub rather than something already on the port: a caps
    # document naming this stub, and a search that returns the release title we chose.
    curl -fsS "http://127.0.0.1:${TORZNAB_PORT}/api?t=caps" 2>/dev/null \
        | grep -q 'torznab stub' || { fail "whatever answers :${TORZNAB_PORT} is not our stub"; return 1; }
    curl -fsS "http://127.0.0.1:${TORZNAB_PORT}/api?t=search&q=x" 2>/dev/null \
        | grep -qF "$RELEASE_TITLE" || { fail "stub on :${TORZNAB_PORT} does not serve our release"; return 1; }
    log "torznab stub on :${TORZNAB_PORT}, serving ${RELEASE_TITLE@Q} (identity confirmed)"
    return 0
}

stop_torznab() {
    [ -n "$TORZNAB_PID" ] && kill "$TORZNAB_PID" 2>/dev/null
    TORZNAB_PID=""
}

# ---------------------------------------------------------------------------------------------
# Weak storage. fuse-overlayfs over the real download directory, so the container reads the
# files through a filesystem that answers the durable-identity probes differently. Unprivileged
# and reversible; if it is not available the row is reported as not run rather than skipped
# silently, because "we could not test it" and "it passed" must not look the same.
# ---------------------------------------------------------------------------------------------
mount_weak_fs() {
    local lower="$1" mountpoint="$2" work="$3"
    command -v fuse-overlayfs >/dev/null 2>&1 || return 1
    mkdir -p "$mountpoint" "$work/upper" "$work/work"
    fuse-overlayfs -o "lowerdir=${lower},upperdir=${work}/upper,workdir=${work}/work" \
        "$mountpoint" 2>&1 || return 1
    mountpoint -q "$mountpoint" 2>/dev/null || [ -n "$(ls -A "$mountpoint" 2>/dev/null)" ] || return 1
    return 0
}

umount_weak_fs() {
    local mountpoint="$1"
    [ -d "$mountpoint" ] || return 0
    fusermount3 -u "$mountpoint" 2>/dev/null || fusermount -u "$mountpoint" 2>/dev/null || true
}

# ---------------------------------------------------------------------------------------------
# One row.
#   run_case <name> <ext> <layout> <count> <auto|manual> <local|weak> <holdopen 0|1>
# Sets CASE_STATUS, CASE_BLOCK, CASE_LIBRARY, CASE_LOG for the caller.
# ---------------------------------------------------------------------------------------------
CASE_STATUS=""; CASE_BLOCK=""; CASE_LIBRARY=""; CASE_LOG=""; CASE_STAMP=""
run_case() {
    local name="$1" ext="$2" layout="$3" count="$4" mode="$5" fs="$6" holdopen="$7"
    CASE_STATUS="notrun"; CASE_BLOCK=""; CASE_LIBRARY="unknown"; CASE_STAMP=""
    SETTLE_THIS="$SETTLE"

    local container="m890-${name}" network="m890-${name}"
    local base port stub_port stub_pid stub_log
    base="$(mktemp -d -t m890-XXXXXX)"
    CASE_LOG="${ROOT}/m890-${name}.container.log"

    port="$(pick_port 14700 14799)" || return 1
    stub_port="$(pick_port 19700 19799)" || return 1

    # Evidence, printed rather than assumed: the ports were free immediately before binding.
    log "  ports: container ${port}, stub ${stub_port}"
    log "  free before bind: '$(ss -ltn 2>/dev/null | grep -c ":${port} ")' and '$(ss -ltn 2>/dev/null | grep -c ":${stub_port} ")' listener(s)"

    mkdir -p "${base}/config" "${base}/lib" "${base}/dl"

    local built entry
    built="$("$PY" "${ROOT}/tools/make_download_fixture.py" --out "${base}/dl" \
        --ext "$ext" --layout "$layout" --count "$count" \
        --title "$BOOK_TITLE" --author "$BOOK_AUTHOR" --asin "$BOOK_ASIN")" \
        || { fail "${name}: could not build the fixture"; return 1; }
    entry="$("$PY" -c "import json,sys;print(json.load(sys.stdin)['entry'])" <<<"$built")"
    log "  on disk: ${built}"

    # The directory the container will read from. For the weak-storage row that is a
    # fuse-overlayfs view of the same bytes, and NOTHING else about the row changes.
    local host_dl="${base}/dl"
    if [ "$fs" = "weak" ]; then
        if ! mount_weak_fs "${base}/dl" "${base}/weak" "${base}/weakwork"; then
            fail "${name}: could not mount fuse-overlayfs; row NOT RUN (not the same as passing)"
            rm -rf "$base"
            CASE_STATUS="notrun-nofs"
            return 1
        fi
        host_dl="${base}/weak"
        log "  weak storage: fuse-overlayfs at ${host_dl}"
        log "  $(stat -f -c 'fstype=%T' "$host_dl") vs local $(stat -f -c 'fstype=%T' "${base}/dl")"
    fi

    # Teardown is registered before anything is started, so an early return cannot leak a
    # container, a network, a stub or a FUSE mount. This is how the stale stubs on this
    # machine came to exist.
    local cleanup="
        kill \$(cat '${base}/stub.pid' 2>/dev/null) 2>/dev/null || true
        ${RUNTIME} rm -f '${container}' >/dev/null 2>&1 || true
        ${RUNTIME} network rm '${network}' >/dev/null 2>&1 || true
        fusermount3 -u '${base}/weak' 2>/dev/null || fusermount -u '${base}/weak' 2>/dev/null || true
    "
    # shellcheck disable=SC2064
    trap "$cleanup" RETURN

    stub_log="${ROOT}/m890-${name}.stub.log"
    local stub_args=(--port "$stub_port" --reflect-root "$host_dl"
                     --reflect-save-path /downloads --reflect-entry "$entry"
                     --reflect-name "$RELEASE_TITLE" --verbose)
    [ "$holdopen" = "1" ] && stub_args+=(--hold-open)
    "$PY" "${ROOT}/tools/qbittorrent_stub.py" "${stub_args[@]}" >"$stub_log" 2>&1 &
    stub_pid=$!
    echo "$stub_pid" > "${base}/stub.pid"

    local ready=0 _
    for _ in $(seq 1 20); do
        curl -fsS "http://127.0.0.1:${stub_port}/api/v2/app/version" >/dev/null 2>&1 && { ready=1; break; }
        sleep 1
    done
    [ "$ready" -eq 1 ] || { fail "${name}: stub never came up on :${stub_port}"; return 1; }

    # IDENTITY CONTROL for the stub. A stale stub from another session answers version and
    # login identically, and serves a queue that is not this case. Only a stub reflecting THIS
    # fixture can return this file list, so read it back and insist on it. A non-reflecting
    # stub answers `[]` here, including the stale one running with --count 0.
    local reported
    reported="$(curl -fsS "http://127.0.0.1:${stub_port}/api/v2/torrents/files?hash=identitycheck" 2>/dev/null \
        | "$PY" -c "import json,sys;print(','.join(f['name'] for f in json.load(sys.stdin)))" 2>/dev/null)"
    local expected
    expected="$("$PY" -c "import json,sys;print(','.join(json.load(sys.stdin)['files']))" <<<"$built")"
    if [ "$reported" != "$expected" ]; then
        fail "${name}: the stub answering :${stub_port} is not ours"
        fail "        expected files [${expected}] but it reported [${reported}]"
        return 1
    fi
    log "  stub identity confirmed: it reports exactly this case's files"

    "$PY" "${ROOT}/tools/ffprobe_provisioner.py" --config-dir "${base}/config" >/dev/null 2>&1 \
        || log "  WARNING: ffprobe was not provisioned; a first-boot download race may bite"

    "$RUNTIME" network create "$network" >/dev/null 2>&1
    # No --network=host. Loopback-bound. Own network, own config dir, own name.
    "$RUNTIME" run -d --name "$container" --network "$network" \
        -p "127.0.0.1:${port}:4545" \
        -e "LISTENARR_LOG_LEVEL=${LOG_LEVEL}" \
        -v "${base}/config:/app/config" \
        -v "${host_dl}:/downloads" \
        -v "${base}/lib:/audiobooks" \
        "$IMAGE" >/dev/null || { fail "${name}: container did not start"; return 1; }

    # A port answering is evidence of nothing until it is matched to the container we started.
    "$RUNTIME" port "$container" | grep -q ":${port}\$" \
        || { fail "${name}: ${container} is not serving ${port}; refusing to write to it"; return 1; }
    log "  podman port ${container}: $("$RUNTIME" port "$container" | tr '\n' ' ')"

    local api="http://127.0.0.1:${port}/api/v1"
    local up=0
    for _ in $(seq 1 90); do curl -fsS "${api}/system/status" >/dev/null 2>&1 && { up=1; break; }; sleep 2; done
    [ "$up" -eq 1 ] || { fail "${name}: API never came up"; "$RUNTIME" logs "$container" 2>&1 | tail -20; return 1; }

    # Stock stamp, read from the dependency manifest rather than asked of the API, which
    # truncates the build-metadata suffix and so reports a patched build as stock.
    CASE_STAMP="$("$RUNTIME" exec "$container" grep -o 'Listenarr\.Api/[0-9][^"]*' \
        /app/Listenarr.Api.deps.json 2>/dev/null | head -1)"
    log "  stock stamp: ${CASE_STAMP}"

    local key
    key="$("$PY" -c "import json;print(json.load(open('${base}/config/config.json'))['ApiKey'])" 2>/dev/null)"
    [ -n "$key" ] || { fail "${name}: no api key"; return 1; }
    local auth=(-H "X-Api-Key: ${key}" -H 'Content-Type: application/json')

    curl -s -X POST "${api}/rootfolders" "${auth[@]}" \
        -d '{"name":"lib","path":"/audiobooks","isDefault":true,"caseSensitivityMode":"Sensitive"}' >/dev/null

    # A fresh instance has NO quality profiles at all, and an audiobook with no profile makes
    # search-and-download refuse before it ever reaches an indexer ("Audiobook has no quality
    # profile assigned"). That is a property of a brand-new install, not of the bug, so a
    # permissive profile is created here: every quality allowed, no minimum seeders, no minimum
    # score, so nothing a row does can be mistaken for the scorer rejecting the release. It is
    # identical for every row, which is what makes the rows comparable.
    curl -s -X POST "${api}/qualityprofile" "${auth[@]}" -d '{
        "name":"matrix","description":"permissive, so scoring cannot confound a row",
        "qualities":[{"quality":"MP3 320kbps","allowed":true,"priority":0,"codec":"MP3"},
                     {"quality":"AAC 128kbps","allowed":true,"priority":1,"codec":"AAC"},
                     {"quality":"FLAC","allowed":true,"priority":2,"codec":"FLAC"},
                     {"quality":"Unknown","allowed":true,"priority":3}],
        "cutoffQuality":"MP3 320kbps","minimumSize":0,"maximumSize":0,
        "preferredFormats":["m4b","mp3","m4a","flac","mp4","opus"],
        "preferredWords":[],"mustNotContain":[],"mustContain":[],"preferredLanguages":[],
        "minimumSeeders":0,"minimumScore":0,"isDefault":true,
        "preferNewerReleases":true,"maximumAge":0}' >/dev/null

    curl -s -X POST "${api}/download-clients" "${auth[@]}" -d "{
        \"name\":\"stub-qb\",\"type\":\"qbittorrent\",\"host\":\"host.containers.internal\",
        \"port\":${stub_port},\"username\":\"admin\",\"password\":\"admin\",
        \"useSSL\":false,\"isEnabled\":true}" >/dev/null

    curl -s -X POST "${api}/indexers" "${auth[@]}" -d "{
        \"name\":\"stub-torznab\",\"type\":\"Torrent\",\"implementation\":\"Torznab\",
        \"url\":\"http://host.containers.internal:${TORZNAB_PORT}\",\"apiKey\":\"stub\",
        \"categories\":\"3030\",\"isEnabled\":true,\"enableRss\":false,
        \"enableAutomaticSearch\":true,\"enableInteractiveSearch\":true}" >/dev/null

    local id
    id="$(curl -s -X POST "${api}/library/add" "${auth[@]}" \
        -d "{\"metadata\":{\"asin\":\"${BOOK_ASIN}\",\"title\":\"${BOOK_TITLE}\",\"authors\":[\"${BOOK_AUTHOR}\"]},\"monitored\":true,\"autoSearch\":false}" \
        | "$PY" -c "import json,sys;d=json.load(sys.stdin);print(d.get('id') or (d.get('audiobook') or {}).get('id') or '')")"
    [ -n "$id" ] || { fail "${name}: could not add the book"; return 1; }
    log "  audiobook id ${id}"

    if [ "$mode" = "auto" ]; then
        local grab
        grab="$(curl -s -X POST "${api}/download/search-and-download" "${auth[@]}" \
            -d "{\"audiobookId\":${id}}")"
        log "  search-and-download: ${grab}"
        if ! printf '%s' "$grab" | grep -q '"success":true'; then
            # No grab means no download row, and waiting SETTLE seconds for one would only
            # burn three minutes to report "norow". Say what actually went wrong instead.
            fail "${name}: the automatic grab never happened, so no import was attempted"
            "$RUNTIME" logs "$container" >"$CASE_LOG" 2>&1
            grep -nE "Scored|REJECTED|No acceptable|download client|Searching for audiobook" \
                "$CASE_LOG" | tail -8 | sed 's/^/    | /'
            CASE_STATUS="nograb"
            SETTLE_THIS=0
        fi
    else
        local req
        req="$("$PY" - "$entry" "$id" <<'REQEOF'
import json, os, sys
entry, aid = sys.argv[1], int(sys.argv[2])
full = os.path.join("/downloads", entry)
print(json.dumps({"path": "/downloads", "action": "hardlink/copy", "items": [
    {"relativePath": entry, "fullPath": full, "matchedAudiobookId": aid}]}))
REQEOF
)"
        log "  manual-import request: ${req}"
        log "  manual-import response: $(curl -s -X POST "${api}/library/manual-import" "${auth[@]}" -d "$req")"
    fi

    # Wait for a TERMINAL state rather than sleeping a flat interval. ImportBlocked is only
    # reached after the processing job exhausts its retries, so a short wait would report
    # "still importing" for a row that ends up blocked.
    local waited=0 status="" blocked=""
    local settle="${SETTLE_THIS:-$SETTLE}"
    while [ "$waited" -lt "$settle" ]; do
        read -r status blocked <<<"$(curl -s "${api}/downloads" "${auth[@]}" | "$PY" -c "
import json, sys
try:
    payload = json.load(sys.stdin)
except Exception:
    print('nojson', '-'); raise SystemExit
rows = payload if isinstance(payload, list) else (payload.get('items') or payload.get('downloads') or [])
if not rows:
    print('norow', '-'); raise SystemExit
row = rows[0]
reason = row.get('importBlockReason') or '-'
print(row.get('status') or row.get('statusText') or '?', str(reason).replace(' ', '_'))
" 2>/dev/null)"
        case "$status" in
            *[Bb]locked*|*[Mm]oved*|*[Ff]ailed*|*[Ii]mported*) break ;;
        esac
        sleep 6; waited=$((waited + 6))
    done
    [ "$CASE_STATUS" = "nograb" ] || CASE_STATUS="${status:-unknown}"
    CASE_BLOCK="${blocked:--}"

    # Did the library item actually become present? A status is a claim; a file in the root
    # folder is the fact. Checked on the host, outside the container's own reporting.
    local placed
    placed="$(find "${base}/lib" -type f \( -name '*.mp3' -o -name '*.m4b' -o -name '*.m4a' \
        -o -name '*.flac' -o -name '*.mp4' \) 2>/dev/null | wc -l)"
    CASE_LIBRARY="$placed"

    "$RUNTIME" logs "$container" >"$CASE_LOG" 2>&1
    log "  after ${waited}s: status=${CASE_STATUS} blockReason=${CASE_BLOCK} filesInLibrary=${placed}"
    log "  container log: ${CASE_LOG}"

    # The lines that actually say why, for a reporter to grep for on their own instance.
    grep -nE "Unable to import|No importable files|do not match|No audio files were registered|cannot be pinned|No successful audio import|Import(ing|ed)? |Blocked|Resolved [0-9]+ file|remaining after checking" \
        "$CASE_LOG" | tail -25 | sed 's/^/    | /'

    if [ "$KEEP" = "1" ]; then
        log "  --keep: leaving ${base} and the container in place"
        trap - RETURN
        return 0
    fi

    # Verified teardown, printed. Not asserted.
    eval "$cleanup"
    trap - RETURN
    log "  teardown: podman ps -a for this case -> '$("$RUNTIME" ps -a --filter "name=${container}" --format '{{.Names}}' | tr '\n' ' ')'"
    log "  teardown: podman network ls for this case -> '$("$RUNTIME" network ls --filter "name=${network}" --format '{{.Name}}' | tr '\n' ' ')'"
    log "  teardown: ${port} has $(ss -ltn 2>/dev/null | grep -c ":${port} ") listener(s), ${stub_port} has $(ss -ltn 2>/dev/null | grep -c ":${stub_port} ")"
    log "  teardown: production ${PRODUCTION_PORT} still has $(ss -ltn 2>/dev/null | grep -c ":${PRODUCTION_PORT} ") listener(s), none of them ours"
    rm -rf "$base"
    return 0
}

# ---------------------------------------------------------------------------------------------
ROWS=()
record() { ROWS+=("$1|$2|$3|$4|$5|$6"); }

wanted() { [ -z "$ONLY" ] || [ "$ONLY" = "$1" ]; }

RESULT=0
BROKEN=0

trap 'stop_torznab' EXIT
start_torznab || { fail "no indexer stub, nothing can be measured"; exit 3; }

log "image: ${IMAGE}"
log "image digest: $("$RUNTIME" image inspect "$IMAGE" --format '{{.Digest}}' 2>/dev/null)"
log "baseline: production ${PRODUCTION_PORT} has $(ss -ltn 2>/dev/null | grep -c ":${PRODUCTION_PORT} ") listener(s) before we start"

# --- Control. Everything below is meaningless if this row does not import. --------------------
if wanted autoControl; then
    log "CONTROL autoControl: single .mp3, local storage, automatic path — MUST IMPORT"
    run_case autoControl mp3 single 1 auto local 0
    record autoControl "single .mp3, local, auto" "$CASE_STATUS" "$CASE_BLOCK" "$CASE_LIBRARY" "$CASE_STAMP"
    case "$CASE_STATUS" in
        *[Mm]oved*|*[Ii]mported*)
            [ "$CASE_LIBRARY" -ge 1 ] || { fail "BROKEN CHECK: control says imported but no file landed in the root folder"; BROKEN=1; }
            ;;
        *) fail "BROKEN CHECK: the control did not import (status ${CASE_STATUS})."
           fail "              This script is measuring its own plumbing. No verdict below is worth anything."
           BROKEN=1 ;;
    esac
    [ "$BROKEN" = "1" ] && { fail "stopping rather than reporting a verdict"; exit 3; }
    log "control OK: a plain single-file mp3 imports through the automatic path"
fi

if wanted manualControl; then
    log "CONTROL manualControl: the same file through MANUAL import — must come out differently to prove an asymmetry"
    run_case manualControl mp3 single 1 manual local 0
    record manualControl "single .mp3, local, manual" "$CASE_STATUS" "$CASE_BLOCK" "$CASE_LIBRARY" "$CASE_STAMP"
fi

# --- The rows that decide the reporters' question ---------------------------------------------
if wanted mp4Only; then
    log "case mp4Only: a torrent whose only payload is a literal .mp4"
    run_case mp4Only mp4 single 1 auto local 0
    record mp4Only "single .mp4, local, auto" "$CASE_STATUS" "$CASE_BLOCK" "$CASE_LIBRARY" "$CASE_STAMP"
fi

if wanted m4bSingle; then
    log "case m4bSingle: an allowlisted format other than mp3"
    run_case m4bSingle m4b single 1 auto local 0
    record m4bSingle "single .m4b, local, auto" "$CASE_STATUS" "$CASE_BLOCK" "$CASE_LIBRARY" "$CASE_STAMP"
fi

if [ "$WEAK_FS" = "1" ] && wanted weakFs; then
    log "case weakFs: the control's file, read through fuse-overlayfs"
    run_case weakFs mp3 single 1 auto weak 0
    record weakFs "single .mp3, fuse-overlayfs, auto" "$CASE_STATUS" "$CASE_BLOCK" "$CASE_LIBRARY" "$CASE_STAMP"
fi

if wanted m4aSingle; then
    run_case m4aSingle m4a single 1 auto local 0
    record m4aSingle "single .m4a, local, auto" "$CASE_STATUS" "$CASE_BLOCK" "$CASE_LIBRARY" "$CASE_STAMP"
fi

if wanted flacSingle; then
    run_case flacSingle flac single 1 auto local 0
    record flacSingle "single .flac, local, auto" "$CASE_STATUS" "$CASE_BLOCK" "$CASE_LIBRARY" "$CASE_STAMP"
fi

if wanted folderThree; then
    run_case folderThree mp3 folder 3 auto local 0
    record folderThree "3 .mp3 in a folder, local, auto" "$CASE_STATUS" "$CASE_BLOCK" "$CASE_LIBRARY" "$CASE_STAMP"
fi

if wanted nestedTwo; then
    run_case nestedTwo mp3 nested 2 auto local 0
    record nestedTwo "2 .mp3 nested one level, local, auto" "$CASE_STATUS" "$CASE_BLOCK" "$CASE_LIBRARY" "$CASE_STAMP"
fi

if wanted heldOpen; then
    run_case heldOpen mp3 single 1 auto local 1
    record heldOpen "single .mp3, client holds it open, auto" "$CASE_STATUS" "$CASE_BLOCK" "$CASE_LIBRARY" "$CASE_STAMP"
fi

echo
log "MATRIX"
printf '%-16s %-42s %-16s %-34s %-6s %s\n' case "on disk / path" status blockReason files stamp
for row in "${ROWS[@]}"; do
    IFS='|' read -r n d s b f v <<<"$row"
    printf '%-16s %-42s %-16s %-34s %-6s %s\n' "$n" "$d" "$s" "$b" "$f" "$v"
done

echo
log "final teardown check"
log "  our containers left: '$("$RUNTIME" ps -a --filter 'name=m890-' --format '{{.Names}}' | tr '\n' ' ')'"
log "  our networks left:   '$("$RUNTIME" network ls --filter 'name=m890-' --format '{{.Name}}' | tr '\n' ' ')'"
log "  production ${PRODUCTION_PORT}: $(ss -ltn 2>/dev/null | grep -c ":${PRODUCTION_PORT} ") listener(s)"
exit "$RESULT"
