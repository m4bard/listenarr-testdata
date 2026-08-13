"""Does one naming pattern produce one layout, whichever code path applies it? (Listenarr)

Driven by ``tools/validate_naming_parity.sh``. Listenarr applies `FolderNamingPattern` from more
than one place. This asks whether those places agree, by putting the SAME book and the SAME pattern
through two of them and comparing the paths that come out:

  import   the destination the manual-import planner computes
  rename   the destination the rename preview plans

A token one path supplies and the other does not is invisible in normal use: an unknown token
resolves to an empty sentinel and its surrounding separator is cleaned up. The pattern does not
error, it just quietly means something different depending on how the file arrived.

Writes the result as JSON to ``$RESULT_OUT`` and prints the comparison.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

API = os.environ["API"]
KEY = os.environ["KEY"]
ASIN = os.environ["ASIN"]
ROOT = os.environ["ROOT"]
LIB_REMOTE = os.environ["LIB_REMOTE"]
SCAN_HOST = os.environ["SCAN_HOST"]
PATTERN = os.environ["PATTERN"]
RESULT_OUT = os.environ["RESULT_OUT"]

HEADERS = {"Content-Type": "application/json", "X-Api-Key": KEY}


def call(path: str, payload=None, method: str = "GET"):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(f"{API}{path}", data=data, method=method, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=180) as response:
            body = response.read().decode()
            return response.status, (json.loads(body) if body.strip() else {})
    except urllib.error.HTTPError as error:
        body = error.read().decode() or "{}"
        try:
            return error.code, json.loads(body)
        except json.JSONDecodeError:
            return error.code, {"raw": body}


with open(os.path.join(ROOT, "corpus", "corpus.json")) as handle:
    books = json.load(handle)["books"]
book = next(b for b in books if b["asin"] == ASIN)

if not book.get("series") or not book.get("series_position"):
    print(f"  INCONCLUSIVE: {ASIN} has no series position, so the token is empty either way.")
    with open(RESULT_OUT, "w") as handle:
        json.dump({"verdict": "inconclusive", "reason": "book has no series position"}, handle)
    sys.exit(2)

print(f"  book    : {book['title']}")
print(f"  series  : {book['series']} #{book['series_position']}")
print(f"  pattern : {PATTERN}")

print("\n  setting the folder naming pattern")
_s, settings = call("/configuration/settings")
if isinstance(settings, dict) and settings:
    settings["folderNamingPattern"] = PATTERN
    call("/configuration/settings", settings, "POST")

status, body = call("/library/add", {
    "metadata": {
        "asin": book["asin"], "title": book["title"], "authors": book["authors"],
        "narrators": book["narrators"], "series": book["series"],
        "seriesNumber": book["series_position"], "source": "Audible", "region": book["region"],
    },
    "monitored": True, "autoSearch": False, "destinationPath": LIB_REMOTE,
}, "POST")
book_id = body.get("id") or (body.get("audiobook") or {}).get("id")
if not book_id:
    print(f"  add failed: HTTP {status} {str(body)[:160]}")
    with open(RESULT_OUT, "w") as handle:
        json.dump({"verdict": "inconclusive", "reason": "add failed"}, handle)
    sys.exit(2)

sources = sorted(
    f"{LIB_REMOTE}/{f}" for f in os.listdir(SCAN_HOST) if f.endswith(".m4b")
)
if not sources:
    print("  INCONCLUSIVE: nothing to import.")
    with open(RESULT_OUT, "w") as handle:
        json.dump({"verdict": "inconclusive", "reason": "no source files"}, handle)
    sys.exit(2)

# PATH ONE: what the manual-import planner computes.
print("  importing, to see what the import planner computes")
status, result = call("/library/manual-import", {
    "path": LIB_REMOTE, "mode": "interactive", "action": "copy",
    "includeCompanionFiles": True, "cleanupEmptySourceFolders": False,
    "items": [{"relativePath": os.path.basename(p), "fullPath": p,
               "matchedAudiobookId": book_id} for p in sources],
}, "POST")
results = result.get("results") or []
import_dest = next((r.get("destinationPath") for r in results if r.get("destinationPath")), None)
errors = [str(r.get("error")) for r in results if not r.get("success")]
for message in errors:
    print(f"    import reported: {message[:140]}")
time.sleep(3)

# PATH TWO: what the rename preview plans, for the same book under the same pattern.
print("  asking the rename preview what it would plan for the same book")
status, preview = call(f"/library/{book_id}/rename/preview", {}, "POST")
rename_dest = preview.get("newFolderPath") or preview.get("NewFolderPath")

print(f"\n  import planner destination : {import_dest}")
print(f"  rename preview destination : {rename_dest}")

row = {"book": book["title"], "series": book["series"],
       "series_position": book["series_position"], "pattern": PATTERN,
       "import_destination": import_dest, "rename_destination": rename_dest}

if not import_dest or not rename_dest:
    print("\n  INCONCLUSIVE: one of the two paths produced nothing to compare.")
    row["verdict"] = "inconclusive"
    with open(RESULT_OUT, "w") as handle:
        json.dump(row, handle, indent=2)
    sys.exit(2)

position = str(book["series_position"])
import_has = position in (import_dest.rsplit("/", 2)[-2] if "/" in import_dest else "")
rename_has = position in rename_dest

print(f"  position {position!r} present in import path : {import_has}")
print(f"  position {position!r} present in rename path : {rename_has}")

# Compare the folder portion. The import destination includes the filename and the rename preview
# is a folder, so trim the file off rather than reporting a difference that is only that.
import_folder = import_dest.rsplit("/", 1)[0]
row["import_folder"] = import_folder

if import_folder.rstrip("/") == rename_dest.rstrip("/"):
    print("\n  AGREE: both paths applied the pattern the same way.")
    row["verdict"] = "agree"
else:
    print("\n  DISAGREE: the same pattern produced two different folders for the same book.")
    print(f"    import : {import_folder}")
    print(f"    rename : {rename_dest}")
    print("  A book imported now and renamed later would move, with no setting having changed.")
    row["verdict"] = "disagree"

with open(RESULT_OUT, "w") as handle:
    json.dump(row, handle, indent=2)
sys.exit(0)
