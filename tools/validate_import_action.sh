#!/usr/bin/env bash
#
# validate_import_action.sh — prove, on Linux, what a completed-file import action actually does.
#
# Listenarr#598 asks for Linux validation of the hardlink/copy action the maintainer wrote but could
# not test. The unit tests there assert both files exist with equal content, which a plain COPY also
# satisfies, so they never distinguish a hardlink from a copy and never exercise the cross-device
# fallback. The same gap applies to any new action: link, symlink and copy are indistinguishable by
# content and only differ in filesystem metadata.
#
# So this drives the real action through the manual-import API and inspects the result on the host,
# classifying it as symlink, hardlink or copy:
#
#   hardlink/copy, same mount      -> a real HARDLINK: destination and source share one inode and
#                                     the link count is >= 2.
#   hardlink/copy, separate mounts -> link() returns EXDEV, so it must fall back to a COPY: a
#                                     distinct inode, with the fallback recorded in the log.
#   symlink, either layout         -> a SYMLINK whose target resolves to the source. Unlike a
#                                     hardlink this is expected to work across mounts as well, which
#                                     is the whole point of the action (Listenarr#771).
#   move, either layout            -> the file is RELOCATED: a plain file at the destination and
#                                     nothing left at the source. rename() returns EXDEV across a
#                                     mount boundary exactly as link() does, so this action needs a
#                                     copy-then-unlink fallback for the separate-mount case in the
#                                     same way hardlink/copy needs a copy fallback.
#
# The source assertion is the one thing that is NOT uniform across actions, and getting it wrong
# would invert the test. hardlink, copy and symlink must all PRESERVE the source, because an import
# that removes it is data loss. A move must REMOVE it, because that is what the word means. So a
# move that leaves the source behind has silently done a copy and doubled the library's size, and a
# move that removes the source without producing a destination has destroyed the file.
#
# Move also needs a verdict for "nothing happened at all". A cross-device move with no fallback does
# not report an error, it just never completes, so the destination simply never appears. Reading
# that as an ordinary failure would miss it; it is reported here as `stalled`.
#
# A pinned ffprobe is provisioned up front (tools/ffprobe_provisioner.py) so manual-import's metadata
# step does not hard-fail on the first-boot download race. Exits non-zero if any case fails.
#
#   ./tools/validate_import_action.sh localhost/listenarr-vet:pr717
#   ./tools/validate_import_action.sh localhost/listenarr-vet:pr771 --action symlink
#
set -uo pipefail
unset TMOUT

IMAGE="${1:?usage: validate_import_action.sh <image> [--action hardlink/copy|symlink|move] [--port N] [--settle N]}"
shift
ACTION="hardlink/copy"
PORT=4620
SETTLE=45          # seconds to wait for a destination to appear before calling it stalled
while [ $# -gt 0 ]; do
    case "$1" in
        --action) ACTION="$2"; shift 2 ;;
        --port)   PORT="$2";   shift 2 ;;
        --settle) SETTLE="$2"; shift 2 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done
case "$ACTION" in
    hardlink/copy|symlink|move) ;;
    *) echo "unknown --action '${ACTION}' (expected: hardlink/copy, symlink, move)" >&2; exit 2 ;;
esac

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${ROOT}/.venv/bin/python"
RUNTIME=podman

log()  { printf '%s [import] %s\n' "$(date +%H:%M:%S)" "$*"; }
fail() { printf '%s [import] FAIL: %s\n' "$(date +%H:%M:%S)" "$*"; }

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

# Run one case. Args: <name> <expect: hardlink|copy|symlink> <src-container-path> <root-container-path>
#                      <host-src-file> <host-dest-dir> <mount-args...>
run_case() {
    local name="$1" expect="$2" csrc="$3" croot="$4" hsrc="$5" hdestdir="$6"; shift 6
    local container="impval-${name}-$$" cfg; cfg="$(mktemp -d)"
    trap '"$RUNTIME" rm -f "$container" >/dev/null 2>&1 || true' RETURN

    log "case ${name}: action ${ACTION}, expect ${expect}"
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

    local req; req="$(ACTION="$ACTION" "$PY" - "$csrc" "$id" <<'REQEOF'
import json, os, sys
full = sys.argv[1]; aid = int(sys.argv[2])
print(json.dumps({"path": os.path.dirname(full), "action": os.environ["ACTION"], "items": [
    {"relativePath": os.path.basename(full), "fullPath": full, "matchedAudiobookId": aid}]}))
REQEOF
)"
    curl -s -X POST "${api}/library/manual-import" "${auth[@]}" -d "$req" >/dev/null

    # Wait for a destination to appear rather than sleeping a flat interval. A cross-device move
    # with no fallback never errors and never finishes, so "nothing arrived" is a real outcome that
    # has to be told apart from "not yet". The wait is bounded and the elapsed time is reported, so
    # a stall is visible as a stall instead of looking like a slow machine.
    local waited=0
    while [ "$waited" -lt "$SETTLE" ]; do
        [ -n "$(find "$hdestdir" \( -type f -o -type l \) -name '*.m4b' -print -quit 2>/dev/null)" ] && break
        sleep 3; waited=$((waited + 3))
    done
    sleep 2   # let a late write settle before classifying
    log "  waited ${waited}s for a destination"

    # Classify on the host. Symlinks are checked FIRST: stat follows a symlink, so a link pointing
    # at the source reports the source's own inode and would otherwise be read as a hardlink.
    #
    # A symlink written by the container records the CONTAINER's path for the source, so it will not
    # resolve when read from the host, where that path does not exist. That is correct behaviour and
    # not a broken link, so compare the recorded target against the container-side source path
    # rather than trying to follow it here. Judging it with readlink -f on the host reports every
    # working symlink as broken.
    local src_inode; src_inode="$(stat -c '%i' "$hsrc")"
    local dest verdict=missing
    while IFS= read -r -d '' dest; do
        if [ -L "$dest" ]; then
            local target; target="$(readlink "$dest" 2>/dev/null || true)"
            if [ "$target" = "$csrc" ]; then
                verdict=symlink
            else
                verdict=wrongTargetSymlink
            fi
            log "  dest is a symlink -> ${target:-nothing} (source in container: ${csrc}) -> ${verdict}"
            continue
        fi
        local di dl; di="$(stat -c '%i' "$dest")"; dl="$(stat -c '%h' "$dest")"
        if [ "$di" = "$src_inode" ] && [ "$dl" -ge 2 ]; then verdict=hardlink; else verdict=copy; fi
        log "  dest inode=${di} links=${dl} -> ${verdict}"
    done < <(find "$hdestdir" \( -type f -o -type l \) -name '*.m4b' -print0)

    # The source assertion is action-dependent, and inverting it is the easy way to write a test
    # that passes on a broken move. Every other action must leave the source alone; a move must
    # consume it.
    if [ "$ACTION" = "move" ]; then
        if [ -e "$hsrc" ]; then
            if [ "$verdict" = "missing" ]; then
                # Nothing arrived and the source is untouched. That is the cross-device stall:
                # no destination, no error, no data lost, and no way for an operator to see it.
                verdict=stalled
                log "  no destination after ${waited}s and the source is untouched -> stalled"
            else
                # A destination exists but the source survives, so this duplicated the file
                # instead of relocating it. Reported distinctly because it is not a stall and it
                # quietly doubles what the library occupies.
                verdict=copiedNotMoved
                log "  destination exists but source survives -> copiedNotMoved"
            fi
        elif [ "$verdict" = "missing" ]; then
            fail "${name}: SOURCE REMOVED AND NO DESTINATION WRITTEN (data loss)"
            "$RUNTIME" logs "$container" 2>&1 | grep -iE "move|rename|EXDEV|cross|mutation" | tail -6
            return 1
        else
            verdict=moved
            log "  source consumed and destination written -> moved"
        fi
    else
        [ -e "$hsrc" ] && log "  source preserved: yes" || { fail "${name}: SOURCE REMOVED (data loss)"; return 1; }
    fi

    if [ "$verdict" != "$expect" ]; then
        fail "${name}: expected ${expect}, observed ${verdict}"
        "$RUNTIME" logs "$container" 2>&1 | grep -iE "hardlink|symlink|mutation|copy|link" | tail -4
        return 1
    fi
    log "case ${name}: OK (${verdict} as expected)"
    return 0
}

# A hardlink cannot cross a mount boundary and a symlink can, so the two actions expect different
# outcomes from the same two layouts. That difference is the reason the symlink action exists.
if [ "$ACTION" = "symlink" ]; then
    EXPECT_SAME=symlink
    EXPECT_SEPARATE=symlink
elif [ "$ACTION" = "move" ]; then
    # A move is a move on either layout. Crossing a mount boundary changes how it must be
    # implemented, not what the caller asked for: rename() fails with EXDEV and the action has to
    # fall back to copy-then-unlink. Expecting anything other than `moved` for the separate-mount
    # case would be encoding the bug as the specification.
    EXPECT_SAME=moved
    EXPECT_SEPARATE=moved
else
    EXPECT_SAME=hardlink
    EXPECT_SEPARATE=copy
fi

# --- Case A: source and library on ONE mount ------------------------------------------
A_BASE="$(mktemp -d)"; mkdir -p "$A_BASE/lib" "$A_BASE/src"
"$PY" "${ROOT}/tools/generate_library.py" --scenario happy-path --out "$A_BASE/src" --seed 1 --limit 1 --force >/dev/null 2>&1
A_SRC="$(find "$A_BASE/src" -name '*.m4b' | head -1)"
run_case sameMount "$EXPECT_SAME" "/data/src${A_SRC#$A_BASE/src}" /data/lib "$A_SRC" "$A_BASE/lib" \
    -v "${A_BASE}:/data" || RESULT=1

# --- Case B: source and library on SEPARATE mounts ------------------------------------
B_SRC_DIR="$(mktemp -d)"; B_LIB_DIR="$(mktemp -d)"
"$PY" "${ROOT}/tools/generate_library.py" --scenario happy-path --out "$B_SRC_DIR" --seed 1 --limit 1 --force >/dev/null 2>&1
B_SRC="$(find "$B_SRC_DIR" -name '*.m4b' | head -1)"
run_case sepMounts "$EXPECT_SEPARATE" "/src${B_SRC#$B_SRC_DIR}" /audiobooks "$B_SRC" "$B_LIB_DIR" \
    -v "${B_SRC_DIR}:/src" -v "${B_LIB_DIR}:/audiobooks" || RESULT=1

echo
if [ "$RESULT" -eq 0 ]; then
    if [ "$ACTION" = "symlink" ]; then
        log "VALIDATION PASSED: symlink proven on one mount and across separate mounts."
    elif [ "$ACTION" = "move" ]; then
        log "VALIDATION PASSED: move relocated the file on one mount and across separate mounts,"
        log "                   consuming the source in both and losing nothing."
    else
        log "VALIDATION PASSED: hardlink proven on same mount; copy-fallback proven across mounts."
    fi
else
    fail "VALIDATION FAILED — see cases above."
fi
exit "$RESULT"
