#!/usr/bin/env bash
#
# validate_queue_poll_resilience.sh — does one unreadable field in a download client's queue
# response cost you the rest of the queue?
#
# Listenarr reads a qBittorrent queue by deserializing the `/api/v2/torrents/info` array into
# JsonElements and pulling typed values out of each torrent. The typed accessors it uses throw
# when a value arrives in a JSON token form they do not expect, and the loop that walks the
# torrents does not catch per iteration. So the interesting question is not "does a bad field
# throw" — of course it does — but what happens to the torrents AFTER it.
#
# There are three possible answers and they are very different to operate:
#
#   whole poll fails   the client is reported unavailable, the stale-snapshot machinery kicks
#                      in, and an operator sees something is wrong.
#   one torrent lost   the bad row is skipped, the other N-1 arrive, a warning names the hash.
#   batch truncated    everything from the bad row onward disappears, and the poll still
#                      reports itself healthy and live. Nothing downstream can tell this from
#                      a queue that genuinely got shorter.
#
# The third is the one worth having a check for, because it is invisible. This script tells the
# three apart by serving a queue of known length with the malformed torrent at a known position,
# then counting what Listenarr reports and reading back the health flags it publishes alongside.
#
# tools/qbittorrent_stub.py stands in for the client. Nothing here needs a real qBittorrent, and
# nothing here depends on a real library: the queue is generated, so its length is a fact this
# repo owns rather than something to be taken on trust.
#
# THE CONTROL MUST PASS. Case `allWellFormed` serves the same N torrents with nothing wrong with
# them. If Listenarr does not report all N for that, then this script is measuring the display
# filter or the stub or the settings write, and every other result it prints is meaningless. It
# is checked first and a failure there is reported as a BROKEN CHECK, not as a bug found.
#
# Cases run:
#
#   allWellFormed     control. N torrents, none malformed             -> expect N
#   floatMidBatch     torrent K of N carries a fractional `downloaded`  -> expect N
#   stringMidBatch    torrent K of N carries a quoted `downloaded`      -> expect N
#   floatFirst        torrent 1 of N carries a fractional `downloaded`  -> expect N
#
# Every non-control case expects N, i.e. it asserts the FIXED behaviour: one unreadable field
# should cost at most the torrent it is on. A build with the defect returns K-1 instead and the
# case fails, which is the polarity the rest of the tools in this repo use.
#
# `stringMidBatch` is included because it is the same defect approached from the other side and
# it has already been reported once for a different client (Listenarrs/Listenarr#618, #619, on
# NZBGet). A fix that only handles fractional numbers would pass floatMidBatch and still fail
# this one.
#
#   ./tools/validate_queue_poll_resilience.sh
#   ./tools/validate_queue_poll_resilience.sh --image localhost/listenarr-vet:fix --count 8 --index 4
#
set -uo pipefail
unset TMOUT

IMAGE="ghcr.io/listenarrs/listenarr:canary"
PORT=14620
STUB_PORT=18111
COUNT=6
INDEX=3
SETTLE=20

while [ $# -gt 0 ]; do
    case "$1" in
        --image)     IMAGE="$2";     shift 2 ;;
        --port)      PORT="$2";      shift 2 ;;
        --stub-port) STUB_PORT="$2"; shift 2 ;;
        --count)     COUNT="$2";     shift 2 ;;
        --index)     INDEX="$2";     shift 2 ;;
        --settle)    SETTLE="$2";    shift 2 ;;
        -h|--help)   sed -n '2,50p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${ROOT}/.venv/bin/python"
RUNTIME=podman
STUB="${ROOT}/tools/qbittorrent_stub.py"

log()  { printf '%s [queuepoll] %s\n' "$(date +%H:%M:%S)" "$*"; }
fail() { printf '%s [queuepoll] FAIL: %s\n' "$(date +%H:%M:%S)" "$*"; }

command -v "$RUNTIME" >/dev/null 2>&1 || { echo "podman required"; exit 2; }
[ -x "$PY" ] || { echo "no venv — python3 -m venv .venv && .venv/bin/pip install -e ."; exit 2; }
[ -f "$STUB" ] || { echo "missing ${STUB}"; exit 2; }

[ "$INDEX" -ge 1 ] && [ "$INDEX" -le "$COUNT" ] || { echo "--index must be within 1..--count" >&2; exit 2; }

RESULT=0
BROKEN=0

# Run one case. Args: <name> <malformed-index (0 = none)> <kind>
# Echoes nothing; sets CASE_ITEMS, CASE_LOGGED, CASE_UNAVAILABLE, CASE_STALE as globals so the
# caller can report the health flags as well as the count. The flags matter as much as the
# count: a truncation that also marked the client unavailable would at least be visible.
CASE_ITEMS=-1; CASE_LOGGED=-1; CASE_UNAVAILABLE=unknown; CASE_STALE=unknown
run_case() {
    local name="$1" malformed="$2" kind="$3"
    local container="qpoll-${name}-$$" cfg stub_log stub_pid
    cfg="$(mktemp -d)"; stub_log="$(mktemp)"

    CASE_ITEMS=-1; CASE_LOGGED=-1; CASE_UNAVAILABLE=unknown; CASE_STALE=unknown

    local stub_args=(--port "$STUB_PORT" --count "$COUNT")
    [ "$malformed" -ne 0 ] && stub_args+=(--malformed-index "$malformed" --malformed-kind "$kind")
    "$PY" "$STUB" "${stub_args[@]}" >"$stub_log" 2>&1 &
    stub_pid=$!

    # The stub and the container both have to go away even when a case bails out early,
    # otherwise the next case binds a busy port and fails for the wrong reason.
    trap '
        kill "'"$stub_pid"'" 2>/dev/null || true
        "'"$RUNTIME"'" rm -f "'"$container"'" >/dev/null 2>&1 || true
    ' RETURN

    local ready=0 _
    for _ in $(seq 1 20); do
        curl -fsS "http://127.0.0.1:${STUB_PORT}/api/v2/app/version" >/dev/null 2>&1 && { ready=1; break; }
        sleep 1
    done
    [ "$ready" -eq 1 ] || { fail "${name}: stub did not start on :${STUB_PORT}"; return 1; }

    "$RUNTIME" run -d --name "$container" -p "${PORT}:4545" -e LISTENARR_LOG_LEVEL=Debug \
        -v "${cfg}:/app/config" "$IMAGE" >/dev/null || { fail "${name}: container did not start"; return 1; }

    local api="http://localhost:${PORT}/api/v1"
    local up=0
    for _ in $(seq 1 60); do curl -fsS "${api}/system/status" >/dev/null 2>&1 && { up=1; break; }; sleep 2; done
    [ "$up" -eq 1 ] || { fail "${name}: API never came up"; "$RUNTIME" logs "$container" 2>&1 | tail -15; return 1; }

    local key; key="$("$PY" -c "import json;print(json.load(open('${cfg}/config.json'))['ApiKey'])" 2>/dev/null)"
    [ -n "$key" ] || { fail "${name}: no api key"; return 1; }
    local auth=(-H "X-Api-Key: ${key}" -H 'Content-Type: application/json')

    # The container reaches the stub on the host through podman's host alias rather than through
    # a published port, so the stub never has to be exposed beyond this machine.
    curl -s -X POST "${api}/download-clients" "${auth[@]}" -d "{
        \"name\":\"stub-qb\",\"type\":\"qbittorrent\",\"host\":\"host.containers.internal\",
        \"port\":${STUB_PORT},\"username\":\"admin\",\"password\":\"admin\",
        \"useSSL\":false,\"isEnabled\":true}" >/dev/null

    # Listenarr hides queue rows from an external client that it does not itself track, unless
    # they are completed AND this setting is on. The stub serves completed torrents, so without
    # this the API reports zero for every case and the control catches it.
    local settings; settings="$(mktemp)"
    curl -s "${api}/configuration/settings" "${auth[@]}" \
        | "$PY" -c "import json,sys;d=json.load(sys.stdin);d['showCompletedExternalDownloads']=True;print(json.dumps(d))" >"$settings"
    curl -s -X POST "${api}/configuration/settings" "${auth[@]}" --data-binary "@${settings}" >/dev/null
    rm -f "$settings"

    # Poll until the queue stops changing rather than sleeping a flat interval: the first read
    # can land before the settings write has taken effect on a cached snapshot.
    local waited=0 body
    while [ "$waited" -lt "$SETTLE" ]; do
        sleep 4; waited=$((waited + 4))
        body="$(curl -s "${api}/download/queue" "${auth[@]}")"
        [ -n "$body" ] && break
    done
    body="$(curl -s "${api}/download/queue" "${auth[@]}")"

    read -r CASE_ITEMS CASE_UNAVAILABLE CASE_STALE <<<"$(printf '%s' "$body" | "$PY" -c "
import json,sys
d = json.load(sys.stdin)
print(len(d.get('items') or []), d.get('hasUnavailableClients'), d.get('hasStaleData'))
" 2>/dev/null)"

    # The adapter's own count, before the display filter touches it. Reading both tells a
    # truncated poll apart from a poll that was fine and a display rule that hid rows.
    CASE_LOGGED="$("$RUNTIME" logs "$container" 2>&1 \
        | grep -oE 'Client stub-qb has [0-9]+ queue items' | tail -1 | grep -oE '[0-9]+' || echo -1)"

    local unreachable; unreachable="$("$RUNTIME" logs "$container" 2>&1 | grep -c 'client may be unreachable' || true)"

    log "  case ${name}: adapter reported ${CASE_LOGGED}, API showed ${CASE_ITEMS} of ${COUNT}"
    log "  case ${name}: hasUnavailableClients=${CASE_UNAVAILABLE} hasStaleData=${CASE_STALE} unreachableLogLines=${unreachable}"
    if [ "$malformed" -ne 0 ]; then
        "$RUNTIME" logs "$container" 2>&1 | grep -E 'FormatException|InvalidOperationException' | head -2 | sed 's/^/    /'
    fi
    return 0
}

# --- Control first. Everything after it is only meaningful if this one holds. -------------
log "control: ${COUNT} well-formed torrents, expecting all ${COUNT}"
if ! run_case allWellFormed 0 float; then
    fail "control case could not be run"
    BROKEN=1
elif [ "$CASE_ITEMS" -ne "$COUNT" ] || [ "$CASE_LOGGED" -ne "$COUNT" ]; then
    fail "BROKEN CHECK: control served ${COUNT} good torrents but Listenarr reported ${CASE_LOGGED}/${CASE_ITEMS}."
    fail "              Nothing below distinguishes anything. Fix the harness before reading a verdict."
    BROKEN=1
else
    log "control OK: all ${COUNT} torrents survived a clean poll"
fi

if [ "$BROKEN" -eq 1 ]; then
    exit 3
fi

# --- The real cases ------------------------------------------------------------------------
verdict_case() {
    local name="$1" malformed="$2" kind="$3"
    log "case ${name}: torrent ${malformed} of ${COUNT} has a ${kind} 'downloaded', expecting all ${COUNT} to survive"
    if ! run_case "$name" "$malformed" "$kind"; then
        fail "${name}: could not be run"
        RESULT=1
        return
    fi
    if [ "$CASE_ITEMS" -eq "$COUNT" ]; then
        log "case ${name}: OK — the unreadable field cost nothing"
        return
    fi
    local expected_if_truncated=$((malformed - 1))
    if [ "$CASE_ITEMS" -eq "$expected_if_truncated" ]; then
        fail "${name}: BATCH TRUNCATED — ${CASE_ITEMS} of ${COUNT} survived, everything from torrent ${malformed} onward was dropped"
        if [ "$CASE_UNAVAILABLE" = "False" ] || [ "$CASE_UNAVAILABLE" = "false" ]; then
            fail "${name}: and the snapshot still reports itself healthy, so the loss is not visible anywhere"
        fi
    elif [ "$CASE_ITEMS" -eq $((COUNT - 1)) ]; then
        fail "${name}: one torrent lost (${CASE_ITEMS} of ${COUNT}) — degraded, but the batch was not truncated"
    else
        fail "${name}: ${CASE_ITEMS} of ${COUNT} survived — neither clean, nor truncated at ${malformed}"
    fi
    RESULT=1
}

verdict_case floatMidBatch  "$INDEX" float
verdict_case stringMidBatch "$INDEX" string
verdict_case floatFirst     1        float

echo
if [ "$RESULT" -eq 0 ]; then
    log "VALIDATION PASSED: a torrent with an unreadable numeric field costs at most that torrent."
else
    fail "VALIDATION FAILED — one unreadable field cost more than its own torrent. See cases above."
fi
exit "$RESULT"
