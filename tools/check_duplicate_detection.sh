#!/usr/bin/env bash
#
# check_duplicate_detection.sh — what does a build report for two ASINs of ONE work?
#
# The corpus deliberately carries four works that each appear under TWO Audible ASINs sharing a
# single series slot. That is this repo's headline finding: an ASIN identifies a manifestation,
# not a work, so "different ASIN" and "different book" are not the same claim. Three of the four
# pairs also have byte-identical title and author, which is what a library looks like when
# somebody has added the same book twice.
#
# This adds both sides of each pair to a running instance and asks the duplicate endpoint what it
# sees. It reports; it does not judge. What counts as a duplicate is a product decision, and the
# point of running it is to see the answer against real catalogue data rather than invented rows.
#
#   ./tools/check_duplicate_detection.sh localhost/listenarr-vet:<sha>
#   ./tools/check_duplicate_detection.sh ghcr.io/listenarrs/listenarr:canary
#
# Needs podman or docker, and an image to test. Build one from any branch with
# tools/vet-against.sh, or pass a published tag.
set -euo pipefail
unset TMOUT

IMAGE="${1:?usage: check_duplicate_detection.sh <image>}"
ENDPOINT="${ENDPOINT:-/library/duplicates}"
PORT="${PORT:-4610}"
CONTAINER="${CONTAINER:-check-duplicates}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d)"
API="http://127.0.0.1:${PORT}/api/v1"
PY="${ROOT}/.venv/bin/python"
[[ -x "$PY" ]] || PY=python3
RUNTIME="$(command -v podman || command -v docker)"

log() { echo "$(date +%H:%M:%S) [duplicates] $*"; }
cleanup() { "$RUNTIME" rm -f "$CONTAINER" >/dev/null 2>&1 || true; rm -rf "$WORK"; }
trap cleanup EXIT

# Both ASINs of each pair carry the same seriesPrimary.asin and the same position in the corpus,
# which is built and re-verified against live catalogue metadata by tools/build_corpus.py.
TWINS=(
  "B008DFUGCQ B071YLS9YL"   # A Princess of Mars, identical titles
  "B007BR5KZA B002V5CJM4"   # The Wonderful Wizard of Oz, identical titles
  "B002UZJF4U B002V0RG8G"   # The Three Musketeers, identical titles
  "B01FKWL15A B076HSP1FT"   # Leagues, titles differ only in numeral style
)
ASINS=()
for pair in "${TWINS[@]}"; do for a in $pair; do ASINS+=("$a"); done; done

log "generating a library for ${#ASINS[@]} ASINs across ${#TWINS[@]} works"
GEN_ARGS=(); for a in "${ASINS[@]}"; do GEN_ARGS+=(--only-asin "$a"); done
"$PY" "${ROOT}/tools/generate_library.py" --scenario duplicate-editions \
    --out "${WORK}/lib" "${GEN_ARGS[@]}" >/dev/null

mkdir -p "${WORK}/config"
"$PY" "${ROOT}/tools/ffprobe_provisioner.py" --config-dir "${WORK}/config" >/dev/null 2>&1 || true

log "starting ${IMAGE}"
"$RUNTIME" rm -f "$CONTAINER" >/dev/null 2>&1 || true
"$RUNTIME" run -d --name "$CONTAINER" -p "${PORT}:4545" \
    -v "${WORK}/lib:/audiobooks:z" -v "${WORK}/config:/app/config:z" "$IMAGE" >/dev/null
for _ in $(seq 1 90); do curl -fsS "${API}/rootfolders" >/dev/null 2>&1 && break; sleep 2; done
curl -fsS "${API}/rootfolders" >/dev/null 2>&1 || {
    echo "API never came up:" >&2; "$RUNTIME" logs "$CONTAINER" 2>&1 | tail -20 >&2; exit 1; }

KEY="$("$PY" -c "import json;print(json.load(open('${WORK}/config/config.json'))['ApiKey'])")"
AUTH=(-H "X-Api-Key: ${KEY}" -H 'Content-Type: application/json')
curl -fsS -X POST "${API}/rootfolders" "${AUTH[@]}" \
    -d '{"name":"check","path":"/audiobooks","isDefault":true,"caseSensitivityMode":"Sensitive"}' >/dev/null

for asin in "${ASINS[@]}"; do
    ROOT="$ROOT" API="$API" KEY="$KEY" ASIN="$asin" "$PY" - <<'PY' || true
import json, os, urllib.request
books = json.load(open(os.path.join(os.environ["ROOT"], "corpus", "corpus.json")))["books"]
book = next((b for b in books if b["asin"] == os.environ["ASIN"]), None)
if book:
    payload = json.dumps({"metadata": {
        "asin": book["asin"], "title": book["title"], "authors": book["authors"],
        "narrators": book["narrators"], "series": book["series"],
        "seriesNumber": book["series_position"], "source": "Audible", "region": book["region"],
    }, "monitored": True, "autoSearch": False}).encode()
    req = urllib.request.Request(f"{os.environ['API']}/library/add", data=payload, method="POST",
        headers={"Content-Type": "application/json", "X-Api-Key": os.environ["KEY"]})
    try:
        urllib.request.urlopen(req, timeout=60)
    except Exception as exc:
        print(f"  could not add {os.environ['ASIN']}: {exc}")
PY
done

log "library now holds:"
curl -fsS "${API}/library" "${AUTH[@]}" | "$PY" -c "
import json,sys
d = json.load(sys.stdin)
items = d if isinstance(d, list) else d.get('items', d.get('data', []))
for i in sorted(items, key=lambda x: str(x.get('title'))):
    print(f\"    id={i.get('id')} {i.get('asin')} {i.get('title')!r}\")"

log "GET ${ENDPOINT}"
CODE="$(curl -sS -o "${WORK}/out.json" -w '%{http_code}' "${API}${ENDPOINT}" "${AUTH[@]}")"
log "HTTP ${CODE}"
"$PY" - "${WORK}/out.json" <<'PY'
import json, sys
try:
    payload = json.load(open(sys.argv[1]))
except Exception as exc:
    print(f"  response was not JSON: {exc}")
    raise SystemExit(0)
print(json.dumps(payload, indent=2)[:3000])
groups = payload.get("duplicateGroups")
if isinstance(groups, list):
    print(f"\n  groups reported: {len(groups)} (four works were planted, each under two ASINs)")
PY
