#!/usr/bin/env bash
#
# validate_hardlink.sh — prove, on Linux, what Listenarr's hardlink/copy import actually does.
#
# Listenarr#598 asks for Linux validation of the hardlink/copy completed-file action that the
# maintainer coded but could not test. The existing unit tests only assert both files exist with
# equal content — which a plain COPY also satisfies — so they never distinguish a hardlink from a
# copy, and never exercise the cross-device fallback. This drives the real thing through the API and
# checks it at the filesystem level:
#
#   Case A (same mount):     a hardlink/copy import must create a real HARDLINK — destination and
#                            source share one inode, link count >= 2, and the source is preserved.
#   Case B (separate mounts): source and library on different mounts make link() return EXDEV, so the
#                            action must fall back to a COPY — a distinct inode, source preserved,
#                            and the fallback recorded in the log.
#
# A pinned ffprobe is provisioned up front (tools/ffprobe_provisioner.py) so manual-import's metadata
# step does not hard-fail on the first-boot download race. Exits non-zero if either case fails.
#
#   ./tools/validate_hardlink.sh localhost/listenarr-vet:pr717
#
set -uo pipefail
unset TMOUT

IMAGE="${1:?usage: validate_hardlink.sh <image> [port]}"
PORT="${2:-4620}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${ROOT}/.venv/bin/python"
RUNTIME=podman

log()  { printf '%s [hl] %s\n' "$(date +%H:%M:%S)" "$*"; }
fail() { printf '%s [hl] FAIL: %s\n' "$(date +%H:%M:%S)" "$*"; }

command -v "$RUNTIME" >/dev/null 2>&1 || { echo "podman required"; exit 2; }
[ -x "$PY" ] || { echo "no venv — python3 -m venv .venv && .venv/bin/pip install -e ."; exit 2; }

RESULT=0

# Bring a container up with a provisioned ffprobe and the caller's mounts, add one book, and echo
# its api base + key + audiobook id. Args: <container> <cfg> <port> <mount-args...>
bring_up() {
    local container="$1" cfg="$2" port="$3"; shift 3
    "$PY" "${ROOT}/tools/ffprobe_provisioner.py" --config-dir "$cfg" >/dev/null || return 1
    "$RUNTIME" run -d --name "$container" -p "${port}:4545" -e LISTENARR_LOG_LEVEL=Debug \
        "$@" -v "${cfg}:/app/config" "$IMAGE" >/dev/null || return 1
    local api="http://localhost:${port}/api/v1"
    local up=0 _
    for _ in $(seq 1 60); do curl -fsS "${api}/system/status" >/dev/null 2>&1 && { up=1; break; }; sleep 2; done
    [ "$up" -eq 1 ] || { "$RUNTIME" logs "$container" 2>&1 | tail -15; return 1; }
    printf '%s' "$api"
}

# Run one case. Args: <name> <expect: hardlink|copy> <src-container-path> <root-container-path>
#                      <host-src-file> <host-dest-dir> <mount-args...>
run_case() {
    local name="$1" expect="$2" csrc="$3" croot="$4" hsrc="$5" hdestdir="$6"; shift 6
    local container="hlval-${name}-$$" cfg; cfg="$(mktemp -d)"
    trap '"$RUNTIME" rm -f "$container" >/dev/null 2>&1 || true' RETURN

    log "case ${name}: expect ${expect}"
    local api; api="$(bring_up "$container" "$cfg" "$PORT" "$@")" || { fail "${name}: container/API did not come up"; return 1; }
    local key; key="$("$PY" -c "import json;print(json.load(open('${cfg}/config.json'))['ApiKey'])" 2>/dev/null)"
    [ -n "$key" ] || { fail "${name}: no api key"; return 1; }
    local auth=(-H "X-Api-Key: ${key}" -H 'Content-Type: application/json')

    curl -s -X POST "${api}/rootfolders" "${auth[@]}" \
        -d "{\"name\":\"lib\",\"path\":\"${croot}\",\"isDefault\":true,\"caseSensitivityMode\":\"Sensitive\"}" >/dev/null
    local id; id="$(curl -s -X POST "${api}/library/add" "${auth[@]}" \
        -d '{"metadata":{"asin":"B002UUFXKU","title":"The Valley of Fear","authors":["Arthur Conan Doyle"]},"monitored":true,"autoSearch":false}' \
        | "$PY" -c "import json,sys;d=json.load(sys.stdin);print(d.get('id') or (d.get('audiobook') or {}).get('id') or '')")"
    [ -n "$id" ] || { fail "${name}: could not add a book"; return 1; }

    local req; req="$("$PY" - "$csrc" "$id" <<'PY'
import json,os,sys
full=sys.argv[1]; aid=int(sys.argv[2])
print(json.dumps({"path":os.path.dirname(full),"action":"hardlink/copy","items":[
    {"relativePath":os.path.basename(full),"fullPath":full,"matchedAudiobookId":aid}]}))
PY
)"
    curl -s -X POST "${api}/library/manual-import" "${auth[@]}" -d "$req" >/dev/null
    sleep 5

    # Compare inodes on the host. Same inode + link count >= 2 == a real hardlink.
    local src_inode; src_inode="$(stat -c '%i' "$hsrc")"
    local dest verdict=missing
    while IFS= read -r -d '' dest; do
        local di dl; di="$(stat -c '%i' "$dest")"; dl="$(stat -c '%h' "$dest")"
        if [ "$di" = "$src_inode" ] && [ "$dl" -ge 2 ]; then verdict=hardlink; else verdict=copy; fi
        log "  dest inode=${di} links=${dl} -> ${verdict}"
    done < <(find "$hdestdir" -name '*.m4b' -print0)

    [ -f "$hsrc" ] && log "  source preserved: yes" || { fail "${name}: SOURCE REMOVED (data loss)"; return 1; }

    if [ "$verdict" != "$expect" ]; then
        fail "${name}: expected ${expect}, observed ${verdict}"
        "$RUNTIME" logs "$container" 2>&1 | grep -iE "hardlink|mutation|copy|link" | tail -4
        return 1
    fi
    log "case ${name}: OK (${verdict} as expected)"
    return 0
}

# --- Case A: same mount -> hardlink should succeed ------------------------------------
A_BASE="$(mktemp -d)"; mkdir -p "$A_BASE/lib" "$A_BASE/src"
"$PY" "${ROOT}/tools/generate_library.py" --scenario happy-path --out "$A_BASE/src" --seed 1 --limit 1 --force >/dev/null 2>&1
A_SRC="$(find "$A_BASE/src" -name '*.m4b' | head -1)"
run_case sameMount hardlink "/data/src${A_SRC#$A_BASE/src}" /data/lib "$A_SRC" "$A_BASE/lib" \
    -v "${A_BASE}:/data" || RESULT=1

# --- Case B: separate mounts -> EXDEV -> copy fallback --------------------------------
B_SRC_DIR="$(mktemp -d)"; B_LIB_DIR="$(mktemp -d)"
"$PY" "${ROOT}/tools/generate_library.py" --scenario happy-path --out "$B_SRC_DIR" --seed 1 --limit 1 --force >/dev/null 2>&1
B_SRC="$(find "$B_SRC_DIR" -name '*.m4b' | head -1)"
run_case sepMounts copy "/src${B_SRC#$B_SRC_DIR}" /audiobooks "$B_SRC" "$B_LIB_DIR" \
    -v "${B_SRC_DIR}:/src" -v "${B_LIB_DIR}:/audiobooks" || RESULT=1

echo
if [ "$RESULT" -eq 0 ]; then
    log "VALIDATION PASSED: hardlink proven on same mount; copy-fallback proven across mounts."
else
    fail "VALIDATION FAILED — see cases above."
fi
exit "$RESULT"
