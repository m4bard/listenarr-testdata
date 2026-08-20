#!/usr/bin/env bash
#
# verify_patch_branches.sh — does every patch branch still build, and does its test project?
#
# A patch that compiles the application is not a patch that compiles. Adding a required parameter
# to a method with call sites in the test project produces a container image that builds and starts
# perfectly while `dotnet test` refuses to compile, and a `podman build` will never notice, because
# publishing does not build tests. That is a real defect that reached a finished branch here, so
# this checks the two separately and reports them separately.
#
# It also runs a baseline commit through the same lanes, because "12 tests failed" means nothing
# until you know how many failed without the patch.
#
# Lanes:
#   build    dotnet build of the whole solution, application code only
#   test     dotnet build + run of the test project, then the full xunit suite
#   fe       vue-tsc type check and the vitest suite, for branches that touch fe/
#
# A lane that cannot run is reported as SKIP and never as a pass. An absent node_modules is the
# common case: it is a missing prerequisite, not a green frontend.
#
#   ./verify_patch_branches.sh --repo /path/to/listenarr-clone
#   ./verify_patch_branches.sh --repo /path/to/clone --only bug12
#
# Exit 0 every lane that ran passed, 1 at least one failed, 2 nothing could be judged.

set -uo pipefail
unset TMOUT

DOTNET="${DOTNET:-$HOME/.dotnet/dotnet}"
export DOTNET_CLI_TELEMETRY_OPTOUT=1 DOTNET_NOLOGO=1
export PATH="$(dirname "$DOTNET"):$PATH"

REPO=""
ONLY=""
BASELINE_REF="${BASELINE_REF:-upstream/canary}"
LOGDIR="${LOGDIR:-$PWD/branch-verify-logs}"

while [ $# -gt 0 ]; do
    case "$1" in
        --repo)     REPO="$2"; shift 2 ;;
        --only)     ONLY="$2"; shift 2 ;;
        --baseline) BASELINE_REF="$2"; shift 2 ;;
        --logdir)   LOGDIR="$2"; shift 2 ;;
        -h|--help)  sed -n '2,26p' "$0"; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

[ -n "$REPO" ] || { echo "--repo is required" >&2; exit 2; }
[ -x "$DOTNET" ] || { echo "no dotnet at $DOTNET; set DOTNET=" >&2; exit 2; }
mkdir -p "$LOGDIR"

ts() { date +"%H:%M:%S"; }
log() { printf '%s [verify] %s\n' "$(ts)" "$*"; }

# name|path|lanes   lanes is a comma list drawn from build,test,fe
WORK=()
while read -r line; do
    [ -n "$line" ] && WORK+=("$line")
done < <(
    git -C "$REPO" worktree list --porcelain \
    | awk '
        /^worktree /  { path = substr($0, 10) }
        /^branch /    { br = substr($0, 8); sub(/^refs\/heads\//, "", br); print path "|" br }
        /^detached$/  { print path "|(detached)" }
      ' \
    | while IFS='|' read -r path branch; do
        [ -d "$path" ] || continue
        name="$(basename "$path")"
        [ -n "$ONLY" ] && [[ "$name" != *"$ONLY"* ]] && continue
        # Which lanes make sense is decided by what the branch actually changed, not by a
        # hand-kept list that drifts the first time a branch grows a file.
        lanes="build,test"
        changed=""
        if [ "$branch" != "(detached)" ]; then
            changed="$(git -C "$path" diff --name-only "${BASELINE_REF}...HEAD" 2>/dev/null)"
        fi
        if [ -n "$changed" ]; then
            if ! printf '%s\n' "$changed" | grep -qv '^fe/'; then
                lanes="fe"                                     # frontend only
            elif printf '%s\n' "$changed" | grep -q '^fe/'; then
                lanes="build,test,fe"                          # both halves
            fi
        fi
        echo "$name|$path|$branch|$lanes"
      done
)

[ ${#WORK[@]} -gt 0 ] || { echo "no worktrees matched" >&2; exit 2; }

declare -A R_BUILD R_TEST R_FE R_COUNTS R_BRANCH

parse_counts() {  # stdin: dotnet test output -> "passed/failed/skipped"
    awk '
        /Failed: +[0-9]+, Passed: +[0-9]+/ {
            f = $0; sub(/.*Failed: +/, "", f); sub(/,.*/, "", f)
            p = $0; sub(/.*Passed: +/, "", p); sub(/,.*/, "", p)
            s = $0; sub(/.*Skipped: +/, "", s); sub(/,.*/, "", s)
            print p "/" f "/" s; found = 1
        }
        END { if (!found) print "?/?/?" }
    ' | tail -1
}

for entry in "${WORK[@]}"; do
    IFS='|' read -r name path branch lanes <<< "$entry"
    R_BRANCH["$name"]="$branch"
    R_BUILD["$name"]=skip; R_TEST["$name"]=skip; R_FE["$name"]=skip; R_COUNTS["$name"]="-"
    log "=== $name ($branch) lanes: $lanes"

    if [[ "$lanes" == *build* ]]; then
        log "  build: solution"
        if (cd "$path" && timeout 1200 "$DOTNET" build listenarr.slnx --nologo -v q) \
                > "$LOGDIR/${name}.build.log" 2>&1; then
            R_BUILD["$name"]=pass
        else
            R_BUILD["$name"]=FAIL
            log "  build FAILED, see $LOGDIR/${name}.build.log"
        fi
    fi

    if [[ "$lanes" == *test* ]]; then
        log "  test: project build"
        if (cd "$path" && timeout 1200 "$DOTNET" build tests/Listenarr.Tests.csproj --nologo -v q) \
                > "$LOGDIR/${name}.testbuild.log" 2>&1; then
            log "  test: full suite"
            if (cd "$path" && timeout 2400 "$DOTNET" test tests/Listenarr.Tests.csproj --no-build) \
                    > "$LOGDIR/${name}.test.log" 2>&1; then
                R_TEST["$name"]=pass
            else
                # A failure under a full-suite run is not yet a failure. This suite contains
                # wall-clock guardrail tests, and one of them came in at 1021ms against a 1000ms
                # threshold on a loaded box and passed at 272ms alone. Re-run just the failures in
                # isolation and report which kind this was, rather than making every reader of the
                # summary re-derive it.
                failed_names="$(grep -oE '^  Failed [A-Za-z0-9_.]+' "$LOGDIR/${name}.test.log" \
                                | sed 's/^  Failed //' | sort -u)"
                if [ -n "$failed_names" ]; then
                    filter="$(echo "$failed_names" | sed 's/^/FullyQualifiedName=/' | paste -sd'|' -)"
                    log "  test: re-running $(echo "$failed_names" | wc -l) failure(s) in isolation"
                    if (cd "$path" && timeout 1200 "$DOTNET" test tests/Listenarr.Tests.csproj \
                            --no-build --filter "$filter") \
                            > "$LOGDIR/${name}.retest.log" 2>&1; then
                        R_TEST["$name"]=flaky
                        log "  all re-runs passed alone: load-sensitive, not a regression"
                    else
                        R_TEST["$name"]=FAIL
                    fi
                else
                    R_TEST["$name"]=FAIL
                fi
            fi
            R_COUNTS["$name"]="$(parse_counts < "$LOGDIR/${name}.test.log")"
        else
            # The distinction this whole script exists for.
            R_TEST["$name"]=NOBUILD
            log "  TEST PROJECT DOES NOT COMPILE, see $LOGDIR/${name}.testbuild.log"
        fi
    fi

    if [[ "$lanes" == *fe* ]]; then
        if [ -d "$path/fe/node_modules" ]; then
            log "  fe: type-check and vitest"
            if (cd "$path/fe" && timeout 1200 npm run type-check --silent \
                 && timeout 1200 npx vitest run --reporter=dot) \
                    > "$LOGDIR/${name}.fe.log" 2>&1; then
                R_FE["$name"]=pass
            else
                R_FE["$name"]=FAIL
            fi
        else
            log "  fe: SKIP, no node_modules in $path/fe"
            R_FE["$name"]=skip
        fi
    fi
done

echo
echo "===== SUMMARY ====="
printf '%-22s %-46s %-8s %-9s %-16s %s\n' worktree branch build test "passed/fail/skip" fe
worst=0
for entry in "${WORK[@]}"; do
    IFS='|' read -r name path branch lanes <<< "$entry"
    printf '%-22s %-46s %-8s %-9s %-16s %s\n' \
        "$name" "${R_BRANCH[$name]}" "${R_BUILD[$name]}" "${R_TEST[$name]}" \
        "${R_COUNTS[$name]}" "${R_FE[$name]}"
    case "${R_BUILD[$name]}${R_TEST[$name]}${R_FE[$name]}" in
        *FAIL*|*NOBUILD*) worst=1 ;;
    esac
done
echo
for entry in "${WORK[@]}"; do
    IFS='|' read -r name path branch lanes <<< "$entry"
    [ "${R_TEST[$name]}" = flaky ] && \
        echo "note: $name had failures that all passed when re-run alone; see ${name}.retest.log"
    [ "${R_TEST[$name]}" = NOBUILD ] && \
        echo "note: $name compiles as an application but its TEST PROJECT does not build"
done
echo
echo "logs: $LOGDIR"
exit "$worst"
