#!/usr/bin/env bash
#
# validate_companion_import.sh — do companion files beside the audio survive a manual import?
#
# `POST /library/manual-import` takes `includeCompanionFiles`. When it is set, ManualImportCompanionImporter
# sweeps the folders the selected audio came from and brings the non-audio files along. This tool asks
# whether that actually happens, against a running container, by looking at the filesystem afterwards.
#
# Read from the source first, so the tool knows what it is looking for. The companion pass calls
# EnsureCreatedHierarchyAsync(destinationDirectory, destinationRoot, ...), where `destinationRoot` is
# ManualImportPathPlanner.DetermineScanPath(...) — the common parent of the destination paths the import
# just produced, i.e. the BOOK folder. That argument is the managed boundary, and the boundary is handed
# to LibraryDirectoryOwnershipBoundaryAuthorizer.AuthorizeAsync, which requires the boundary to be
# *equivalent to* a configured root folder, not merely inside one. A book folder is not a root folder, so
# the authorizer refuses. The primary audio path does not hit this because it selects its boundary with
# LibraryDirectoryOwnershipPlanning.SelectMostSpecificBoundary over the configured root paths, which
# returns the root folder itself. Same call, different argument, and only one of them is a root.
#
# That reading predicts two things this tool checks rather than assumes:
#   - the failure has nothing to do with where the SOURCE lives, so it should reproduce identically
#     whether the source folder is outside every root folder or inside one,
#   - the audio still imports fine, and the API still reports success, so the loss is silent.
#
# Three named controls, because a companion check is unusually easy to write so that it cannot fail:
#
#   MUST ARRIVE      the selected audio file itself. The destination folder is taken from the API's own
#                    destinationPath, so if the audio is not there, the tool is looking in the wrong
#                    place or the import never ran, and nothing else it reports means anything.
#   MUST NOT ARRIVE  decoy.bak, with `.bak` put in the import blacklist first. If a blacklisted file
#                    lands at the destination, the inspection is not discriminating between files at all.
#   PROVENANCE, NOT EXISTENCE
#                    metadata.json. The bug report this tool was written against asserted that
#                    Listenarr regenerates its own metadata.json at the destination, which would make
#                    "is there a metadata.json at the destination?" pass on a build where the companion
#                    pass is completely broken. That premise does not hold on canary: there is no
#                    metadata.json writer anywhere in the tree, and no run of this tool has seen one
#                    appear. The control is kept anyway, because it costs one stat call and it is the
#                    difference between a check that survives a writer being added later and one that
#                    silently starts passing when that happens. Every companion this tool writes carries
#                    a per-run sentinel string, so the tool computes the naive verdict beside the
#                    provenance verdict and prints both, and says plainly when the masking it guards
#                    against did not occur. .nfo and .opf carry the finding regardless, because
#                    Listenarr generates neither, so nothing can mask their absence.
#
# Exit 0 the companions came across, 1 they were dropped, 2 the run could not be judged.
#
#   ./tools/validate_companion_import.sh --image ghcr.io/listenarrs/listenarr:canary
#   ./tools/validate_companion_import.sh --image ghcr.io/listenarrs/listenarr:canary --case inside
#
set -uo pipefail
unset TMOUT

IMAGE=""
PORT=4641
SETTLE=45
CASES="outside inside"

usage() {
    cat <<'USAGE'
usage: validate_companion_import.sh --image <image> [--case outside|inside|both] [--port N] [--settle N]

  --case outside   source folder outside every configured root folder (the ordinary download shape)
  --case inside    source folder inside the configured root folder
  --case both      run both (default)
USAGE
}

while [ $# -gt 0 ]; do
    case "$1" in
        --image)  IMAGE="$2";  shift 2 ;;
        --port)   PORT="$2";   shift 2 ;;
        --settle) SETTLE="$2"; shift 2 ;;
        --case)
            case "$2" in
                outside) CASES="outside" ;;
                inside)  CASES="inside" ;;
                both)    CASES="outside inside" ;;
                *) echo "--case must be outside, inside or both" >&2; exit 2 ;;
            esac
            shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done
[ -n "$IMAGE" ] || { usage >&2; exit 2; }

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${ROOT}/.venv/bin/python"
RUNTIME=podman

log()  { printf '%s [comp] %s\n' "$(date +%H:%M:%S)" "$*"; }
fail() { printf '%s [comp] FAIL: %s\n' "$(date +%H:%M:%S)" "$*"; }
die()  { printf '%s [comp] ERROR: %s\n' "$(date +%H:%M:%S)" "$*" >&2; exit 2; }

command -v "$RUNTIME" >/dev/null 2>&1 || die "podman required"
[ -x "$PY" ] || die "no venv — python3 -m venv .venv && .venv/bin/pip install -e ."

REPORT="$(mktemp)"
trap 'rm -f "$REPORT"' EXIT

# Run one case. Args: <case name: outside|inside>
run_case() {
    local casename="$1"
    local base; base="$(mktemp -d)"
    local container="compimp-${casename}-$$"
    local sentinel="LISTENARR-HARNESS-COMPANION-${casename}-$$"
    mkdir -p "$base/lib" "$base/src" "$base/cfg"
    chmod 755 "$base"

    log "case ${casename}: sentinel ${sentinel}"

    # The source folder is the only thing that differs between the two cases. `outside` puts it on a
    # path no root folder covers; `inside` puts it under the configured root. If the boundary reading
    # above is right, neither placement changes the outcome, because the boundary that gets authorized
    # is derived from the DESTINATION.
    local gen_out csrc_prefix
    if [ "$casename" = "inside" ]; then
        gen_out="$base/lib/_incoming"; csrc_prefix="/data/lib/_incoming"
    else
        gen_out="$base/src"; csrc_prefix="/data/src"
    fi
    mkdir -p "$gen_out"

    "$PY" "${ROOT}/tools/generate_library.py" --scenario happy-path --out "$gen_out" \
        --seed 1 --limit 1 --force >/dev/null 2>&1
    local hsrc; hsrc="$(find "$gen_out" -name '*.m4b' | head -1)"
    [ -n "$hsrc" ] || { fail "${casename}: generator produced no audio"; return 2; }
    local csrc="${csrc_prefix}${hsrc#$gen_out}"
    local hsrcdir; hsrcdir="$(dirname "$hsrc")"

    "$PY" "${ROOT}/tools/ffprobe_provisioner.py" --config-dir "$base/cfg" >/dev/null \
        || { fail "${casename}: could not provision ffprobe"; return 2; }

    log "  start ${IMAGE}"
    "$RUNTIME" run -d --name "$container" -p "${PORT}:4545" -e LISTENARR_LOG_LEVEL=Debug \
        -v "$base:/data" -v "$base/cfg:/app/config" "$IMAGE" >/dev/null \
        || { fail "${casename}: container did not start"; return 2; }

    local api="http://localhost:${PORT}/api/v1" up=0 _
    for _ in $(seq 1 60); do curl -fsS "${api}/system/status" >/dev/null 2>&1 && { up=1; break; }; sleep 2; done
    [ "$up" -eq 1 ] || { "$RUNTIME" logs "$container" 2>&1 | tail -15; fail "${casename}: API never came up"; return 2; }

    local key; key="$("$PY" -c "import json;print(json.load(open('${base}/cfg/config.json'))['ApiKey'])" 2>/dev/null)"
    [ -n "$key" ] || { fail "${casename}: no api key"; return 2; }
    local auth=(-H "X-Api-Key: ${key}" -H 'Content-Type: application/json')

    curl -s -X POST "${api}/rootfolders" "${auth[@]}" \
        -d '{"name":"lib","path":"/data/lib","isDefault":true,"caseSensitivityMode":"Sensitive"}' >/dev/null

    # Blacklist .bak so decoy.bak becomes a MUST-NOT-ARRIVE control. Without a file that is supposed to
    # be filtered, "nothing arrived" and "everything is filtered" look the same from the destination.
    local settings newsettings
    settings="$(curl -s "${api}/configuration/settings" "${auth[@]}")"
    newsettings="$(printf '%s' "$settings" | "$PY" -c \
        "import json,sys;s=json.load(sys.stdin);s['importBlacklistExtensions']=['.bak'];print(json.dumps(s))")"
    curl -s -X POST "${api}/configuration/settings" "${auth[@]}" -d "$newsettings" >/dev/null

    local id; id="$(curl -s -X POST "${api}/library/add" "${auth[@]}" \
        -d '{"metadata":{"asin":"B002UUFXKU","title":"The Valley of Fear","authors":["Arthur Conan Doyle"]},"monitored":true,"autoSearch":false}' \
        | "$PY" -c "import json,sys;d=json.load(sys.stdin);print(d.get('id') or (d.get('audiobook') or {}).get('id') or '')")"
    [ -n "$id" ] || { fail "${casename}: could not add a book"; return 2; }

    # Every companion carries the sentinel, so a file at the destination can be attributed to this run
    # rather than assumed. metadata.json in particular: if a build ever does generate one, only the
    # sentinel tells the copied file from the generated one.
    log "  drop companions beside the audio, each stamped with the sentinel"
    printf '<nfo>%s</nfo>\n' "$sentinel"                       > "$hsrcdir/book.nfo"
    printf '<package>%s</package>\n' "$sentinel"               > "$hsrcdir/book.opf"
    printf 'notes: %s\n' "$sentinel"                           > "$hsrcdir/reader-notes.txt"
    printf '{"title":"The Valley of Fear","sentinel":"%s"}\n' "$sentinel" > "$hsrcdir/metadata.json"
    printf 'decoy %s\n' "$sentinel"                            > "$hsrcdir/decoy.bak"

    log "  manual-import with includeCompanionFiles=true"
    local req; req="$("$PY" - "$csrc" "$id" <<'REQEOF'
import json, os, sys
full = sys.argv[1]; aid = int(sys.argv[2])
print(json.dumps({
    "path": os.path.dirname(full),
    "action": "copy",
    "includeCompanionFiles": True,
    "items": [{"relativePath": os.path.basename(full), "fullPath": full, "matchedAudiobookId": aid}],
}))
REQEOF
)"
    local resp; resp="$(curl -s -X POST "${api}/library/manual-import" "${auth[@]}" -d "$req")"
    local dest; dest="$(printf '%s' "$resp" | "$PY" -c \
        "import json,sys;d=json.load(sys.stdin);r=(d.get('results') or [{}])[0];print(r.get('destinationPath') or '')" 2>/dev/null)"
    if [ -z "$dest" ]; then
        fail "${casename}: the import reported no destination path"
        printf '%s\n' "$resp" | head -c 600; echo
        return 2
    fi

    # Take the destination folder from the API's own answer rather than recomputing the naming pattern.
    # Recomputing it is how a companion check ends up inspecting an empty directory and calling it a bug.
    local hdestdir; hdestdir="$(dirname "${base}/lib${dest#/data/lib}")"
    log "  destination folder: ${hdestdir#$base}"

    local waited=0
    while [ "$waited" -lt "$SETTLE" ]; do
        [ -n "$(find "$hdestdir" -type f -name '*.m4b' -print -quit 2>/dev/null)" ] && break
        sleep 3; waited=$((waited + 3))
    done
    sleep 3   # let the companion pass and any generated metadata settle before looking
    log "  waited ${waited}s for the audio to land"

    # Corroborate with the server's own account of the pass, the way Bug 16's check leaned on the
    # `Blocked` line. A filesystem observation plus the log line that explains it is a different claim
    # from a filesystem observation alone.
    local logs; logs="$("$RUNTIME" logs "$container" 2>&1)"
    local n_failed n_boundary passline
    n_failed="$(printf '%s' "$logs"   | grep -cF 'Failed to import companion file' || true)"
    n_boundary="$(printf '%s' "$logs" | grep -cF 'The requested directory boundary is not a configured root folder' || true)"
    passline="$(printf '%s' "$logs"   | grep -F 'companion-file pass completed' | tail -1 || true)"

    # Print the first refusal in full. The frames name which call site passed the boundary that was
    # rejected, which is the difference between reporting a symptom and reporting a cause.
    if [ "$n_failed" -gt 0 ]; then
        log "  first companion failure as the server recorded it:"
        printf '%s' "$logs" | grep -A 12 -F 'Failed to import companion file' | head -14 | sed 's/^/      /'
    fi

    CASE="$casename" DESTDIR="$hdestdir" SENTINEL="$sentinel" \
    NFAILED="$n_failed" NBOUNDARY="$n_boundary" PASSLINE="$passline" REPORT="$REPORT" "$PY" <<'PY'
import json, os, pathlib, sys

destdir = pathlib.Path(os.environ["DESTDIR"])
sentinel = os.environ["SENTINEL"]
case = os.environ["CASE"]

def state(name):
    """present-with-sentinel (ours), present-without (someone else wrote it), or absent."""
    path = destdir / name
    if not path.exists():
        return "absent"
    try:
        return "ours" if sentinel in path.read_text(errors="replace") else "foreign"
    except OSError:
        return "unreadable"

audio = sorted(p.name for p in destdir.glob("*.m4b")) if destdir.is_dir() else []
companions = {name: state(name) for name in
              ("book.nfo", "book.opf", "reader-notes.txt", "metadata.json", "decoy.bak")}

print(f"===== case {case} =====")
print(f"  destination contents:            {sorted(p.name for p in destdir.iterdir()) if destdir.is_dir() else '<no such folder>'}")
for name, value in companions.items():
    print(f"  {name:<18} at destination: {value}")
print(f"  'Failed to import companion file' log lines:   {os.environ['NFAILED']}")
print(f"  'boundary is not a configured root folder':    {os.environ['NBOUNDARY']}")
print(f"  server's own summary: {os.environ['PASSLINE'].strip() or '<not logged>'}")

# --- controls -------------------------------------------------------------------------
if not audio:
    print("\n  CONTROL FAILED (must arrive): no audio file at the destination the API named, so the "
          "import did not run or this is the wrong folder. Nothing below can be judged.")
    verdict = "inconclusive"
elif companions["decoy.bak"] != "absent":
    print("\n  CONTROL FAILED (must not arrive): decoy.bak is blacklisted and still reached the "
          "destination, so this inspection is not discriminating between files.")
    verdict = "inconclusive"
else:
    print(f"\n  CONTROL OK (must arrive):     audio present: {audio}")
    print("  CONTROL OK (must not arrive): blacklisted decoy.bak did not arrive")

    # Existence is not provenance. "Is there a metadata.json at the destination?" is the obvious way
    # to test this, and it would answer the wrong question on any build that generates one. Canary
    # does not, so both verdicts are computed and printed and the run says which case it landed in,
    # rather than the tool asserting a masking effect it did not observe.
    naive_pass = (destdir / "metadata.json").exists()
    real_pass = companions["metadata.json"] == "ours"
    print(f"  naive check (metadata.json exists at all):     {'PASS' if naive_pass else 'FAIL'}")
    print(f"  provenance check (it is the one we wrote):     {'PASS' if real_pass else 'FAIL'}")
    if naive_pass and not real_pass:
        print("  CONTROL FIRED (supposed to fail): the naive check passes on a build where the "
              "companion pass did nothing. Only the sentinel tells them apart.")
    elif naive_pass and real_pass:
        print("  NOTE: the companion metadata.json came across, so the masking control is moot here.")
    else:
        print("  NOTE: no metadata.json was generated at the destination at all, so the masking this "
              "control exists to expose did not occur on this run.")

    # .nfo and .opf carry the verdict. Listenarr never writes either, so their absence cannot be
    # explained away by a regenerated replacement and their presence cannot be faked.
    dropped = [n for n in ("book.nfo", "book.opf", "reader-notes.txt") if companions[n] != "ours"]
    if dropped:
        print(f"\nREPRODUCED: the audio imported and the API reported success, but {', '.join(dropped)} "
              "never reached the destination. These are file types Listenarr does not generate, so "
              "nothing masks the loss.")
        verdict = "reproduced"
    else:
        print("\nPASS: the companion files came across with the audio.")
        verdict = "pass"

with open(os.environ["REPORT"], "a", encoding="utf-8") as handle:
    handle.write(json.dumps({
        "case": case, "verdict": verdict, "companions": companions,
        "failed_lines": int(os.environ["NFAILED"]), "boundary_lines": int(os.environ["NBOUNDARY"]),
    }) + "\n")

sys.exit({"pass": 0, "reproduced": 1}.get(verdict, 2))
PY
    local rc=$?
    "$RUNTIME" rm -f "$container" >/dev/null 2>&1 || true
    return $rc
}

RESULT=0
for case_name in $CASES; do
    run_case "$case_name"; rc=$?
    [ "$rc" -gt "$RESULT" ] && RESULT=$rc
    PORT=$((PORT + 1))
    echo
done

echo "===== SUMMARY ====="
REPORT="$REPORT" "$PY" <<'PY'
import json, os

rows = [json.loads(line) for line in open(os.environ["REPORT"], encoding="utf-8") if line.strip()]
for row in rows:
    print(f"  {row['case']:<8} {row['verdict']:<13} "
          f"companion-failure log lines: {row['failed_lines']}, boundary refusals: {row['boundary_lines']}")

# The scope question the symptom alone cannot answer. If the source placement changes nothing, the
# refusal is about the destination boundary and has nothing to do with where the files came from.
verdicts = {row["case"]: row["verdict"] for row in rows}
if len(verdicts) == 2 and verdicts.get("outside") == verdicts.get("inside"):
    print(f"\n  SCOPE: identical outcome ({verdicts['outside']}) whether the source folder is outside "
          "every root folder or inside one, so the source's placement is not what decides this.")
elif len(verdicts) == 2:
    print(f"\n  SCOPE: the outcome DIFFERS by source placement — outside: {verdicts.get('outside')}, "
          f"inside: {verdicts.get('inside')}.")
PY

if [ "$RESULT" -eq 0 ]; then
    log "VALIDATION PASSED: companion files survived the manual import."
elif [ "$RESULT" -eq 1 ]; then
    fail "companion files were dropped — see the cases above."
else
    fail "no verdict: a control did not hold."
fi
exit "$RESULT"
