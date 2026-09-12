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
# So this drives real files through the manual-import API and then reads each DESTINATION file's
# tags on the host, looking for the three places the writer puts an ASIN
# (`----:com.apple.iTunes:ASIN`, a `TXXX:ASIN` frame, or a Xiph `ASIN` field).
#
# THREE CONTAINERS, NOT ONE. `TagLibAudioTagWriter.ApplyAsinTag` writes into a different tag
# system per container, and they are three code paths that fail independently: the MP4 write goes
# through TagLib.Mpeg4, the ID3 write through a TXXX frame, the Xiph write through a comment
# field. A run that only ever imports an m4b reports on a third of the writer and says nothing
# about the other two. The sources come from the tag-dialects scenario with the tag state forced
# to correct-no-asin, which is what emits the same untagged single-file book in every container
# the writer knows. Each container is a different corpus book, because the generator takes the
# container from the dialect axis and one book here is one file, so each format gets its own
# library record, its own import and its own pair of gates, and the table at the end keeps them
# apart. `--format` narrows it to one.
#
# Two gates run per format, and that format gets no verdict unless both land, because the reader
# has to be shown capable of BOTH answers on THAT container or neither answer means anything:
#
#   control   a copy of the same generated file with the ASIN stamped onto it by hand — the exact
#             tag the writer targets for that container. The reader MUST call this `tagged`. If a
#             file that demonstrably carries the tag reads as untagged, the reader is broken for
#             this container and every `untagged` verdict it has printed for it is worthless.
#   subject   the generated file as it goes in, which MUST read `untagged`. A generator that
#             embedded an ASIN of its own would make "an ASIN is present afterwards" true without
#             Listenarr having written anything, and the check would pass on a broken build.
#
# The verdict is the FILE's. Once both gates land, the source went in carrying no ASIN and the
# library record carried one, so what the destination file says afterwards is a statement about
# the writer and about nothing else.
#
# The server's own logs are read as corroboration only: the writer logs either "Wrote ASIN tag" or
# "Failed to write ASIN tag". Neither line appearing used to end the run as unjudgeable, and that
# was wrong. The wording belongs to the server and can move, and a step that logs nothing at all
# looks identical to one whose message changed, so a log that says nothing is a note about the log
# rather than a reason to withhold a verdict the file already supports. What the log can still do is
# name a cause: a "Failed to write" line brings its frames with it. The lines are counted per
# import rather than for the whole run, or the first format's failure would be read as every
# format's.
#
# The preconditions the log check used to stand in for are checked directly instead, and each one
# ends that format on its own: the book's record has to carry an ASIN (or nothing asked for a tag),
# no destination file of that container may exist before its import (or the file being read is a
# previous import's), and the destination file has to exist and be readable as audio (or there is
# nothing to judge).
#
# A pinned ffprobe is provisioned up front (tools/ffprobe_provisioner.py) so the import's own
# metadata step does not hard-fail on the first-boot download race.
#
#   ./tools/validate_asin_tag_embed.sh --image ghcr.io/listenarrs/listenarr:canary
#   ./tools/validate_asin_tag_embed.sh --image ghcr.io/listenarrs/listenarr:canary --format flac
#
# Exit 0 every requested container carried the ASIN, 1 at least one did not, 2 nothing could be
# judged. A failure outranks an unjudged container in the exit code, because the wrong behaviour is
# the more useful of the two answers; the table says which containers were which.
#
set -uo pipefail
unset TMOUT

IMAGE="ghcr.io/listenarrs/listenarr:canary"
FORMATS="m4b mp3 flac"
SCENARIO="tag-dialects"    # the one scenario that emits every container the writer knows
TAG_STATE="correct-no-asin"
STRUCTURE="single"         # one book, one file, so a format's source is unambiguous
LIMIT=6                    # the dialect axis cycles over 5 dialects; 6 books cover all of them
ASIN=""                    # restrict generation to one book; then only its container can be judged
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
  --format LIST   containers to judge: all, or any of m4b, mp3, flac, comma-separated
                  (default: all three)
  --asin ASIN     generate one book only; only its own container can then be judged
  --scenario KEY  scenario to generate the sources from (default: ${SCENARIO})
  --seed N        generator seed (default: ${SEED})
  --port N        host port (default: ${PORT})
  --settle N      seconds to wait for each destination (default: ${SETTLE})
  --label TEXT    label for the report header
  --json DIR      write control-<fmt>.json / subject-<fmt>.json / imported-<fmt>.json into DIR
  --keep          leave the container running
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --image)    IMAGE="$2";    shift 2 ;;
        --format)
            case "$2" in
                all) FORMATS="m4b mp3 flac" ;;
                *)   FORMATS="$(printf '%s' "$2" | tr ',' ' ')" ;;
            esac
            shift 2 ;;
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
for fmt in $FORMATS; do
    case "$fmt" in
        m4b|mp3|flac) ;;
        *) echo "--format takes m4b, mp3, flac or all, not '${fmt}'" >&2; exit 2 ;;
    esac
done

log()  { printf '%s [asintag] %s\n' "$(date +%H:%M:%S)" "$*"; }
die()  { printf '%s [asintag] ERROR: %s\n' "$(date +%H:%M:%S)" "$*" >&2; exit 2; }
fail() { printf '%s [asintag] FAIL: %s\n' "$(date +%H:%M:%S)" "$*"; }

# shellcheck source=tools/lib/container_runtime.sh
. "${ROOT}/tools/lib/container_runtime.sh"
cr_require || die "no usable container runtime"
cr_scrub_image "$IMAGE"
[ -x "$PY" ] || die "no venv — python3 -m venv .venv && .venv/bin/pip install -e ."

cleanup() {
    [ "$KEEP" -eq 1 ] && { log "leaving ${CONTAINER} on port ${PORT}"; return 0; }
    crun rm -f "$CONTAINER" >/dev/null 2>&1
    return 0
}
trap cleanup EXIT

jsonarg() { [ -n "$JSON_DIR" ] && printf -- '--json\n%s/%s.json' "$JSON_DIR" "$1"; }

log "image ${LABEL}"
log "containers to judge: ${FORMATS}"

# A container that ran as root in these bind mounts leaves files this account cannot unlink, so a
# plain `rm -rf` here fails quietly and the run then reads the PREVIOUS run's destination file and
# reports on it as though it were this one. cr_force_rm finishes the job through the runtime and
# says so if it cannot, and a container left over from an interrupted run is removed before it can
# hold the port.
cr_remove_stale "listenarr-asintag-"
cr_force_rm "$SRCDIR" "$LIBDIR" "$CONFIG" "${ROOT}"/build/asintag-control.* \
    || die "a previous run's files are still here; this run would measure them"
mkdir -p "$SRCDIR" "$LIBDIR" "$CONFIG"

GEN=(--scenario "$SCENARIO" --out "$SRCDIR" --seed "$SEED" --structure "$STRUCTURE" --force)
[ -n "$TAG_STATE" ] && GEN+=(--tag-state "$TAG_STATE")
if [ -n "$ASIN" ]; then GEN+=(--only-asin "$ASIN"); else GEN+=(--limit "$LIMIT"); fi
log "generating sources from '${SCENARIO}'"
"$PY" "${ROOT}/tools/generate_library.py" "${GEN[@]}" >/dev/null || die "generation failed"

# One source file per requested container, with the book it belongs to. The book comes from the
# manifest rather than from a name in this script: the container is chosen by the generator's
# dialect axis, so which book carries which container is the generator's business, and a hardcoded
# title here would silently stop matching it.
SOURCES="$("$PY" - "$SRCDIR" $FORMATS <<'PYEOF'
import json, pathlib, sys

root = pathlib.Path(sys.argv[1])
manifest = json.loads((root / "manifest.json").read_text())
entries = [e for e in manifest["entries"]
           if e.get("kind") == "book" and e.get("part", 1) == 1 and not e.get("hazard")]
def refused_for_its_path(path: str) -> bool:
    """Would manual-import refuse this source before the writer is ever reached?

    A path component ending in a dot -- an ordinary book title like `Stalky and Co.` -- is
    rejected by Listenarr's own path validation as a traversal attempt and the request
    500s. That is worth knowing and it is a different bug from this one; a source that
    trips it measures the path validator rather than the tag writer, so another book is
    preferred for that container and the swap is announced rather than made quietly.
    """
    return any(part.endswith(".") for part in pathlib.PurePosixPath(path).parts)


for ext in sys.argv[2:]:
    candidates = [e for e in entries if e["path"].lower().endswith(f".{ext}")]
    usable = [e for e in candidates if not refused_for_its_path(e["path"])]
    match = (usable or candidates or [None])[0]
    if match is None:
        print(f"{ext}\tMISSING")
        continue
    if not usable:
        print(f"asin-tag: every .{ext} source has a path component ending in a dot, which "
              f"manual-import refuses, so {match['true_title']!r} will probably be refused too",
              file=sys.stderr)
    elif len(usable) < len(candidates):
        skipped = ", ".join(e["true_title"] for e in candidates if e not in usable)
        print(f"asin-tag: {ext}: skipped {skipped} (a path component ends in a dot, which "
              f"manual-import refuses as a traversal attempt) in favour of {match['true_title']!r}",
              file=sys.stderr)
    print("\t".join([ext, match["path"], match["belongs_to_asin"], match["true_title"],
                     (match["true_authors"] or ["Unknown"])[0], match.get("dialect") or "?"]))
PYEOF
)" || die "could not read the generator's manifest"

# --- The instance ----------------------------------------------------------------------
"$PY" "${ROOT}/tools/ffprobe_provisioner.py" --config-dir "$CONFIG" >/dev/null \
    || die "could not provision ffprobe"
FFPROBE="${CONFIG}/ffmpeg/ffprobe"

crun rm -f "$CONTAINER" >/dev/null 2>&1
crun run -d --name "$CONTAINER" -p "${PORT}:4545" -e LISTENARR_LOG_LEVEL=Debug \
    "${CR_OWNERSHIP_ARGS[@]}" \
    -v "${SRCDIR}:/src" -v "${LIBDIR}:/audiobooks" -v "${CONFIG}:/app/config" \
    "$IMAGE" >/dev/null || die "could not start ${IMAGE}"

API="http://localhost:${PORT}/api/v1"
for _ in $(seq 1 120); do curl -fsS "${API}/system/status" >/dev/null 2>&1 && break; sleep 2; done
curl -fsS "${API}/system/status" >/dev/null 2>&1 || {
    crun logs "$CONTAINER" 2>&1 | tail -15; die "API never came up"; }

KEY=$("$PY" -c "import json; print(json.load(open('${CONFIG}/config.json'))['ApiKey'])") || die "no ApiKey"
AUTH=(-H "X-Api-Key: ${KEY}" -H 'Content-Type: application/json')

curl -fsS -X POST "${API}/rootfolders" "${AUTH[@]}" \
    -d '{"name":"lib","path":"/audiobooks","isDefault":true,"caseSensitivityMode":"Sensitive"}' \
    >/dev/null || die "could not create the root folder"

# Count a log line for this import only. Counting over the whole run would read the first
# container's failure as every container's.
logcount() { printf '%s' "$1" | grep -c "$2"; }

# Judge one container. 0 the ASIN was embedded, 1 it was not, 2 this container could not be judged.
# Args: <ext> <relative source path> <asin> <title> <author> <dialect>
judge_format() {
    local fmt="$1" rel="$2" asin="$3" title="$4" author="$5" dialect="$6"
    local src="${SRCDIR}/${rel}" control="${ROOT}/build/asintag-control.${fmt}"

    echo
    log "=== ${fmt} (${dialect}) — ${title} / ${asin}"
    [ -f "$src" ] || { fail "${fmt}: the manifest names a source the generator did not write"; return 2; }
    log "  source: ${src#"$ROOT"/}"

    # --- Gate 1: the reader can see an ASIN that IS there in THIS container -------------
    cp "$src" "$control"
    "$PY" "${ROOT}/tools/asin_tag_probe.py" stamp "$control" --asin "$asin" >/dev/null \
        || { fail "${fmt}: could not stamp the control file"; return 2; }
    if ! "$PY" "${ROOT}/tools/asin_tag_probe.py" read "$control" --label "control ${fmt}" \
            --expect-asin "$asin" --ffprobe "$FFPROBE" $(jsonarg "control-${fmt}"); then
        fail "${fmt}: THE CONTROL READ AS UNTAGGED. It carries the exact tag the writer targets"
        fail "        for this container, so the reader is broken for it; ignore its verdict."
        return 2
    fi

    # --- Gate 2: the reader can see that an ASIN is NOT there ---------------------------
    if "$PY" "${ROOT}/tools/asin_tag_probe.py" read "$src" --label "subject ${fmt}" \
            --ffprobe "$FFPROBE" $(jsonarg "subject-${fmt}"); then
        fail "${fmt}: THE SOURCE FILE ALREADY CARRIES AN ASIN. An ASIN found after the import"
        fail "        would prove nothing about who wrote it. Generate from a tag state of"
        fail "        correct-no-asin, or pick another book for this container."
        return 2
    fi

    # --- The book ----------------------------------------------------------------------
    local book_id stored
    book_id=$(curl -s -X POST "${API}/library/add" "${AUTH[@]}" \
        -d "$("$PY" - "$asin" "$title" "$author" <<'REQEOF'
import json, sys
asin, title, author = sys.argv[1:4]
print(json.dumps({"metadata": {"asin": asin, "title": title, "authors": [author]},
                  "monitored": True, "autoSearch": False}))
REQEOF
)" | "$PY" -c "import json,sys; d=json.load(sys.stdin); print(d.get('id') or (d.get('audiobook') or {}).get('id') or '')")
    [ -n "$book_id" ] || { fail "${fmt}: could not add ${asin}"; return 2; }

    # The book has to carry the ASIN in the database or the writer is never called at all — the
    # controller only enriches when audiobook.Asin is non-blank. Checking it here keeps "no tag was
    # written" from being read as a bug when the real answer is that nothing asked for one.
    stored=$(curl -fsS "${API}/library/${book_id}" "${AUTH[@]}" \
        | "$PY" -c "import json,sys; d=json.load(sys.stdin); print((d.get('audiobook') or d).get('asin') or '')" 2>/dev/null)
    [ -n "$stored" ] || { fail "${fmt}: the added book carries no ASIN, so the writer is never called"; return 2; }
    log "  audiobook ${book_id}, library record carries ASIN ${stored}"

    # A destination of this container must not exist yet. A stale one reads exactly like a freshly
    # imported file, and the tag on it would be some other import's answer.
    [ -z "$(find "$LIBDIR" -type f -name "*.${fmt}" -print -quit 2>/dev/null)" ] || {
        fail "${fmt}: a .${fmt} destination already exists before this import, so nothing found"
        fail "        afterwards could be attributed to it"
        return 2
    }

    # --- The import --------------------------------------------------------------------
    local before_wrote before_failed logs req
    logs="$(crun logs "$CONTAINER" 2>&1)"
    before_wrote=$(logcount "$logs" "Wrote ASIN tag")
    before_failed=$(logcount "$logs" "Failed to write ASIN tag")

    req=$("$PY" - "/src/${rel}" "$book_id" <<'REQEOF'
import json, os, sys
full, aid = sys.argv[1], int(sys.argv[2])
print(json.dumps({"path": os.path.dirname(full), "action": "hardlink/copy", "items": [
    {"relativePath": os.path.basename(full), "fullPath": full, "matchedAudiobookId": aid}]}))
REQEOF
)
    log "  importing"
    local resp reported
    resp="$(curl -s -X POST "${API}/library/manual-import" "${AUTH[@]}" -d "$req")"
    # The response arrives as an argument, not on stdin: `$PY -` already reads the program
    # from stdin, so a pipe into it feeds the parser its own source and it sees nothing.
    reported="$("$PY" - "$resp" <<'RESPEOF'
import json, sys
try:
    payload = json.loads(sys.argv[1])
except Exception:
    sys.exit(0)
results = payload.get("results") if isinstance(payload, dict) else None
print((((results or [{}])[0]) or {}).get("destinationPath") or "")
RESPEOF
)"
    # An import the API refused used to look exactly like one that ran and wrote nothing: the
    # tool waited out the whole settle window and then blamed the writer for a file that was
    # never going to appear. Ask the response first.
    if [ -z "$reported" ]; then
        fail "${fmt}: the import was refused, so the writer never ran and nothing here is about"
        fail "        it. The API answered:"
        printf '%s' "$resp" | head -c 400 | sed 's/^/      /'; echo
        crun logs "$CONTAINER" 2>&1 | grep -iE "exception|not allowed" | tail -3 | sed 's/^/      /'
        return 2
    fi

    local waited=0 dest
    while [ "$waited" -lt "$SETTLE" ]; do
        [ -n "$(find "$LIBDIR" -type f -name "*.${fmt}" -print -quit 2>/dev/null)" ] && break
        sleep 3; waited=$((waited + 3))
    done
    sleep 3   # let the enrichment step run after the file appears
    dest="$(find "$LIBDIR" -type f -name "*.${fmt}" | head -1)"
    if [ -z "$dest" ]; then
        crun logs "$CONTAINER" 2>&1 | tail -20
        fail "${fmt}: no destination after ${waited}s — the import never completed, so there is"
        fail "        nothing to judge for this container"
        return 2
    fi
    log "  destination after ${waited}s: ${dest#"$ROOT"/}"

    # --- The verdict: read the file ----------------------------------------------------
    "$PY" "${ROOT}/tools/asin_tag_probe.py" read "$dest" --label "imported ${fmt}" \
        --expect-asin "$asin" --ffprobe "$FFPROBE" $(jsonarg "imported-${fmt}")
    local verdict=$?
    if [ "$verdict" -eq 2 ]; then
        fail "${fmt}: the destination could not be read as tagged audio at all, so there is no"
        fail "        file evidence either way. That is a broken destination, not a statement"
        fail "        about the tag writer."
        return 2
    fi

    # --- Corroboration: what the server said about this import -------------------------
    local wrote failed
    logs="$(crun logs "$CONTAINER" 2>&1)"
    wrote=$(( $(logcount "$logs" "Wrote ASIN tag") - before_wrote ))
    failed=$(( $(logcount "$logs" "Failed to write ASIN tag") - before_failed ))
    if [ "$wrote" -gt 0 ] || [ "$failed" -gt 0 ]; then
        log "  corroboration: ${wrote} 'Wrote ASIN tag', ${failed} 'Failed to write ASIN tag'"
    else
        log "  NOTE: the server logged neither 'Wrote ASIN tag' nor 'Failed to write ASIN tag' for"
        log "        this import, so the log corroborates nothing here. The writer's wording is the"
        log "        server's to change, and a step that logs nothing looks the same as one whose"
        log "        message moved, so this does not withhold the verdict. The verdict is the file's."
    fi
    if [ "$failed" -gt 0 ]; then
        printf '%s' "$logs" | grep -A12 "Failed to write ASIN tag" | tail -14 | sed 's/^/      /'
    fi

    if [ "$verdict" -eq 0 ]; then
        log "  PASSED (${fmt}): the imported file carries ${asin} in its own tags."
        log "        The control read tagged and the source read untagged on this container in the"
        log "        same run, so the reader was shown capable of both answers for it."
        [ "$wrote" -eq 0 ] && [ "$failed" -eq 0 ] && \
            log "        The file carries the tag whatever the log does or does not say about it."
    else
        fail "${fmt}: the imported file carries no ASIN of its own."
        fail "        The reader called the stamped control 'tagged' moments earlier, on a copy of"
        fail "        this same file, so the tag is genuinely absent rather than unreadable."
        fail "        The import itself reported success and the file is intact; only the"
        fail "        enrichment step silently did nothing."
        if [ "$failed" -gt 0 ]; then
            fail "        The server recorded the failure itself, above."
        elif [ "$wrote" -gt 0 ]; then
            fail "        The server logged a successful write and the file disagrees with it."
        else
            fail "        The server logged nothing either way. The library record carried an ASIN,"
            fail "        no .${fmt} destination existed before the import, and the file that"
            fail "        appeared is readable, so 'nothing asked for a tag' and 'the import never"
            fail "        ran' are already ruled out without the log's help."
        fi
    fi
    return "$verdict"
}

declare -A RESULT=()
while IFS=$'\t' read -r fmt rel asin title author dialect; do
    [ -n "$fmt" ] || continue
    if [ "$rel" = "MISSING" ]; then
        echo
        fail "${fmt}: the generator produced no .${fmt} source. '${SCENARIO}' emits one container"
        fail "        per book from its dialect axis, so a --asin that names a single book can only"
        fail "        ever cover that book's container. Narrow --format, or drop --asin."
        RESULT["$fmt"]=2
        continue
    fi
    judge_format "$fmt" "$rel" "$asin" "$title" "$author" "$dialect"
    RESULT["$fmt"]=$?
done <<< "$SOURCES"

echo
echo "===== SUMMARY ====="
OVERALL=0
for fmt in $FORMATS; do
    case "${RESULT[$fmt]:-2}" in
        0) printf '  %-5s embedded\n'   "$fmt" ;;
        1) printf '  %-5s NOT embedded\n' "$fmt" ;;
        *) printf '  %-5s no verdict\n'  "$fmt" ;;
    esac
done
for fmt in $FORMATS; do [ "${RESULT[$fmt]:-2}" -eq 2 ] && OVERALL=2; done
for fmt in $FORMATS; do [ "${RESULT[$fmt]:-2}" -eq 1 ] && OVERALL=1; done

echo
if [ "$OVERALL" -eq 0 ]; then
    log "PASSED: every container judged carried the ASIN."
elif [ "$OVERALL" -eq 1 ]; then
    fail "at least one container did not carry the ASIN. A container that failed is reported as a"
    fail "        failure even when another could not be judged, because the wrong behaviour is the"
    fail "        more useful answer; the table above says which was which."
else
    fail "no container could be judged."
fi
exit "$OVERALL"
