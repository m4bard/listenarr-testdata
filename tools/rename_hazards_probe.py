"""Drive a real rename over the path-hazard corpus (the destructive axis).

Called by ``tools/validate_rename_hazards.sh`` between the before-snapshot and the audit. Its job
is to get every generated book into the library, force a relocation, and then EXECUTE the rename
rather than previewing it. Previewing is what the sidecar check does, and a plan that reads
correctly is not evidence about what the filesystem ends up holding.

Prints what it did and writes a summary to ``$RESULT_OUT``. The verdict is not decided here: the
audit in ``verify_scan.py`` owns that, because the question is about bytes on disk rather than
anything the API reports.
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
ROOT = os.environ["ROOT"]
LIB_REMOTE = os.environ["LIB_REMOTE"]
MANIFEST = os.environ["MANIFEST"]
RESULT_OUT = os.environ["RESULT_OUT"]
PATTERN = os.environ["PATTERN"]

HEADERS = {"Content-Type": "application/json", "X-Api-Key": KEY}


def call(path: str, payload=None, method: str = "GET"):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(f"{API}{path}", data=data, method=method, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=300) as response:
            body = response.read().decode()
            return response.status, (json.loads(body) if body.strip() else {})
    except urllib.error.HTTPError as error:
        body = error.read().decode() or "{}"
        try:
            return error.code, json.loads(body)
        except json.JSONDecodeError:
            return error.code, {"raw": body}


with open(MANIFEST) as handle:
    manifest = json.load(handle)
with open(os.path.join(ROOT, "corpus", "corpus.json")) as handle:
    corpus = {b["asin"]: b for b in json.load(handle)["books"]}

asins = []
for entry in manifest["entries"]:
    asin = entry.get("belongs_to_asin")
    if asin and asin not in asins:
        asins.append(asin)
print(f"  manifest holds {len(manifest['entries'])} files across {len(asins)} books")

added = []
for asin in asins:
    book = corpus.get(asin)
    if not book:
        continue
    _s, body = call("/library/add", {
        "metadata": {
            "asin": book["asin"], "title": book["title"], "authors": book["authors"],
            "narrators": book["narrators"], "series": book["series"],
            "seriesNumber": book["series_position"], "source": "Audible",
            "region": book["region"],
        },
        "monitored": True, "autoSearch": False, "destinationPath": LIB_REMOTE,
    }, "POST")
    book_id = body.get("id") or (body.get("audiobook") or {}).get("id")
    if book_id:
        added.append(book_id)
print(f"  added {len(added)} book(s) to the library")
if not added:
    with open(RESULT_OUT, "w") as handle:
        json.dump({"stage": "add", "error": "no books added"}, handle)
    sys.exit(2)

# The scan is what attaches the on-disk files to those records. Without it the rename has nothing
# to move and a clean audit afterwards would only mean nothing happened.
print("  scanning so the hazard files become tracked")
for book_id in added:
    call(f"/library/{book_id}/scan", {}, "POST")
time.sleep(8)

tracked = 0
for book_id in added:
    _s, dbg = call(f"/library/{book_id}/files-debug")
    # This endpoint has returned both a bare list and an object wrapping one, so accept either
    # rather than assuming the shape and crashing partway through a destructive run.
    if isinstance(dbg, list):
        files = dbg
    elif isinstance(dbg, dict):
        files = dbg.get("files") or dbg.get("audiobookFiles") or []
    else:
        files = []
    tracked += len(files) if isinstance(files, list) else 0
print(f"  tracked files after scan: {tracked}")

# Force a relocation. If the pattern does not change where things belong, the renamer has nothing to
# do and the audit proves nothing about the hazards.
print(f"  setting folder naming pattern to {PATTERN!r} to force a relocation")
_s, settings = call("/configuration/settings")
if isinstance(settings, dict) and settings:
    settings["folderNamingPattern"] = PATTERN
    call("/configuration/settings", settings, "POST")

status, preview = call("/library/rename/preview", {"audiobookIds": added}, "POST")
previews = (preview if isinstance(preview, list)
            else (preview.get("previews") or preview.get("items") or []))
changed = [p for p in previews if p.get("folderChanged") or p.get("hasChanges")]
print(f"  rename preview: HTTP {status}, {len(previews)} plan(s), {len(changed)} with changes")

operations = []
for plan in previews:
    file_ops = [
        {"fileId": f.get("fileId"), "currentPath": f.get("currentPath") or "",
         "newPath": f.get("newPath") or ""}
        for f in (plan.get("fileRenames") or [])
        if f.get("newPath")
    ]
    if plan.get("newFolderPath") or file_ops:
        operations.append({
            "audiobookId": plan.get("audiobookId"),
            "newFolderPath": plan.get("newFolderPath"),
            "fileRenames": file_ops,
        })

print(f"  executing {len(operations)} rename operation(s)")
status, result = call("/library/rename", {"operations": operations}, "POST")
print(f"  execute: HTTP {status}")
if isinstance(result, dict):
    for key in ("succeeded", "failed", "message"):
        if key in result:
            print(f"    {key}: {str(result[key])[:160]}")
time.sleep(6)

total_files = len(manifest["entries"])
coverage = (tracked / total_files * 100) if total_files else 0.0

with open(RESULT_OUT, "w") as handle:
    json.dump({
        "books_added": len(added), "tracked_files": tracked, "total_files": total_files,
        "coverage_pct": round(coverage, 1),
        "plans": len(previews), "plans_with_changes": len(changed),
        "operations_executed": len(operations), "execute_status": status,
    }, handle, indent=2)

# How much was actually exercised decides how much a clean audit is worth. Two runs can both come
# back "no loss, no escape" while one moved most of the corpus and the other moved a fraction of it,
# and reporting them identically would invite exactly the wrong comparison.
print(f"  EXERCISED: {tracked}/{total_files} files tracked ({coverage:.0f}%), "
      f"{len(changed)} of {len(previews)} plans had changes")
if coverage < 50:
    print("  NOTE: under half the hazard files were tracked, so a clean audit below is weaker")
    print("  evidence than a run with fuller coverage. Compare coverage before comparing verdicts.")

# A rename that moved nothing is not a passing result, it is an untested one. Say so loudly here so
# the shell driver can refuse a verdict rather than reporting a clean audit.
if not operations or tracked == 0:
    print("  NOTHING WAS RENAMED: no tracked files or no operations, so the audit that follows")
    print("  would be vacuous.")
    sys.exit(3)
sys.exit(0)
