# shellcheck shell=bash
#
# container_runtime.sh — pick a container runtime that actually works, and clean up after it.
#
# Sourced by the validators in tools/. Three things kept ending runs early, none of them
# hypothetical:
#
#   Which binary exists is not which runtime works.
#       A host can carry both binaries with every container under docker and an empty
#       rootless podman store. `command -v podman` picks podman and the run then observes
#       nothing. So the candidates are probed instead of looked up: a runtime counts as
#       usable when `version` and `ps` both succeed against it. An empty store passes
#       both, which is right — it is usable and merely empty.
#
#   The caller may not be in the group that owns the socket.
#       `docker ps` then fails with a permission error rather than an absence, which is a
#       different problem needing a different message. Each candidate is retried once
#       through `sudo -n`, never interactively, so a host without passwordless sudo says
#       the account needs container access instead of blocking on a password prompt with
#       nobody watching.
#
#   A container writes into its bind mounts as root.
#       Under a rootful runtime the files it leaves cannot be unlinked by the caller, the
#       next run's `rm -rf` fails, and the run then measures the PREVIOUS run's leftovers
#       while reporting on this one. Prevention comes first (cr_ownership_args), removal
#       second (cr_force_rm), and the removal is verified rather than assumed.
#
# Set LISTENARR_TEST_RUNTIME=podman|docker to override the probe; docker wins a tie
# otherwise, because the rootless store is the one that is usually empty.

CR_RUNTIME=""
CR_SUDO=0
CR_ROOTLESS=0
CR_SCRUB_IMAGE=""
CR_OWNERSHIP_ARGS=()

# Is this runtime drivable from here at all, optionally through `sudo -n`?
cr_usable() {
    local runtime="$1" via="${2:-direct}"
    local -a prefix=()
    command -v "$runtime" >/dev/null 2>&1 || return 1
    if [ "$via" = "sudo" ]; then
        command -v sudo >/dev/null 2>&1 || return 1
        prefix=(sudo -n)
    fi
    "${prefix[@]}" "$runtime" version >/dev/null 2>&1 || return 1
    "${prefix[@]}" "$runtime" ps >/dev/null 2>&1 || return 1
    return 0
}

# Run the detected runtime, escalating only if that is the only way it works.
crun() {
    if [ "$CR_SUDO" -eq 1 ]; then
        sudo -n "$CR_RUNTIME" "$@"
    else
        "$CR_RUNTIME" "$@"
    fi
}

# Does the detected runtime already map a container's root onto the caller's own uid?
# That mapping is what makes bind-mounted files land caller-owned without being asked,
# so it decides whether cr_ownership_args has anything to do.
cr_detect_rootless() {
    CR_ROOTLESS=0
    [ "$CR_SUDO" -eq 0 ] || return 0
    [ "$(id -u)" -ne 0 ] || return 0
    case "$CR_RUNTIME" in
        podman)
            [ "$(crun info --format '{{.Host.Security.Rootless}}' 2>/dev/null)" = "true" ] \
                && CR_ROOTLESS=1 ;;
        docker)
            crun info --format '{{join .SecurityOptions ","}}' 2>/dev/null \
                | grep -q 'rootless' && CR_ROOTLESS=1 ;;
    esac
    return 0
}

# Probe for a usable runtime. 0 and CR_RUNTIME set, or 1 with nothing set.
cr_detect() {
    local order="docker podman" candidate
    case "${LISTENARR_TEST_RUNTIME:-}" in
        podman) order="podman" ;;
        docker) order="docker" ;;
        "")     ;;
        *) printf 'LISTENARR_TEST_RUNTIME must be podman or docker\n' >&2; return 1 ;;
    esac

    # Unprivileged across both candidates before escalating to either: a runtime that
    # works without sudo is always the better choice, even when it is second in order.
    CR_RUNTIME=""; CR_SUDO=0
    for candidate in $order; do
        if cr_usable "$candidate" direct; then CR_RUNTIME="$candidate"; CR_SUDO=0; break; fi
    done
    if [ -z "$CR_RUNTIME" ]; then
        for candidate in $order; do
            if cr_usable "$candidate" sudo; then CR_RUNTIME="$candidate"; CR_SUDO=1; break; fi
        done
    fi
    [ -n "$CR_RUNTIME" ] || return 1
    cr_detect_rootless
    cr_ownership_args
    return 0
}

# Detect, describe, or explain. 0 usable, 1 not, with a message a reader can act on.
cr_require() {
    local candidate present="" how=""
    if cr_detect; then
        [ "$CR_SUDO" -eq 1 ] && how=" through sudo -n"
        [ "$CR_ROOTLESS" -eq 1 ] && how="${how} (rootless, so its writes are already ours)"
        printf '%s [runtime] %s%s\n' "$(date +%H:%M:%S)" "$CR_RUNTIME" "$how"
        return 0
    fi
    for candidate in podman docker; do
        command -v "$candidate" >/dev/null 2>&1 && present="${present} ${candidate}"
    done
    if [ -z "$present" ]; then
        printf 'no container runtime: neither podman nor docker is on PATH.\n' >&2
    else
        printf 'a container runtime is installed (%s) but this account cannot drive it: the\n' \
            "${present# }" >&2
        printf 'probe could neither list containers directly nor through sudo -n. Add the account\n' >&2
        printf 'to the group that owns the runtime socket (docker: the docker group), or give it a\n' >&2
        printf 'rootless store it can use, or grant it passwordless sudo for the runtime. Nothing\n' >&2
        printf 'here will prompt for a password.\n' >&2
    fi
    return 1
}

# Args that make a container's writes into a bind mount land owned by the CALLER, so
# there is nothing for cleanup to work around afterwards.
#
# A rootless runtime maps the container's root onto the caller's uid already, which is
# why it gets nothing here: adding --userns=keep-id there would map the container's root
# to a SUBUID instead and reintroduce the very files the caller cannot unlink. A rootful
# runtime has no such mapping, so the image's own entrypoint is asked to drop privilege
# with PUID/PGID, which it honours — it chowns its config directory and re-execs the app
# through gosu. That is the closest equivalent the rootful case has to keep-id, and it
# costs nothing when the mapping is already right.
cr_ownership_args() {
    CR_OWNERSHIP_ARGS=()
    [ "$CR_ROOTLESS" -eq 1 ] && return 0
    CR_OWNERSHIP_ARGS=(-e "PUID=$(id -u)" -e "PGID=$(id -g)")
    return 0
}

# The image cr_force_rm borrows a root shell from. Any image with /bin/sh will do; the
# callers pass the image already under test so nothing extra has to be pulled.
cr_scrub_image() { CR_SCRUB_IMAGE="$1"; }

# rm -rf that copes with what a rootful container left behind. 0 everything is gone,
# 1 something survived, which the caller must treat as a dirty run rather than ignore.
cr_force_rm() {
    local target parent leaf
    for target in "$@"; do
        [ -e "$target" ] || continue
        rm -rf "$target" 2>/dev/null
        [ -e "$target" ] || continue

        # What is left was written by a container running as root, or as a mapped subuid,
        # inside a bind mount. The runtime can unlink what this account cannot, so a
        # throwaway container mounts the PARENT and removes the leaf by name. Mounting
        # the parent rather than the target itself keeps the deletion inside a directory
        # this harness made, and passing the leaf as an argument keeps it out of the
        # shell string.
        parent="$(cd "$(dirname "$target")" 2>/dev/null && pwd)" || parent=""
        leaf="$(basename "$target")"
        if [ -n "$parent" ] && [ -n "$CR_SCRUB_IMAGE" ] && [ -n "$CR_RUNTIME" ]; then
            crun run --rm --entrypoint /bin/sh -v "${parent}:/scrub" "$CR_SCRUB_IMAGE" \
                -c 'rm -rf -- "/scrub/$1"' _ "$leaf" >/dev/null 2>&1
            rm -rf "$target" 2>/dev/null
        fi
        if [ -e "$target" ]; then
            printf 'could not remove %s: a previous run left files this account cannot unlink,\n' \
                "$leaf" >&2
            printf 'and the runtime could not remove them either. This run would measure them.\n' >&2
            return 1
        fi
    done
    return 0
}

# Containers this harness left behind, matched by name prefix. An interrupted run leaves
# one holding the port, and the next run then reports "the API never came up" about a
# container it did not start.
cr_remove_stale() {
    local prefix="$1" name
    while IFS= read -r name; do
        [ -n "$name" ] || continue
        crun rm -f "$name" >/dev/null 2>&1
    done < <(crun ps -a --format '{{.Names}}' 2>/dev/null | grep -E "^${prefix}" || true)
    return 0
}
