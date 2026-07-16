#!/usr/bin/env bash
#
# vet-against.sh — build a Listenarr branch/PR from source and run the harness against it.
#
# Codifies the manual clone -> podman build -> benchmark_scan flow: given a repo and a branch,
# it produces (or reuses) a container image for that exact commit and scans a generated library
# against it. This is how the scan overmatch was reproduced against the #717 path-hardening branch.
#
#   ./tools/vet-against.sh --branch bugfix/unix-folder-name-space --layout listenarr --no-basepath
#   ./tools/vet-against.sh --repo https://github.com/someone/Listenarr.git --branch my-fix --dry-run
#
set -euo pipefail
unset TMOUT

REPO="https://github.com/Listenarrs/Listenarr.git"
BRANCH=""
DRY_RUN=0
RUNTIME=""
PASSTHROUGH=()   # forwarded verbatim to benchmark_scan.sh (--layout, --scenario, --books, ...)

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

die()  { echo "vet-against: $*" >&2; exit 1; }
log()  { echo "$(date +%H:%M:%S) [vet] $*" >&2; }

usage() {
    cat <<EOF
vet-against.sh — build a Listenarr branch and run the harness against it.

  --repo URL       git repo to build (default: ${REPO})
  --branch REF     branch or tag to build (REQUIRED)
  --dry-run        print the clone/build/run plan and exit; touch nothing
  --help           this help

Any other flag is forwarded to benchmark_scan.sh, e.g.:
  --layout KEY     force a single layout (or alias, e.g. 'listenarr')
  --scenario KEY   scenario to generate
  --books N        how many audiobooks to add and scan
  --no-basepath    clear BasePath so the scan falls back to the library root
  --keep           leave the container running for inspection
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --repo)    REPO="$2";   shift 2 ;;
        --branch)  BRANCH="$2"; shift 2 ;;
        --dry-run) DRY_RUN=1;   shift ;;
        -h|--help) usage; exit 0 ;;
        *)         PASSTHROUGH+=("$1"); shift ;;
    esac
done

[[ -n "$BRANCH" ]] || die "--branch is required (see --help)"
command -v git >/dev/null 2>&1 || die "git is required (Ubuntu: sudo apt install git)"
if command -v podman >/dev/null 2>&1; then
    RUNTIME=podman
elif docker info >/dev/null 2>&1; then
    RUNTIME=docker
else
    die "no usable container runtime (podman not installed, docker daemon unreachable)"
fi

# One clone per invocation; discarded at the end (the image is what persists).
SRC="${ROOT}/build/vet-src"

if [[ "$DRY_RUN" -eq 1 ]]; then
    # Show the plan without touching anything. The image tag is only known after a real clone
    # (it is derived from the resolved commit), so describe it rather than invent a SHA.
    cat <<EOF
plan (dry run — nothing executed):
  1. git clone --depth 1 --branch ${BRANCH} ${REPO} ${SRC}
  2. tag  = listenarr-vet:<short-sha of ${BRANCH}>
  3. ${RUNTIME} build -t <tag> ${SRC}        # skipped if <tag> already exists
  4. ${ROOT}/tools/benchmark_scan.sh --image localhost/<tag>${PASSTHROUGH:+ ${PASSTHROUGH[*]}}
  5. rm -rf ${SRC}                            # the image is cached, the clone is not
EOF
    exit 0
fi

log "cloning ${BRANCH} from ${REPO}"
rm -rf "$SRC"
git clone --depth 1 --branch "$BRANCH" "$REPO" "$SRC" >/dev/null 2>&1 \
    || die "could not clone ${BRANCH} from ${REPO}"

SHA="$(git -C "$SRC" rev-parse --short HEAD)"
TAG="listenarr-vet:${SHA}"
log "resolved ${BRANCH} -> ${SHA}"

# Reuse the image for this exact commit if it is already built — verified against the store,
# never assumed. (Rootless stores are per-account; a cached image elsewhere is not cached here.)
if "$RUNTIME" images --format '{{.Repository}}:{{.Tag}}' 2>/dev/null | grep -qx "localhost/${TAG}"; then
    log "image ${TAG} already built; reusing"
else
    log "building ${TAG} (this takes a few minutes)"
    "$RUNTIME" build --network=host -t "$TAG" "$SRC" >/dev/null \
        || die "build failed for ${BRANCH}"
fi

rm -rf "$SRC"

log "running the harness against ${TAG}"
exec "${ROOT}/tools/benchmark_scan.sh" --image "localhost/${TAG}" "${PASSTHROUGH[@]}"
