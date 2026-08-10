"""One mode of the Library Import re-listing check (Listenarr#616).

Driven by ``tools/validate_import_relisting.sh``, which starts a fresh container per mode and sets
the environment this reads. Separate file rather than a heredoc so each mode gets its own instance:
an earlier mode's import leaves tracked rows behind, and the whole question here is what the tracked
set contains.

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
ACTION = os.environ["ACTION"]
ROW_OUT = os.environ["ROW_OUT"]
SCAN_ROOT = os.environ["SCAN_ROOT"]
DEST_ROOT = os.environ["DEST_ROOT"]
SCAN_HOST = os.environ["SCAN_HOST"]
SCAN_ROOT_ID = os.environ["SCAN_ROOT_ID"]
DEST_ROOT_ID = os.environ["DEST_ROOT_ID"]
ROOT = os.environ["ROOT"]

HEADERS = {"Content-Type": "application/json", "X-Api-Key": KEY}


def call(path: str, payload=None, method: str = "GET"):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(f"{API}{path}", data=data, method=method, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=180) as response:
            return response.status, json.loads(response.read().decode() or "{}")
    except urllib.error.HTTPError as error:
        body = error.read().decode() or "{}"
        try:
            return error.code, json.loads(body)
        except json.JSONDecodeError:
            return error.code, {"raw": body}


def scan_unmatched(root_id: str) -> list[str]:
    """Run an unmatched scan on a root folder and return the file paths it reports."""
    status, body = call(f"/rootfolders/{root_id}/scan-unmatched", {}, "POST")
    job = body.get("jobId")
    if not job:
        print(f"    scan enqueue failed: HTTP {status} {str(body)[:160]}")
        return []

    for _ in range(90):
        time.sleep(2)
        _s, result = call(f"/rootfolders/unmatched-results/{job}")
        state = (result.get("status") or "").lower()
        if state in ("completed", "complete", "succeeded", "failed", "error"):
            if state in ("failed", "error"):
                print(f"    scan failed: {str(result.get('error'))[:160]}")
                return []
            paths = []
            for item in result.get("items") or []:
                # The result shape has varied across versions; take whichever path field is present
                # rather than assuming one, and fall back to any nested file list.
                for key in ("filePath", "path", "fullPath"):
                    if item.get(key):
                        paths.append(item[key])
                        break
                else:
                    for nested in item.get("files") or []:
                        if isinstance(nested, str):
                            paths.append(nested)
                        elif isinstance(nested, dict):
                            for key in ("filePath", "path", "fullPath"):
                                if nested.get(key):
                                    paths.append(nested[key])
                                    break
            return sorted(set(paths))
    print("    scan never completed")
    return []


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

print(f"\n--- action: {ACTION} ---")
if not sources:
    print("  INCONCLUSIVE: no source files in the drop folder.")
    finish({"action": ACTION, "verdict": "inconclusive", "reason": "no sources"})

# Baseline. If the drop folder's file is not reported BEFORE the import, then a later absence proves
# nothing: the check would be blind rather than the behaviour correct.
print("  baseline scan of the drop folder (before any import)")
before = scan_unmatched(SCAN_ROOT_ID)
print(f"    unmatched reported: {len(before)}")
for path in before:
    print(f"      {path}")
if not before:
    print("  INCONCLUSIVE: the drop folder's file was not reported even before importing,")
    print("  so this check cannot see what it is meant to be watching.")
    finish({"action": ACTION, "verdict": "inconclusive", "reason": "baseline empty"})

status, body = call("/library/add", {
    "metadata": metadata, "monitored": True, "autoSearch": False,
    "destinationPath": DEST_ROOT,
}, "POST")
book_id = body.get("id") or (body.get("audiobook") or {}).get("id")
if not book_id:
    print(f"  add failed: HTTP {status} {str(body)[:200]}")
    finish({"action": ACTION, "verdict": "inconclusive", "reason": "add failed"})

print(f"  importing with action={ACTION}")
status, result = call("/library/manual-import", {
    "path": SCAN_ROOT,
    "mode": "interactive",
    "action": ACTION,
    "includeCompanionFiles": True,
    "cleanupEmptySourceFolders": False,
    "items": [{"relativePath": os.path.basename(p), "fullPath": p, "matchedAudiobookId": book_id}
              for p in sources],
}, "POST")
results = result.get("results") or []
succeeded = [r for r in results if r.get("success")]
errors = [str(r.get("error")) for r in results if not r.get("success")]
print(f"    {len(succeeded)}/{len(results)} file(s) imported")
for message in errors:
    print(f"    failed: {message[:160]}")
if not succeeded:
    if not results:
        # No result rows at all, rather than rows that failed. The usual cause is an action string
        # the enum does not recognise, which is accepted quietly instead of rejected.
        print(f"  INCONCLUSIVE: the import returned no result rows (HTTP {status}). Check that")
        print(f"  '{ACTION}' is a FileAction name; 'hardlink' is spelled 'hardlink/copy'.")
    else:
        print("  INCONCLUSIVE: nothing imported, so there is nothing to re-list.")
    finish({"action": ACTION, "verdict": "inconclusive", "reason": "import failed",
            "errors": errors})

time.sleep(3)
source_survives = [p for p in sources
                   if os.path.isfile(os.path.join(SCAN_HOST, os.path.basename(p)))]
print(f"    source file still on disk after import: {bool(source_survives)}")

# The library root is the control. Its file IS tracked, so a correct filter drops it. If it shows up
# here, the tracked-path filter is not working at all and nothing else in this run means much.
print("  control scan of the library root (its file is tracked, so it should report nothing)")
control = scan_unmatched(DEST_ROOT_ID)
print(f"    unmatched reported: {len(control)}")
for path in control:
    print(f"      {path}")

print("  re-scan of the drop folder (after the import)")
after = scan_unmatched(SCAN_ROOT_ID)
print(f"    unmatched reported: {len(after)}")
for path in after:
    print(f"      {path}")

row = {"action": ACTION, "before": before, "after": after, "control": control,
       "source_survives": bool(source_survives)}

if control:
    print("  CHECK NOT SOUND: the library root reported a tracked file as unmatched, so the")
    print("  path filter is not working generally and the drop-folder result proves little.")
    row["verdict"] = "unsound"
    finish(row)

if not source_survives:
    # A move leaves nothing behind, so the drop folder must be empty. That is the filter working by
    # construction rather than by the tracked set, which is worth saying plainly.
    if after:
        print("  UNEXPECTED: the source is gone from disk yet still reported as unmatched.")
        row["verdict"] = "relisted"
    else:
        print("  NOT RE-LISTED: the source no longer exists, so there is nothing left to report.")
        print("  This does not exercise the tracked-path filter; it is absence, not filtering.")
        row["verdict"] = "moot"
    finish(row)

if after:
    print("  RE-LISTED: the source file survived the import and is still offered as an unmatched")
    print("  candidate, even though its contents are now in the library.")
    row["verdict"] = "relisted"
else:
    print("  FILTERED: the surviving source file is no longer offered as a candidate.")
    row["verdict"] = "filtered"
finish(row)
