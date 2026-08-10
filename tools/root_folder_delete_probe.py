"""One scenario of the root-folder deletion check (Listenarr#602).

Driven by ``tools/validate_root_folder_delete.sh``, which starts a fresh container per scenario and
sets the environment this reads. Separate file rather than a heredoc because each scenario needs its
own database: root folder rows and audiobook BasePaths are exactly what is under test, so carrying
them between scenarios would invalidate the result.

Writes one result row as JSON to ``$ROW_OUT`` and prints the reasoning to stdout.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API = os.environ["API"]
KEY = os.environ["KEY"]
ASIN = os.environ["ASIN"]
SCENARIO = os.environ["SCENARIO"]
ROW_OUT = os.environ["ROW_OUT"]
ROOT = os.environ["ROOT"]
DATA_HOST = os.environ["DATA_HOST"]

HEADERS = {"Content-Type": "application/json", "X-Api-Key": KEY}


def call(path: str, payload=None, method: str = "GET"):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(f"{API}{path}", data=data, method=method, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=120) as response:
            body = response.read().decode()
            return response.status, (json.loads(body) if body.strip() else {})
    except urllib.error.HTTPError as error:
        body = error.read().decode() or "{}"
        try:
            return error.code, json.loads(body)
        except json.JSONDecodeError:
            return error.code, {"raw": body}


def make_root(name: str, path: str, default: bool = False) -> int | None:
    status, body = call("/rootfolders", {
        "name": name, "path": path, "isDefault": default, "caseSensitivityMode": "Sensitive",
    }, "POST")
    if status >= 400:
        print(f"    could not create root '{name}' at {path}: HTTP {status} {str(body)[:140]}")
        return None
    return body.get("id") or (body.get("rootFolder") or {}).get("id")


def base_path_of(book_id: int) -> str | None:
    _s, body = call(f"/library/{book_id}")
    return body.get("basePath") or (body.get("audiobook") or {}).get("basePath")


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

print(f"\n--- scenario: {SCENARIO} ---")
row: dict = {"scenario": SCENARIO}

if SCENARIO == "sibling":
    # The control. Two roots that do NOT nest, with the book under one of them. Deleting the other,
    # which owns nothing, must succeed. If this fails too then the delete gate is broken generally
    # and the nested result below would say nothing specific about nesting.
    owner = make_root("alpha", "/audiobooks/alpha", default=True)
    empty = make_root("beta", "/audiobooks/beta")
    if not owner or not empty:
        finish({**row, "verdict": "inconclusive", "reason": "root creation failed"})
    print(f"  roots: alpha (owner) id={owner}, beta (empty) id={empty}, not nested")

    status, body = call("/library/add", {
        "metadata": metadata, "monitored": True, "autoSearch": False,
        "destinationPath": "/audiobooks/alpha",
    }, "POST")
    book_id = body.get("id") or (body.get("audiobook") or {}).get("id")
    if not book_id:
        finish({**row, "verdict": "inconclusive", "reason": f"add failed HTTP {status}"})
    print(f"  book BasePath: {base_path_of(book_id)!r}")

    status, body = call(f"/rootfolders/{empty}", None, "DELETE")
    print(f"  DELETE the empty sibling root -> HTTP {status} {str(body.get('message', ''))[:90]}")
    row.update({"delete_status": status})
    if status < 400:
        print("  CONTROL PASSED: an unrelated empty root deletes cleanly.")
        row["verdict"] = "control-ok"
    else:
        print("  CONTROL FAILED: even a non-nested empty root refuses to delete, so the gate is")
        print("  broken more generally than nesting and the nested result proves nothing specific.")
        row["verdict"] = "control-failed"
    finish(row)

# Both remaining scenarios need the nested arrangement: an outer root that CONTAINS an inner root,
# with every book living under the inner one. The outer therefore owns nothing of its own.
outer = make_root("outer", "/audiobooks", default=False)
inner = make_root("inner", "/audiobooks/inner", default=True)
if not outer or not inner:
    finish({**row, "verdict": "inconclusive", "reason": "root creation failed"})
print(f"  roots: outer=/audiobooks id={outer}, inner=/audiobooks/inner id={inner}")
print("  the API accepted a root nested inside another root")
row["nesting_accepted"] = True

status, body = call("/library/add", {
    "metadata": metadata, "monitored": True, "autoSearch": False,
    "destinationPath": "/audiobooks/inner",
}, "POST")
book_id = body.get("id") or (body.get("audiobook") or {}).get("id")
if not book_id:
    finish({**row, "verdict": "inconclusive", "reason": f"add failed HTTP {status}"})

sources = sorted(
    f"/audiobooks/inner/{f}"
    for f in os.listdir(os.path.join(DATA_HOST, "inner"))
    if f.endswith(".m4b")
)
if sources:
    call("/library/manual-import", {
        "path": "/audiobooks/inner", "mode": "interactive", "action": "copy",
        "includeCompanionFiles": True, "cleanupEmptySourceFolders": False,
        "items": [{"relativePath": os.path.basename(p), "fullPath": p,
                   "matchedAudiobookId": book_id} for p in sources],
    }, "POST")
    time.sleep(2)

before = base_path_of(book_id)
print(f"  book BasePath: {before!r}  (under the INNER root, not the outer)")
row["base_path_before"] = before

if SCENARIO == "nested-delete":
    status, body = call(f"/rootfolders/{outer}", None, "DELETE")
    message = str(body.get("message", ""))[:120]
    print(f"  DELETE the outer root -> HTTP {status} {message}")
    row.update({"delete_status": status, "message": message})

    if status >= 400:
        print("  REPRODUCED #602: the outer root owns no books of its own, yet it cannot be")
        print("  deleted, because the gate matches any BasePath that merely starts with its path")
        print("  and the inner root's books sit underneath it.")
        row["verdict"] = "reproduced"
    else:
        print("  NOT REPRODUCED: the outer root deleted despite containing the inner root.")
        row["verdict"] = "deleted"
    finish(row)

if SCENARIO == "nested-reassign":
    # The documented escape hatch. Deleting with reassignTo is supposed to move the affected books
    # to another root first. With nested roots the "affected" set includes books that already live
    # under the destination, so the question is what their stored path becomes.
    status, body = call(f"/rootfolders/{outer}?reassignTo={inner}", None, "DELETE")
    message = str(body.get("message", ""))[:120]
    print(f"  DELETE outer?reassignTo=inner -> HTTP {status} {message}")
    time.sleep(2)
    after = base_path_of(book_id)
    print(f"  book BasePath after : {after!r}")
    row.update({"delete_status": status, "message": message, "base_path_after": after})

    on_disk = after is not None and os.path.isdir(
        os.path.join(DATA_HOST, after.replace("/audiobooks/", "", 1))
    )
    print(f"  that path exists on disk: {on_disk}")
    row["path_exists_on_disk"] = on_disk

    # A wrong row in the database is only worth reporting if the user can feel it. Ask the library
    # what it now thinks of this book, so the finding is a consequence rather than an observation.
    _s, listing = call("/library")
    items = (listing if isinstance(listing, list)
             else (listing.get("items") or listing.get("audiobooks") or []))
    entry = next((i for i in items if i.get("id") == book_id), None)
    if entry is not None:
        status_text = entry.get("status")
        print(f"  the library now reports this book as: {status_text!r}")
        row["library_status_after"] = status_text
    files_on_disk = sum(
        len([f for f in filenames if f.endswith(".m4b")])
        for _dirpath, _dirnames, filenames in os.walk(DATA_HOST)
    )
    print(f"  audio files still present on disk: {files_on_disk}")
    row["files_on_disk_after"] = files_on_disk

    if after and before and after != before and not on_disk:
        print("  PATH CORRUPTED: reassigning rewrote the stored path to somewhere that does not")
        print("  exist. The book was already under the destination root, so the suffix it kept")
        print("  from the outer root got prepended a second time.")
        print("  Note what this does NOT do: the files are still on disk and still tracked, and")
        print("  the book's status is unchanged, so nothing is visibly broken yet. The damage is")
        print("  latent, in that anything later relying on BasePath now points at a directory")
        print("  that is not there.")
        row["verdict"] = "corrupted"
    elif after == before:
        print("  UNCHANGED: the stored path survived the reassign.")
        row["verdict"] = "unchanged"
    elif on_disk:
        print("  REWRITTEN BUT VALID: the path changed and still points at something real.")
        row["verdict"] = "rewritten-valid"
    else:
        print("  INCONCLUSIVE: could not judge the resulting path.")
        row["verdict"] = "inconclusive"
    finish(row)

finish({**row, "verdict": "inconclusive", "reason": f"unknown scenario {SCENARIO}"})
