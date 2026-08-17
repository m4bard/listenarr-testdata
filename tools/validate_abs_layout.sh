#!/usr/bin/env bash
#
# validate_abs_layout.sh — does a Listenarr-shaped library survive Audiobookshelf's own parser?
#
# Listenarr can already write the layout Audiobookshelf reads best without any code change,
# because `{Asin}` is an existing naming token. That makes "point Listenarr at Audiobookshelf" a
# documentation task rather than a feature. Documentation nobody checks rots, and both ways this
# arrangement fails are silent:
#
#   * an ASIN that is not exactly ten uppercase alphanumerics is not ignored, it stays glued to the
#     title, so the book ends up titled `Some Book [b0015t963c]` and nothing errors
#   * a non-numeric series position does the same and loses the sequence, which is ordinary data
#     rather than a mistake anyone made
#
# So this generates real libraries in the recommended shapes and runs Audiobookshelf's REAL parser
# over them, comparing against the manifest. No regex is reimplemented here; when Audiobookshelf
# changes its rules this check changes its answer instead of agreeing with a stale copy.
#
# Three cases, and the control is the point. `series` and `flat` are the recommendation and must
# pass. `control` is Listenarr's own default layout, which carries no ASIN, and it MUST FAIL: a
# conformance check that passes everything is indistinguishable from one that checks nothing.
#
#   ./tools/validate_abs_layout.sh --abs-repo ../audiobookshelf
#
# Exit 0 the recommended shapes round-trip and the control failed as it should, 1 a recommended
# shape did not survive, 2 the run could not be judged.
#
set -uo pipefail
unset TMOUT

ABS_REPO=""
SEED=1
LIMIT=8
JSON_DIR=""
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${ROOT}/.venv/bin/python"
FFMPEG_SOURCE="system"

usage() {
    cat <<EOF
validate_abs_layout.sh — check a generated library against Audiobookshelf's own parser.

  --abs-repo PATH  an Audiobookshelf checkout (REQUIRED)
                     git clone --depth 1 https://github.com/advplyr/audiobookshelf
  --seed N         generator seed (default: ${SEED})
  --limit N        corpus books to generate (default: ${LIMIT})
  --json DIR       write <case>.json into DIR
  --ffmpeg-source jellyfin|johnvansickle|system  (default: ${FFMPEG_SOURCE})
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --abs-repo) ABS_REPO="$2"; shift 2 ;;
        --seed)     SEED="$2";     shift 2 ;;
        --limit)    LIMIT="$2";    shift 2 ;;
        --json)     JSON_DIR="$2"; shift 2 ;;
        --ffmpeg-source) FFMPEG_SOURCE="$2"; shift 2 ;;
        -h|--help)  usage; exit 0 ;;
        *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done

log() { printf '%s [abs] %s\n' "$(date +%H:%M:%S)" "$*"; }
die() { printf '%s [abs] ERROR: %s\n' "$(date +%H:%M:%S)" "$*" >&2; exit 2; }

[ -n "$ABS_REPO" ] || { usage >&2; die "--abs-repo is required"; }
[ -f "${ABS_REPO}/server/utils/scandir.js" ] \
    || die "${ABS_REPO} does not look like an Audiobookshelf checkout"
[ -x "$PY" ] || die "no venv — python3 -m venv .venv && .venv/bin/pip install -e ."
command -v node >/dev/null 2>&1 || die "node is required to run Audiobookshelf's parser"
[ -n "$JSON_DIR" ] && mkdir -p "$JSON_DIR"

# case  -> layout, and whether it is expected to survive
run_case() {
    local case_name="$1" layout="$2" must_pass="$3" ignore="${4:-}"
    local out="${ROOT}/build/abs-${case_name}"

    log "generating '${layout}' for case '${case_name}'"
    rm -rf "$out"
    "$PY" "${ROOT}/tools/generate_library.py" \
        --scenario tag-fallback-rescue --layout "$layout" \
        --out "$out" --seed "$SEED" --limit "$LIMIT" --force \
        --ffmpeg-source "$FFMPEG_SOURCE" >/dev/null 2>&1 \
        || { log "[${case_name}] generation failed"; return 2; }

    "$PY" "${ROOT}/tools/abs_conformance.py" \
        --manifest "${out}/manifest.json" --abs-repo "$ABS_REPO" \
        --label "${case_name} (${layout})" \
        ${ignore:+--ignore "$ignore"} \
        ${JSON_DIR:+--json "${JSON_DIR}/${case_name}.json"}
    local rc=$?

    if [ "$must_pass" = "yes" ] && [ "$rc" -ne 0 ]; then
        log "[${case_name}] a recommended shape did not survive"
        return 1
    fi
    if [ "$must_pass" = "no" ] && [ "$rc" -eq 0 ]; then
        log "[${case_name}] THE CONTROL PASSED. It carries no ASIN, so it cannot round-trip."
        log "            Something is wrong with the check itself; ignore the other results."
        return 2
    fi
    return 0
}

OVERALL=0
echo
run_case series  audiobookshelf-asin      yes || OVERALL=$?
echo
run_case flat    audiobookshelf-asin-flat yes "series,sequence" || { rc=$?; [ "$OVERALL" -eq 0 ] && OVERALL=$rc; }
echo
run_case control author-series-title      no  || { rc=$?; [ "$OVERALL" -eq 0 ] && OVERALL=$rc; }
echo

if [ "$OVERALL" -eq 0 ]; then
    log "recommended shapes round-tripped; the control failed as it must"
fi
exit "$OVERALL"
