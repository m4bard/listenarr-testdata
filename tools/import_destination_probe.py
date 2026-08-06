"""One mode of the Library Import destination check (Listenarr#798).

Driven by ``tools/validate_import_destination.sh``, which starts a fresh container per mode and
sets the environment this reads. It is a separate file rather than a heredoc because each mode
needs its own instance: an earlier import leaves a registered file behind, and PR#717's ownership
registry remembers it even after the audiobook row is deleted, which turns a later mode into a
failure that has nothing to do with the destination.

Writes one result row as JSON to ``$ROW_OUT`` and prints the reasoning to stdout.
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
MODE = os.environ["MODE"]
ROW_OUT = os.environ["ROW_OUT"]
SCAN_ROOT = os.environ["SCAN_ROOT"]
DEST_ROOT = os.environ["DEST_ROOT"]
SCAN_HOST = os.environ["SCAN_HOST"]
DEST_HOST = os.environ["DEST_HOST"]
ROOT = os.environ["ROOT"]

HEADERS = {"Content-Type": "application/json", "X-Api-Key": KEY}


def call(path: str, payload: dict | None = None, method: str = "GET") -> tuple[int, dict]:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(f"{API}{path}", data=data, method=method, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=120) as response:
            return response.status, json.loads(response.read().decode() or "{}")
    except urllib.error.HTTPError as error:
        body = error.read().decode() or "{}"
        try:
            return error.code, json.loads(body)
        except json.JSONDecodeError:
            return error.code, {"raw": body}


def base_path_of(body: dict) -> str | None:
    return body.get("basePath") or (body.get("audiobook") or {}).get("basePath")


def id_of(body: dict) -> int | None:
    return body.get("id") or (body.get("audiobook") or {}).get("id")


def organized_under(host_root: str) -> list[str]:
    """Audio files in a SUBDIRECTORY of host_root, i.e. placed there by an import.

    Flat files at the root are the untouched drop folder, not an import, so they never count.
    """
    found = []
    for dirpath, _dirnames, filenames in os.walk(host_root):
        if os.path.abspath(dirpath) == os.path.abspath(host_root):
            continue
        found.extend(os.path.join(dirpath, n) for n in filenames if n.endswith(".m4b"))
    return sorted(found)


def finish(row: dict) -> None:
    with open(ROW_OUT, "w") as handle:
        json.dump(row, handle, indent=2)
    sys.exit(0)


with open(os.path.join(ROOT, "corpus", "corpus.json")) as handle:
    books = json.load(handle)["books"]
book = next(b for b in books if b["asin"] == ASIN)
metadata = {
    "asin": book["asin"], "title": book["title"], "authors": book["authors"],
    "narrators": book["narrators"], "series": book["series"],
    "seriesNumber": book["series_position"], "source": "Audible", "region": book["region"],
}

sources = sorted(
    os.path.join(SCAN_ROOT, f)
    for f in os.listdir(SCAN_HOST)
    if f.endswith(".m4b") and os.path.isfile(os.path.join(SCAN_HOST, f))
)

print(f"\n--- mode: {MODE} ---")
if not sources:
    print("  INCONCLUSIVE: no source files in the drop folder.")
    finish({"mode": MODE, "verdict": "inconclusive", "reason": "no sources"})

if MODE == "new":
    # The frontend's new-book path: the destination rides along on the add.
    status, body = call("/library/add", {
        "metadata": metadata, "monitored": True, "autoSearch": False,
        "destinationPath": DEST_ROOT,
    }, "POST")
    book_id = id_of(body)
elif MODE == "unpatched":
    # The working negative, and the failure this design permits. The book is already in the library
    # pointing at the scanned root and the BasePath patch never lands, which is exactly what the
    # frontend produces when its PUT throws: it catches and continues. Nothing is forced beyond
    # skipping that one call, and the import then runs normally.
    #
    # This mode existing is what makes the other two mean anything. Without a case the check is
    # known to fail, "pass" everywhere is indistinguishable from a check that cannot fail at all.
    status, body = call("/library/add", {
        "metadata": metadata, "monitored": True, "autoSearch": False,
        "destinationPath": SCAN_ROOT,
    }, "POST")
    book_id = id_of(body)
    print("  BasePath left at the scanned root; the frontend's patch is skipped")
else:
    # The frontend's 409 path. The precondition is the part that matters: the book is ALREADY in the
    # library pointing at the SCANNED root, which is what the frontend's own comment describes
    # ("Existing audiobooks may have BasePath = null or pointing to the wrong location"). Staging it
    # any other way tests a book that was already correct, which proves nothing.
    _s, seeded = call("/library/add", {
        "metadata": metadata, "monitored": True, "autoSearch": False,
        "destinationPath": SCAN_ROOT,
    }, "POST")
    book_id = id_of(seeded)
    if book_id:
        _s, staged = call(f"/library/{book_id}")
        print(f"  staged as pre-existing with BasePath: {base_path_of(staged)!r}")

    status, body = call("/library/add", {
        "metadata": metadata, "monitored": True, "autoSearch": False,
        "destinationPath": DEST_ROOT,
    }, "POST")
    print(f"  re-add returned HTTP {status} (409 is the branch under test)")
    book_id = id_of(body) or book_id

if not book_id:
    print(f"  add failed: HTTP {status} {str(body)[:200]}")
    finish({"mode": MODE, "verdict": "inconclusive", "reason": "add failed"})

if MODE == "preexisting":
    pstatus, pbody = call(f"/library/{book_id}", {"basePath": DEST_ROOT}, "PUT")
    print(f"  PUT basePath -> HTTP {pstatus}, stored as: {base_path_of(pbody)!r}")
    if pstatus >= 400:
        # The frontend swallows this failure, so record it rather than aborting. An import that then
        # lands in the wrong place is a consequence of it, not a separate fault.
        print("  NOTE: the BasePath patch failed. The frontend ignores this failure too.")

_s, before = call(f"/library/{book_id}")
print(f"  BasePath before import: {base_path_of(before)!r}")

status, result = call("/library/manual-import", {
    "path": SCAN_ROOT,
    "mode": "interactive",
    "action": "copy",
    "includeCompanionFiles": True,
    "cleanupEmptySourceFolders": False,
    "items": [{"relativePath": os.path.basename(p), "fullPath": p, "matchedAudiobookId": book_id}
              for p in sources],
}, "POST")

results = result.get("results") or []
reported = [r.get("destinationPath") for r in results if r.get("destinationPath")]
succeeded = [r for r in results if r.get("success")]
errors = [str(r.get("error")) for r in results if not r.get("success")]
print(f"  import: HTTP {status}, {len(succeeded)}/{len(results)} file(s) reported success")
for message in errors:
    print(f"    failed: {message[:160]}")
for path in reported:
    print(f"  API says it wrote: {path}")

time.sleep(3)
in_dest = organized_under(DEST_HOST)
in_scan = organized_under(SCAN_HOST)
print(f"  organized under destination root ({DEST_ROOT}): {len(in_dest)}")
for path in in_dest:
    print(f"    {DEST_ROOT}{path[len(DEST_HOST):]}")
print(f"  organized under scanned root ({SCAN_ROOT}):     {len(in_scan)}")
for path in in_scan:
    print(f"    {SCAN_ROOT}{path[len(SCAN_HOST):]}")

row = {"mode": MODE, "reported": reported, "errors": errors,
       "in_destination": len(in_dest), "in_scanned": len(in_scan)}

if in_dest and not in_scan:
    print("  PASS: organized into the selected destination root.")
    row["verdict"] = "pass"
elif in_scan and not in_dest:
    print("  REPRODUCED #798: organized into the SCANNED root, ignoring the destination.")
    row["verdict"] = "reproduced"
elif in_dest and in_scan:
    print("  REPRODUCED #798 (partially): files landed under BOTH roots.")
    row["verdict"] = "reproduced"
else:
    print("  INCONCLUSIVE: nothing was organized under either root.")
    row["verdict"] = "inconclusive"

finish(row)
