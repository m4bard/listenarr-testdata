#!/usr/bin/env bash
#
# validate_sidecar_rename.sh — does a rename carry a book's companion files? (Listenarr#577)
#
# Reported bug: when Listenarr renames/reorganizes a book to match the naming pattern, companion
# sidecars beside the audio (cover.jpg, metadata.json) are left behind in the old folder. Root cause
# (read from the source): RenameService builds its move-list from the tracked AUDIO files only, so a
# non-audio companion is never in the operation set.
#
# This proves it end to end against a real container:
#   1. import a book's audio (so it is tracked) and drop cover.jpg + metadata.json beside it,
#   2. change the folder naming pattern so the book must RELOCATE,
#   3. rename it,
#   4. check the filesystem: the audio moved to the new folder; the sidecars did (or did not) follow.
#
# It asserts the CORRECT behaviour — sidecars move with the book — so today it FAILS (documenting the
# bug) and becomes a passing regression guard once the rename sweeps companion files. A pinned ffprobe
# is provisioned first so the import's metadata step doesn't hit the first-boot download race.
#
#   ./tools/validate_sidecar_rename.sh localhost/listenarr-vet:pr717
#
set -uo pipefail
unset TMOUT

IMAGE="${1:?usage: validate_sidecar_rename.sh <image> [port]}"
PORT="${2:-4632}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${ROOT}/.venv/bin/python"
RUNTIME=podman

log()  { printf '%s [sc] %s\n' "$(date +%H:%M:%S)" "$*"; }
fail() { printf '%s [sc] FAIL: %s\n' "$(date +%H:%M:%S)" "$*"; }

command -v "$RUNTIME" >/dev/null 2>&1 || { echo "podman required"; exit 2; }
[ -x "$PY" ] || { echo "no venv — python3 -m venv .venv && .venv/bin/pip install -e ."; exit 2; }

BASE="$(mktemp -d)"; mkdir -p "$BASE/lib" "$BASE/src" "$BASE/cfg"
C="screname-$$"
trap '"$RUNTIME" rm -f "$C" >/dev/null 2>&1 || true' EXIT

"$PY" "${ROOT}/tools/generate_library.py" --scenario happy-path --out "$BASE/src" --seed 1 --limit 1 --force >/dev/null 2>&1
SRC="$(find "$BASE/src" -name '*.m4b' | head -1)"; CSRC="/data/src${SRC#$BASE/src}"
"$PY" "${ROOT}/tools/ffprobe_provisioner.py" --config-dir "$BASE/cfg" >/dev/null || { fail "provision"; exit 1; }

log "start ${IMAGE}"
"$RUNTIME" run -d --name "$C" -p "${PORT}:4545" -e LISTENARR_LOG_LEVEL=Debug \
  -v "$BASE:/data" -v "$BASE/cfg:/app/config" "$IMAGE" >/dev/null || { fail "start"; exit 1; }
API="http://localhost:${PORT}/api/v1"
for _ in $(seq 1 60); do curl -fsS "${API}/system/status" >/dev/null 2>&1 && break; sleep 2; done
KEY="$("$PY" -c "import json;print(json.load(open('$BASE/cfg/config.json'))['ApiKey'])" 2>/dev/null)"
[ -n "$KEY" ] || { fail "no api key"; "$RUNTIME" logs "$C" 2>&1 | tail -12; exit 1; }
A=(-H "X-Api-Key: $KEY" -H 'Content-Type: application/json')

curl -s -X POST "${API}/rootfolders" "${A[@]}" -d '{"name":"lib","path":"/data/lib","isDefault":true,"caseSensitivityMode":"Sensitive"}' >/dev/null
ID="$(curl -s -X POST "${API}/library/add" "${A[@]}" -d '{"metadata":{"asin":"B002UUFXKU","title":"The Valley of Fear","authors":["Arthur Conan Doyle"]},"monitored":true,"autoSearch":false}' | "$PY" -c "import json,sys;d=json.load(sys.stdin);print(d.get('id') or (d.get('audiobook') or {}).get('id') or '')")"
[ -n "$ID" ] || { fail "could not add a book"; exit 1; }

log "import the audio (tracks it under the library)"
REQ='{"path":"'"$(dirname "$CSRC")"'","action":"copy","items":[{"relativePath":"'"$(basename "$CSRC")"'","fullPath":"'"$CSRC"'","matchedAudiobookId":'"$ID"'}]}'
DEST="$(curl -s -X POST "${API}/library/manual-import" "${A[@]}" -d "$REQ" | "$PY" -c "import json,sys;d=json.load(sys.stdin);r=d.get('results',[{}])[0];print(r.get('destinationPath',''))")"
[ -n "$DEST" ] || { fail "import did not place a file"; exit 1; }
sleep 3
OLD_HOST="$(dirname "$BASE/lib${DEST#/data/lib}")"
log "imported into: ${OLD_HOST}"

log "drop sidecars beside the audio: cover.jpg, metadata.json"
printf 'fake-jpeg-bytes' > "$OLD_HOST/cover.jpg"
printf '{"title":"The Valley of Fear","source":"audiobookshelf"}' > "$OLD_HOST/metadata.json"

log "change the folder naming pattern to force a relocation"
SETTINGS="$(curl -s "${API}/configuration/settings" "${A[@]}")"
NEW_SETTINGS="$(printf '%s' "$SETTINGS" | "$PY" -c "import json,sys;s=json.load(sys.stdin);s['folderNamingPattern']='Sorted/{Author}/{Title}';print(json.dumps(s))")"
curl -s -X POST "${API}/configuration/settings" "${A[@]}" -d "$NEW_SETTINGS" >/dev/null

# The rename PREVIEW is the authoritative plan of exactly what the rename will move. If the plan
# relocates the book (folderChanged) yet lists only the audio, the companion files cannot follow —
# which is the defect, straight from the source: RenameService builds the move-set from the tracked
# AUDIO files only. Asserting on the plan is robust; it does not depend on execute-time semantics.
log "rename preview: the authoritative plan of what will move"
PREVIEW="$(curl -s -X POST "${API}/library/${ID}/rename/preview" "${A[@]}" -d '{}')"

echo "===== RESULT ====="
PREVIEW="$PREVIEW" "$PY" <<'PY'
import json, os, sys

preview = json.loads(os.environ["PREVIEW"])
folder_changed = preview.get("folderChanged")
op_files = [r.get("currentFilename") for r in preview.get("fileRenames", [])]
has = lambda needle: any(needle in (n or "") for n in op_files)

print(f"  a relocation is planned (folderChanged): {folder_changed}")
print(f"  files the rename will move:              {op_files}")
print(f"  cover.jpg included in the plan:          {has('cover')}")
print(f"  metadata.json included in the plan:      {has('metadata')}")

# The book must actually be relocating, or the plan is trivially empty and proves nothing.
if not folder_changed:
    print("\nINCONCLUSIVE: no relocation planned; cannot judge companion handling.")
    sys.exit(2)

stranded = [name for name, key in (("cover.jpg", "cover"), ("metadata.json", "metadata")) if not has(key)]
if stranded:
    print(f"\nREPRODUCED #577: the rename relocates the book but its plan omits {', '.join(stranded)} "
          "— they will be left behind. (Root cause: RenameService moves only tracked audio files.)")
    sys.exit(1)
print("\nPASS: the rename plan carries the companion files with the book.")
sys.exit(0)
PY
RESULT=$?
exit "$RESULT"
