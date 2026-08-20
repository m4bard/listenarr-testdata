#!/usr/bin/env bash
#
# regression_sweep.sh — run a set of runtime checks against one image and tabulate the verdicts.
#
# NOT every check in tools/. The set below is the one that has been run end to end against this
# harness; the others are excluded for stated reasons rather than forgotten, because a sweep that
# quietly covers less than its name suggests is worse than one that covers little and says so.
#
#   check_duplicate_detection.sh   reports, it does not judge: what counts as a duplicate is a
#                                  product decision, so it has no pass/fail to tabulate.
#   validate_abs_layout.sh         needs an Audiobookshelf checkout to run its parser against.
#   not yet wired in               validate_import_destination, validate_rename_hazards,
#                                  validate_root_folder_delete, validate_import_relisting,
#                                  validate_author_cache_variants. All take --image and --port, so
#                                  adding them is mechanical; none has been run from here yet, and
#                                  listing them keeps that a visible gap rather than an invisible one.
#
# The point is to answer "what did this release change?" without touching anything real. Each check
# below generates its own synthetic library, provisions its own throwaway config, starts its own
# container on its own port, and deletes it afterwards. Nothing outside build/ and a few mktemp
# directories is read or written, so the answer costs nothing but time and is repeatable by anyone
# with the repo.
#
# Exit codes are per-check and deliberately not uniform across tools, so they are reported rather
# than summed: 0 pass, 1 the behaviour under test is wrong, 2 the run could not be judged. A 2 is
# not a pass, and treating it as one is how a sweep starts lying.
#
#   ./tools/regression_sweep.sh --image ghcr.io/listenarrs/listenarr:canary
#
set -uo pipefail
unset TMOUT

IMAGE="ghcr.io/listenarrs/listenarr:canary"
BASE_PORT=4800
PER_CHECK_TIMEOUT=900     # a hung check must not stall the sweep
ONLY=""
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

usage() {
    cat <<EOF
regression_sweep.sh — run a set of runtime checks against one image and tabulate.
                      Not every check in tools/; see the header for what is excluded and why.

  --image REF     image under test (default: ${IMAGE})
  --port N        base port; each check gets its own (default: ${BASE_PORT})
  --timeout N     per-check timeout in seconds (default: ${PER_CHECK_TIMEOUT})
  --only NAME     run a single check by name
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --image)   IMAGE="$2";             shift 2 ;;
        --port)    BASE_PORT="$2";         shift 2 ;;
        --timeout) PER_CHECK_TIMEOUT="$2"; shift 2 ;;
        --only)    ONLY="$2";              shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done

log() { printf '%s [sweep] %s\n' "$(date +%H:%M:%S)" "$*"; }

command -v podman >/dev/null 2>&1 || { echo "podman required" >&2; exit 2; }

# Stale containers from an interrupted run hold ports and make the next sweep fail for a reason
# that has nothing to do with the image.
log "clearing any leftover harness containers"
podman rm -f $(podman ps -aq --filter "name=listenarr-" --filter "name=impval-" 2>/dev/null) >/dev/null 2>&1

# name | how it is invoked. %IMAGE% and %PORT% are substituted.
CHECKS=(
  "metadata-fallback|./tools/validate_metadata_fallback.sh --image %IMAGE% --mode both --ffmpeg-source system --port %PORT%"
  "scan-attribution|./tools/validate_scan_attribution.sh --image %IMAGE% --asin B002UUFXKU --port %PORT%"
  "naming-parity|./tools/validate_naming_parity.sh --image %IMAGE% --port %PORT%"
  "reported-size|./tools/validate_reported_size.sh --image %IMAGE% --port %PORT%"
  "sidecar-rename|./tools/validate_sidecar_rename.sh %IMAGE% %PORT%"
  "import-hardlink|./tools/validate_import_action.sh %IMAGE% --action hardlink/copy --port %PORT%"
  "import-move|./tools/validate_import_action.sh %IMAGE% --action move --port %PORT%"
  "companion-import|./tools/validate_companion_import.sh --image %IMAGE% --port %PORT%"
  "asin-tag-embed|./tools/validate_asin_tag_embed.sh --image %IMAGE% --port %PORT%"
  "chapter-grouping|./tools/validate_chapter_grouping.sh --image %IMAGE% --port %PORT%"
  "queue-poll|./tools/validate_queue_poll_resilience.sh --image %IMAGE% --port %PORT%"
)

declare -A RC=()
declare -A SECS=()
port=$BASE_PORT
stamp="$(date +%F-%H%M.%S)"
mkdir -p "${ROOT}/build/sweep-${stamp}"

for entry in "${CHECKS[@]}"; do
    name="${entry%%|*}"; cmd="${entry#*|}"
    [ -n "$ONLY" ] && [ "$ONLY" != "$name" ] && { port=$((port + 4)); continue; }

    cmd="${cmd//%IMAGE%/$IMAGE}"
    cmd="${cmd//%PORT%/$port}"
    out="${ROOT}/build/sweep-${stamp}/${name}.log"

    log "running ${name} on port ${port}"
    start=$(date +%s)
    timeout "$PER_CHECK_TIMEOUT" bash -c "cd '$ROOT' && $cmd" > "$out" 2>&1
    rc=$?
    SECS[$name]=$(( $(date +%s) - start ))
    RC[$name]=$rc
    [ "$rc" -eq 124 ] && log "  ${name} TIMED OUT after ${PER_CHECK_TIMEOUT}s"
    log "  ${name} exit ${rc} in ${SECS[$name]}s -> ${out}"
    port=$((port + 4))
done

echo
echo "  image: ${IMAGE}"
echo "  logs:  build/sweep-${stamp}/"
echo
printf '  %-20s %-6s %-7s %s\n' "check" "exit" "secs" "meaning"
printf '  %-20s %-6s %-7s %s\n' "--------------------" "------" "-------" "-------"
for entry in "${CHECKS[@]}"; do
    name="${entry%%|*}"
    [ -z "${RC[$name]+x}" ] && continue
    case "${RC[$name]}" in
        0)   meaning="pass" ;;
        1)   meaning="FAIL — behaviour under test is wrong" ;;
        2)   meaning="inconclusive — could not judge" ;;
        124) meaning="TIMED OUT" ;;
        *)   meaning="unexpected exit" ;;
    esac
    printf '  %-20s %-6s %-7s %s\n' "$name" "${RC[$name]}" "${SECS[$name]}" "$meaning"
done
echo
echo "  An inconclusive result is not a pass. Read its log before drawing anything from the row."
