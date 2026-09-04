#!/usr/bin/env bash
#
# ci_status.sh — is the gate red on the branch that work actually lands on?
#
# The question this answers is deliberately narrow. A repository's run list mixes scheduled
# jobs, pull-request runs and deploy hooks together, and the scheduled ones here are the most
# reliably green things in it, so the newest few rows are almost always successes. Reading the
# list at a glance therefore says "healthy" while every push has been failing. That is not a
# hypothetical: pushes to this repository's own default branch failed eighteen times in a row
# between 2026-08-07 and 2026-08-28 with weekly drift jobs passing green above them the whole
# time, and nobody noticed for three and a half weeks.
#
# So this looks at PUSH-triggered runs on ONE named branch and nothing else. Scheduled runs,
# pull-request runs and deployment hooks are excluded by construction rather than by filtering
# them out afterwards, because the failure mode is a green row standing in for a red one.
#
# Usage:
#   tools/ci_status.sh                      # this repository, its default branch
#   tools/ci_status.sh owner/repo           # another repository, its default branch
#   tools/ci_status.sh owner/repo#branch    # another repository, a named branch
#   tools/ci_status.sh -q owner/repo ...    # several, quiet unless something is red
#
# The branch is explicit because "the default branch" is the wrong question for some repos.
# A fork whose work happens on a long-lived integration branch pushes nothing to its default
# branch at all, and a check pointed there finds no runs and must not read that as healthy.
#
# Exit codes follow verify_scan: 0 green, 1 red, 2 cannot tell. Inconclusive is deliberately
# not a pass — a check that cannot see the answer must not report success.

set -euo pipefail
unset TMOUT

EXIT_GREEN=0
EXIT_RED=1
EXIT_UNKNOWN=2

QUIET=0

die() { printf 'ci_status: %s\n' "$*" >&2; exit "$EXIT_UNKNOWN"; }

usage() {
    sed -n '3,28p' "$0" | sed 's/^# \{0,1\}//'
    exit 0
}

# --- per-target check -------------------------------------------------------------------
# Prints its own findings. Returns 0 green, 1 red, 2 cannot tell.
check_target() {
    local target="$1" repo branch runs newest

    repo="${target%%#*}"
    if [[ "$target" == *"#"* ]]; then
        branch="${target#*#}"
    else
        branch="$(gh api "repos/$repo" --jq .default_branch 2>/dev/null)" || {
            printf '  %-34s CANNOT TELL  (no answer from the API)\n' "$repo"
            return "$EXIT_UNKNOWN"
        }
    fi
    [[ -n "$branch" ]] || { printf '  %-34s CANNOT TELL  (no branch)\n' "$repo"; return "$EXIT_UNKNOWN"; }

    # Every push run on that branch, newest first. No workflow filter: a workflow added later
    # is covered without anyone remembering to add it here, and on these repositories the push
    # trigger already selects exactly the gate we care about.
    runs="$(gh run list --repo "$repo" --branch "$branch" --event push --limit 40 \
                --json workflowName,conclusion,status,createdAt,headSha,displayTitle,url \
                2>/dev/null)" || {
        printf '  %-34s CANNOT TELL  (query failed)\n' "$repo#$branch"
        return "$EXIT_UNKNOWN"
    }

    if [[ "$(jq 'length' <<<"$runs")" == "0" ]]; then
        # Never green. A branch with no push runs is a branch this check cannot vouch for,
        # and silence here is how a mistyped branch name would look like good news.
        printf '  %-34s CANNOT TELL  (no push-triggered runs on this branch)\n' "$repo#$branch"
        return "$EXIT_UNKNOWN"
    fi

    # The newest COMPLETED run per workflow. An in-progress run has no conclusion yet and must
    # not displace the last known answer.
    newest="$(jq -c '[.[] | select(.conclusion != "" and .conclusion != null)]
                     | group_by(.workflowName)
                     | map(sort_by(.createdAt) | last)
                     | sort_by(.workflowName)' <<<"$runs")"

    if [[ "$(jq 'length' <<<"$newest")" == "0" ]]; then
        printf '  %-34s CANNOT TELL  (nothing has finished yet)\n' "$repo#$branch"
        return "$EXIT_UNKNOWN"
    fi

    local bad
    bad="$(jq -c '[.[] | select(.conclusion != "success")]' <<<"$newest")"

    if [[ "$(jq 'length' <<<"$bad")" == "0" ]]; then
        [[ "$QUIET" == "1" ]] || printf '  %-34s green\n' "$repo#$branch"
        return "$EXIT_GREEN"
    fi

    # Loud. This is the whole point of the script.
    local streak
    printf '\n'
    printf '  !! %s — the last push run is NOT green\n' "$repo#$branch"
    while IFS=$'\t' read -r wf conclusion created sha title url; do
        streak="$(jq --arg wf "$wf" '[.[] | select(.workflowName == $wf)
                                        | .conclusion]
                                    | index("success") // length' <<<"$runs")"
        printf '     %s: %s since %s (%s consecutive)\n' \
               "$wf" "$conclusion" "${created:0:10}" "$streak"
        printf '       %s  %s\n' "${sha:0:8}" "${title:0:60}"
        printf '       %s\n' "$url"
    done < <(jq -r '.[] | [.workflowName, .conclusion, .createdAt, .headSha, .displayTitle, .url]
                        | @tsv' <<<"$bad")
    return "$EXIT_RED"
}

# --- arguments --------------------------------------------------------------------------
targets=()
while (( $# )); do
    case "$1" in
        -q|--quiet) QUIET=1 ;;
        -h|--help)  usage ;;
        -*)         die "unknown option: $1" ;;
        *)          targets+=("$1") ;;
    esac
    shift
done

command -v gh >/dev/null 2>&1 || die "gh is not installed, so the gate state is unknown"
command -v jq >/dev/null 2>&1 || die "jq is not installed, so the gate state is unknown"
gh auth status >/dev/null 2>&1 || die "gh is not authenticated, so the gate state is unknown"

if (( ${#targets[@]} == 0 )); then
    origin="$(git config --get remote.origin.url 2>/dev/null)" \
        || die "no target given and no git remote to infer one from"
    # git@host:owner/repo.git and https://host/owner/repo.git both reduce to owner/repo.
    inferred="${origin##*[:/]}"
    owner="${origin%/*}"; owner="${owner##*[:/]}"
    targets=("$owner/${inferred%.git}")
fi

worst="$EXIT_GREEN"
for target in "${targets[@]}"; do
    set +e
    check_target "$target"
    result=$?
    set -e
    (( result > worst )) && worst=$result
done

if (( worst == EXIT_RED )); then
    printf '\n'
    printf '  What to do, in this order.\n'
    printf '    1. Open the run above and read the FIRST failing step, not the last.\n'
    printf '    2. Reproduce it locally — the gate runs ruff check ., mypy, then pytest -q.\n'
    printf '    3. Fix the cause. Do not add an ignore or narrow the check to make it pass:\n'
    printf '       a gate that is green because it stopped asking is worse than a red one.\n'
    printf '    4. If it is genuinely not our break, say so in the commit that follows.\n'
    printf '\n'
elif (( worst == EXIT_UNKNOWN )); then
    printf '\n  One or more targets could not be read. That is not a pass — find out why.\n\n'
fi

exit "$worst"
