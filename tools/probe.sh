#!/usr/bin/env bash
# Stand up a throwaway Listenarr to settle a behavioural question by measuring it.
#
# This exists because measuring used to cost six fiddly steps and reasoning cost none, so
# reasoning won, and a claim went into a round that a five minute test would have refuted.
# The point of this script is to make "just test it" the cheap option.
#
# Everything here is independent of any production instance on this machine. It refuses host
# networking, refuses the default port, proves its own port free before binding, and matches the
# port back to the container it started before telling you it is up.
#
#   tools/probe.sh up [--image REF] [--name NAME] [--port N]  start one, print how to talk to it
#   tools/probe.sh api <name> <method> <path> [json]  call it, antiforgery handled for you
#   tools/probe.sh local <name> <path>              call it from TRUE loopback, inside
#   tools/probe.sh config <name>                    dump its config.json
#   tools/probe.sh down <name>                      tear it down, confirm nothing leaked
#
unset TMOUT
set -euo pipefail

PRODUCTION_PORT=4545
DEFAULT_IMAGE=ghcr.io/listenarrs/listenarr:canary
STATE_DIR="${TMPDIR:-/tmp}/listenarr-probe"

die() { echo "probe: $*" >&2; exit 1; }

# Collect the ports already spoken for, once, as a space-delimited list.
#
# This deliberately avoids `ss ... | grep -q`, which silently reported every port free. grep -q
# exits the moment it matches, ss takes SIGPIPE, and `pipefail` turns the whole pipeline non-zero,
# so the `&& continue` that was meant to skip a busy port never fired. The failure mode was a
# container that would not start with "address already in use", which reads as a podman problem
# rather than as the free-port check having never worked.
#
# Rootless podman publishes into this user's netns, so ss sees those mappings, but a container
# that is created and not running holds its published port in `podman port` alone. Ask both.
busy_ports() {
    local from_ss from_podman
    from_ss=$(ss -ltn 2>/dev/null | awk 'NR > 1 { n = split($4, a, ":"); print a[n] }' || true)
    from_podman=$(podman ps -a --format '{{.Ports}}' 2>/dev/null \
        | tr ' ,' '\n\n' \
        | awk -F'->' 'NF == 2 { n = split($1, a, ":"); print a[n] }' || true)
    printf '%s %s' "$from_ss" "$from_podman" | tr '\n' ' '
}

pick_port() {
    local wanted="${1:-}" busy
    busy=" $(busy_ports) "

    if [ -n "$wanted" ]; then
        [ "$wanted" = "$PRODUCTION_PORT" ] && die "refusing to bind the production port"
        case "$busy" in
            *" $wanted "*) die "port $wanted is already in use" ;;
        esac
        echo "$wanted"; return 0
    fi

    for p in $(seq 18900 18999); do
        [ "$p" = "$PRODUCTION_PORT" ] && continue
        case "$busy" in
            *" $p "*) continue ;;
        esac
        echo "$p"; return 0
    done
    die "no free port in 18900-18999"
}

cmd_up() {
    local image="$DEFAULT_IMAGE" name="probe-$$" wanted=""
    while [ $# -gt 0 ]; do
        case "$1" in
            --image) image="${2:?}"; shift 2 ;;
            --name)  name="${2:?}";  shift 2 ;;
            --port)  wanted="${2:?}"; shift 2 ;;
            *) die "unknown option $1" ;;
        esac
    done

    local port; port=$(pick_port "$wanted")
    [ "$port" = "$PRODUCTION_PORT" ] && die "refusing to bind the production port"

    local cfg="$STATE_DIR/$name/config"
    rm -rf "$STATE_DIR/$name"; mkdir -p "$cfg"

    podman network create "$name" >/dev/null 2>&1 || true
    podman rm -f "$name" >/dev/null 2>&1 || true

    # No --network=host, ever. Loopback-bound so nothing on the LAN reaches it either.
    podman run -d --name "$name" \
        --network "$name" \
        -p "127.0.0.1:$port:4545" \
        -v "$cfg:/config:Z" \
        "$image" >/dev/null

    # A port answering is evidence of nothing until it is matched to the container we started.
    podman port "$name" | grep -q ":$port\$" \
        || die "$name is not serving $port; refusing to report it as up"

    printf 'waiting for %s on %s' "$name" "$port" >&2
    local waited=0
    until curl -sf -o /dev/null "http://127.0.0.1:$port/api/v1/system/status" 2>/dev/null; do
        sleep 3; waited=$((waited + 3)); printf '.' >&2
        [ "$waited" -gt 300 ] && { echo >&2; die "$name never became healthy"; }
    done
    echo >&2

    echo "$port" > "$STATE_DIR/$name/port"
    local stamp
    stamp=$(podman exec "$name" grep -o 'Listenarr\.Api/[0-9][^"]*' /app/Listenarr.Api.deps.json 2>/dev/null | head -1)

    cat <<EOF
name:     $name
port:     $port   (production port $PRODUCTION_PORT untouched)
version:  $stamp
          a bare version means STOCK; a +m4bard suffix means one of ours, which is
          not a control for anything we intend to report upstream
base url: http://127.0.0.1:$port/api/v1
config:   $cfg   (on disk inside the container at /app/config/config.json)

  tools/probe.sh api   $name GET  /configuration/startupconfig
  tools/probe.sh local $name /configuration/startupconfig    # TRUE loopback, different answer
  tools/probe.sh down  $name
EOF
}

port_of() {
    local f="$STATE_DIR/$1/port"
    [ -f "$f" ] || die "no probe named $1; run 'up' first"
    cat "$f"
}

# Writes need an antiforgery token carried with the same cookie jar. Handled here so nobody
# rediscovers "Invalid or missing CSRF token" and concludes the endpoint is protected.
cmd_api() {
    local name="${1:?name}" method="${2:?method}" path="${3:?path}" body="${4:-}"
    local port; port="$(port_of "$name")"
    local base="http://127.0.0.1:$port/api/v1"
    local jar="$STATE_DIR/$name/jar"

    if [ "$method" = "GET" ]; then
        curl -sf -b "$jar" -c "$jar" "$base$path"
        return
    fi
    local tok
    tok=$(curl -sf -b "$jar" -c "$jar" "$base/antiforgery/token" \
          | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("token") or d.get("requestToken") or "")')
    [ -n "$tok" ] || die "could not obtain an antiforgery token"
    curl -sf -b "$jar" -c "$jar" -X "$method" "$base$path" \
        -H 'Content-Type: application/json' -H "X-XSRF-TOKEN: $tok" \
        ${body:+--data "$body"}
}

# The app sees a request through a mapped port as NOT loopback. That distinction decided a
# finding today, so it gets its own verb rather than a footnote.
cmd_local() {
    local name="${1:?name}" path="${2:?path}"
    podman exec "$name" python3 -c "
import urllib.request, sys
print(urllib.request.urlopen('http://127.0.0.1:4545/api/v1$path', timeout=30).read().decode())
"
}

cmd_config() { podman exec "${1:?name}" sh -lc 'cat /app/config/config.json'; }

cmd_down() {
    local name="${1:?name}"
    local port=""; [ -f "$STATE_DIR/$name/port" ] && port=$(cat "$STATE_DIR/$name/port")
    podman rm -f "$name" >/dev/null 2>&1 || true
    podman network rm "$name" >/dev/null 2>&1 || true
    rm -rf "${STATE_DIR:?}/$name"
    local prod; prod=$(ss -ltn 2>/dev/null | grep -c ":$PRODUCTION_PORT " || true)
    echo "removed $name; production port $PRODUCTION_PORT still has $prod listener(s), not ours"
    [ -n "$port" ] && echo "port $port released: $(ss -ltn 2>/dev/null | grep -c ":$port " || true) listener(s)"
}

case "${1:-}" in
    up)     shift; cmd_up "$@" ;;
    api)    shift; cmd_api "$@" ;;
    local)  shift; cmd_local "$@" ;;
    config) shift; cmd_config "$@" ;;
    down)   shift; cmd_down "$@" ;;
    *) sed -n '2,/^unset TMOUT/p' "$0" | sed 's/^# \{0,1\}//; $d'; exit 2 ;;
esac
