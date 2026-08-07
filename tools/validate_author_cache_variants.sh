#!/usr/bin/env bash
#
# validate_author_cache_variants.sh — does one author with two spellings become two cache rows?
# (Listenarr#672, and the part of it PR#673 does not touch)
#
# Issue #672 is about the Authors overview showing two cards for one person when metadata credits
# them with different spacing around initials. PR#673 fixes the card, entirely in the frontend. The
# issue also claims a second, backend effect that the PR does not touch:
#
#     "The inconsistent name also creates a duplicate AuthorCacheEntry in the database, since the
#      backend normalisation treats 'R. R.' and 'R.R.' as different strings. The secondary entry ends
#      up with no image URL..."
#
# The normalisation half of that is readable from the source. `AudiobookRepository.NormalizeAuthorName`
# keeps only letters, digits and whitespace, so punctuation is DELETED rather than replaced by a
# separator:
#
#     "H. G. Wells"  ->  "h g wells"
#     "H.G. Wells"   ->  "hg wells"
#
# and `AuthorCacheEntries` has a UNIQUE index on (AuthorNameNormalized, Region), so two such rows are
# schema-legal. What is NOT readable from the source is whether a second row actually appears, because
# `UpsertCachedAuthorAsync` matches on ASIN before it compares names. If Audible returns the same
# author ASIN for both spellings, the two collapse into one row and the issue's causal chain does not
# hold as written, even though the normalisation gap is real.
#
# That question is about what an external service returns, so it can only be answered by asking. This
# queries the author lookup endpoint once per spelling and then reads the rows out of the database.
#
# It REPORTS, it does not judge. Whether one row or two is "correct" is a product decision. The useful
# output is the observed rows, their normalized keys, their ASINs and whether an image survived.
#
# The variant is DERIVED, never invented: it takes an author that `corpus/corpus.json` already
# credits with spaced initials and closes the spaces. Both spellings therefore describe a real
# author the corpus actually contains.
#
#   ./tools/validate_author_cache_variants.sh --image ghcr.io/listenarrs/listenarr:canary
#
# NETWORK: unlike the other runtime checks, this one needs the container to reach Audible and
# Audnexus, since the whole question is what they return. A lookup that cannot reach them exits 2
# rather than reporting a result, because "no rows" would otherwise read as "no duplicate".
#
# Exit 0 both spellings resolved to ONE row, 1 they produced TWO rows, 2 nothing conclusive.
#
set -uo pipefail
unset TMOUT

IMAGE="ghcr.io/listenarrs/listenarr:canary"
AUTHOR=""
REGION="us"
PORT=4700
LABEL=""
JSON_OUT=""
KEEP=0
ALL=0
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${ROOT}/.venv/bin/python"
BUILD="${ROOT}/build/author-cache"
CONTAINER="listenarr-authorcache-$$"

usage() {
    cat <<EOF
validate_author_cache_variants.sh — one author, two spellings: one cache row or two? (#672)

  --image REF     container image (default: ${IMAGE})
  --author NAME   author to test; must appear in corpus.json with spaced initials.
                  Default: the first such author in the corpus.
  --region CODE   Audible region (default: ${REGION})
  --port N        host port (default: ${PORT})
  --label TEXT    label for the report header
  --json PATH     also write the observed rows as JSON
  --keep          leave the container running
  --all           run every corpus author that has spaced initials, one container each,
                  and print a summary. One author proves little either way, because
                  whether the spellings split depends on what Audible returns for that
                  particular name.
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --image)  IMAGE="$2";    shift 2 ;;
        --author) AUTHOR="$2";   shift 2 ;;
        --region) REGION="$2";   shift 2 ;;
        --port)   PORT="$2";     shift 2 ;;
        --label)  LABEL="$2";    shift 2 ;;
        --json)   JSON_OUT="$2"; shift 2 ;;
        --keep)   KEEP=1;        shift ;;
        --all)    ALL=1;         shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done
[ -n "$LABEL" ] || LABEL="$IMAGE"

log() { printf '%s [author] %s\n' "$(date +%H:%M:%S)" "$*"; }
die() { printf '%s [author] ERROR: %s\n' "$(date +%H:%M:%S)" "$*" >&2; exit 2; }

if command -v podman >/dev/null 2>&1; then RUNTIME=podman
elif docker info >/dev/null 2>&1; then RUNTIME=docker
else die "no usable container runtime"; fi
[ -x "$PY" ] || die "no venv — python3 -m venv .venv && .venv/bin/pip install -e ."
command -v sqlite3 >/dev/null 2>&1 || die "sqlite3 is required"

cleanup() {
    [ "$KEEP" -eq 1 ] && { log "leaving ${CONTAINER} on port ${PORT}"; return 0; }
    "$RUNTIME" rm -f "$CONTAINER" >/dev/null 2>&1
    return 0
}
trap cleanup EXIT

# --all re-invokes this script once per candidate author. Each author needs its own container and
# its own database, and self-invocation keeps that isolation without a second code path that could
# drift from the single-author one.
if [ "$ALL" -eq 1 ]; then
    CANDIDATES=$(CORPUS_ROOT="$ROOT" "$PY" <<'LISTEOF'
import json, os, re
with open(os.path.join(os.environ["CORPUS_ROOT"], "corpus", "corpus.json")) as handle:
    corpus = json.load(handle)
names = []
for book in corpus["books"]:
    for author in book["authors"]:
        if author not in names:
            names.append(author)
spaced = re.compile(r"\b[A-Z]\.\s+[A-Z]\.")
for name in sorted(n for n in names if spaced.search(n)):
    print(name)
LISTEOF
)
    [ -n "$CANDIDATES" ] || die "no corpus author has spaced initials"

    SPLIT=0; MERGED=0; UNCLEAR=0
    SUMMARY=""
    while IFS= read -r candidate; do
        [ -n "$candidate" ] || continue
        log "=== ${candidate} ==="
        "$0" --image "$IMAGE" --author "$candidate" --region "$REGION" --port "$PORT"
        case $? in
            0) MERGED=$((MERGED + 1)); verdict="one row" ;;
            1) SPLIT=$((SPLIT + 1));  verdict="TWO ROWS" ;;
            *) UNCLEAR=$((UNCLEAR + 1)); verdict="inconclusive" ;;
        esac
        SUMMARY="${SUMMARY}$(printf '  %-20s %s' "$candidate" "$verdict")\n"
    done <<< "$CANDIDATES"

    echo
    echo "===== SWEEP SUMMARY (${LABEL}) ====="
    printf '%b' "$SUMMARY"
    echo
    echo "  split into two rows : ${SPLIT}"
    echo "  merged into one     : ${MERGED}"
    echo "  inconclusive        : ${UNCLEAR}"
    echo
    echo "Splitting depends on whether Audible returns a different author ASIN for the closed-up"
    echo "spelling, so a mixed result is the expected shape rather than a flaw in the check."
    [ "$UNCLEAR" -gt 0 ] && exit 2
    [ "$SPLIT" -gt 0 ] && exit 1
    exit 0
fi

# The spaced spelling comes from the corpus; the unspaced one is derived from it by closing the gaps
# after initials. Deriving it keeps both spellings tied to a real author the corpus contains, rather
# than inventing a name to make the test work.
#
# Command substitution, not `read` from a process substitution: `$?` after `read` reports whether
# `read` got a line, which is not the same thing as whether the picker succeeded.
PICKED=$(CORPUS_ROOT="$ROOT" REQUESTED="$AUTHOR" "$PY" <<'PICKEOF'
import json, os, re, sys

with open(os.path.join(os.environ["CORPUS_ROOT"], "corpus", "corpus.json")) as handle:
    corpus = json.load(handle)
requested = os.environ.get("REQUESTED", "")

names = []
for book in corpus["books"]:
    for author in book["authors"]:
        if author not in names:
            names.append(author)

spaced_initials = re.compile(r"\b[A-Z]\.\s+[A-Z]\.")

if requested:
    if requested not in names:
        sys.exit(3)
    chosen = requested
else:
    candidates = sorted(n for n in names if spaced_initials.search(n))
    if not candidates:
        sys.exit(4)
    chosen = candidates[0]

# Close the space after an initial: "H. G. Wells" -> "H.G. Wells"
variant = re.sub(r"(\b[A-Z]\.)\s+(?=[A-Z]\.)", r"\1", chosen)
if variant == chosen:
    sys.exit(5)
print(chosen)
print(variant)
PICKEOF
)
PICK_STATUS=$?
SPACED=$(printf '%s\n' "$PICKED" | sed -n '1p')
UNSPACED=$(printf '%s\n' "$PICKED" | sed -n '2p')
case "$PICK_STATUS" in
    0) ;;
    3) die "--author must be a name that appears in corpus/corpus.json" ;;
    4) die "no corpus author has spaced initials; pass --author" ;;
    5) die "'${AUTHOR}' has no spaced initials to close; pass a different --author" ;;
    *) die "could not choose an author" ;;
esac
[ -n "$SPACED" ] && [ -n "$UNSPACED" ] || die "could not choose an author"

log "corpus spelling: '${SPACED}'"
log "derived variant:  '${UNSPACED}'"

CONFIG="${BUILD}/config"
LIBRARY="${BUILD}/library"
rm -rf "$BUILD"; mkdir -p "$CONFIG" "$LIBRARY"

"$PY" "${ROOT}/tools/ffprobe_provisioner.py" --config-dir "$CONFIG" >/dev/null \
    || die "could not provision ffprobe"

"$RUNTIME" rm -f "$CONTAINER" >/dev/null 2>&1
"$RUNTIME" run -d --name "$CONTAINER" -p "${PORT}:4545" -e LISTENARR_LOG_LEVEL=Debug \
    -v "${LIBRARY}:/audiobooks" -v "${CONFIG}:/app/config" "$IMAGE" >/dev/null \
    || die "could not start ${IMAGE}"

API="http://localhost:${PORT}/api/v1"
for _ in $(seq 1 120); do curl -fsS "${API}/system/status" >/dev/null 2>&1 && break; sleep 2; done
curl -fsS "${API}/system/status" >/dev/null 2>&1 || die "API never came up"

KEY=$("$PY" -c "import json; print(json.load(open('${CONFIG}/config.json'))['ApiKey'])") || die "no ApiKey"
AUTH=(-H "X-Api-Key: ${KEY}" -H 'Content-Type: application/json')

curl -fsS -X POST "${API}/rootfolders" "${AUTH[@]}" \
    -d '{"name":"lib","path":"/audiobooks","isDefault":true,"caseSensitivityMode":"Sensitive"}' >/dev/null \
    || die "could not create the root folder"

DB="${CONFIG}/database/listenarr.db"
[ -f "$DB" ] || die "no database at ${DB}"

lookup() {
    local name="$1"
    local encoded
    encoded=$("$PY" -c "import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1]))" "$name")
    curl -sS -o /dev/null -w '%{http_code}' \
        "${API}/metadata/author?name=${encoded}&region=${REGION}" "${AUTH[@]}"
}

log "looking up the corpus spelling"
CODE_SPACED=$(lookup "$SPACED")
log "  HTTP ${CODE_SPACED}"
sleep 2
log "looking up the derived variant"
CODE_UNSPACED=$(lookup "$UNSPACED")
log "  HTTP ${CODE_UNSPACED}"
sleep 3

# A lookup that never reached Audible tells us nothing about what Audible returns, and an empty table
# would otherwise read as "no duplicate". Refuse to report rather than report a comfortable non-answer.
ROW_COUNT=$(sqlite3 "$DB" "SELECT COUNT(*) FROM AuthorCacheEntries;" 2>/dev/null || echo "")
if [ -z "$ROW_COUNT" ]; then
    die "could not read AuthorCacheEntries"
fi
if [ "$ROW_COUNT" -eq 0 ]; then
    log "both lookups returned HTTP ${CODE_SPACED}/${CODE_UNSPACED} and no rows were cached"
    "$RUNTIME" logs "$CONTAINER" 2>&1 | grep -iE "audnex|audible|resolve|http" | tail -6
    die "no author was cached at all, so this says nothing about duplicates (network? region?)"
fi

echo "===== ${LABEL} ====="
echo "corpus spelling : ${SPACED}"
echo "derived variant : ${UNSPACED}"
echo "lookup status   : ${CODE_SPACED} / ${CODE_UNSPACED}"
echo
printf '%s\n' "rows in AuthorCacheEntries:"
sqlite3 -header -column "$DB" \
    "SELECT Id, AuthorName, AuthorNameNormalized, AuthorAsin, Region,
            CASE WHEN ImageUrl IS NULL OR ImageUrl = '' THEN 'none' ELSE 'present' END AS Image
     FROM AuthorCacheEntries ORDER BY Id;"
echo

if [ -n "$JSON_OUT" ]; then
    sqlite3 -json "$DB" \
        "SELECT Id, AuthorName, AuthorNameNormalized, AuthorAsin, Region, ImageUrl
         FROM AuthorCacheEntries ORDER BY Id;" > "$JSON_OUT" 2>/dev/null \
        && log "wrote ${JSON_OUT}"
fi

DISTINCT_KEYS=$(sqlite3 "$DB" "SELECT COUNT(DISTINCT AuthorNameNormalized) FROM AuthorCacheEntries;")
DISTINCT_ASINS=$(sqlite3 "$DB" "SELECT COUNT(DISTINCT COALESCE(AuthorAsin,'')) FROM AuthorCacheEntries;")
NO_IMAGE=$(sqlite3 "$DB" "SELECT COUNT(*) FROM AuthorCacheEntries WHERE ImageUrl IS NULL OR ImageUrl = '';")

echo "===== OBSERVED ====="
echo "  rows                     : ${ROW_COUNT}"
echo "  distinct normalized keys : ${DISTINCT_KEYS}"
echo "  distinct author ASINs    : ${DISTINCT_ASINS}"
echo "  rows with no image       : ${NO_IMAGE}"
echo

if [ "$ROW_COUNT" -ge 2 ] && [ "$DISTINCT_KEYS" -ge 2 ]; then
    echo "TWO ROWS: the two spellings cached separately, so the normalisation gap reached the"
    echo "database. The ASIN column above says whether Audible also disagreed, which is what decides"
    echo "if #672's stated cause is the whole story."
    exit 1
fi
if [ "$ROW_COUNT" -eq 1 ]; then
    echo "ONE ROW: the two spellings collapsed to a single entry. The upsert matches on ASIN before"
    echo "it compares names, so the likeliest reading is that Audible returned the same author ASIN"
    echo "for both. The normalisation gap is still present in the source; it just did not surface here."
    exit 0
fi
echo "INCONCLUSIVE: ${ROW_COUNT} row(s) with ${DISTINCT_KEYS} distinct key(s); not a clean answer."
exit 2
