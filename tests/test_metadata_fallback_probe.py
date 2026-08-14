"""Contract tests for the embedded-metadata fallback verdict.

This check exists to say one thing: a correctly tagged file in a folder shape the path heuristics
do not recognise was not claimed. The ways it could say that wrongly all matter more than the
arithmetic:

* false FAIL via absence   — nothing was generated, so nothing was claimed, and "unclaimed" gets
                             printed as though it were a finding about the server
* false PASS via absence   — the server had no ffprobe at all, so the metadata pass never ran and
                             its failure was never actually tested
* false PASS on partial    — some of a multi-file book was claimed and the shortfall is rounded up
                             to success
* size masking the claim   — a row exists, so the claim passes, while the bytes it recorded came
                             from somewhere other than the file

The last one is why the size is reported beside the verdict rather than folded into it. A row
carrying the length of a descriptor symlink instead of the file is a real defect, and letting it
decide the claim verdict would either hide it or hide the claim.
"""
from __future__ import annotations

import json
import pathlib
import sqlite3
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

from metadata_fallback_probe import (
    EXIT_FAIL,
    EXIT_INCONCLUSIVE,
    EXIT_PASS,
    FallbackReport,
    ProbeEvidence,
    main,
    read_log,
)

ASIN = "B000TESTED"

REFUSAL_LOG = (
    "warn: Listenarr.Infrastructure.Ffmpeg.FfmpegService\n"
    "      Unable to extract metadata using ffprobe; using public filename metadata\n"
    "      Listenarr.Infrastructure.Ffmpeg.FfmpegException: ffprobe target is not a "
    "supported audio file: 399\n"
)
SUCCESS_LOG = (
    "info: Listenarr.Infrastructure.Ffmpeg.FfmpegService\n"
    "      ffprobe exit code 0 for file The Valley of Fear.m4b; stderr length=0\n"
)
NO_FFPROBE_LOG = (
    "info: Listenarr.Application.Metadata.Extraction.MetadataService\n"
    "      No bundled ffprobe available at configured location; skipping ffprobe for "
    "file: The Valley of Fear.m4b\n"
)


def evidence(**kwargs) -> ProbeEvidence:
    base = dict(ffprobe_missing=False, refusals=[], probe_runs=[],
                enrichment_exceptions=0)
    base.update(kwargs)
    return ProbeEvidence(**base)


def report(claimed_files: int = 0, on_disk_files: int = 1,
           claimed_bytes: int | None = None, on_disk_bytes: int = 2416,
           mode: str = "fallback", ev: ProbeEvidence | None = None) -> FallbackReport:
    return FallbackReport(
        label="test", mode=mode, layout="title-only", asin=ASIN,
        title="A Book", tag_state="correct-with-asin",
        on_disk_files=on_disk_files, on_disk_bytes=on_disk_bytes,
        claimed_files=claimed_files, claimed_bytes=claimed_bytes,
        evidence=ev if ev is not None else evidence(refusals=["399"]),
    )


# --------------------------------------------------------------------------
# The verdict itself
# --------------------------------------------------------------------------

def test_unclaimed_file_fails():
    assert report(claimed_files=0).verdict == "unclaimed"
    assert report(claimed_files=0).exit_code == EXIT_FAIL


def test_claimed_file_passes():
    r = report(claimed_files=1, claimed_bytes=2416,
               ev=evidence(probe_runs=["book.m4b"]))
    assert r.verdict == "claimed"
    assert r.exit_code == EXIT_PASS


def test_partial_claim_is_not_a_pass():
    """Two of three files claimed is a failure, not a success with a caveat."""
    r = report(claimed_files=2, on_disk_files=3, claimed_bytes=1600)
    assert r.verdict == "partial"
    assert r.exit_code == EXIT_FAIL


def test_nothing_generated_is_inconclusive_not_a_finding():
    """No files on disk means the run says nothing about the server."""
    r = report(claimed_files=0, on_disk_files=0)
    assert r.verdict == "inconclusive"
    assert r.exit_code == EXIT_INCONCLUSIVE


def test_absent_ffprobe_is_inconclusive_not_a_finding():
    """Without ffprobe the metadata pass returns before probing, so it was never tested."""
    r = report(claimed_files=0, ev=evidence(ffprobe_missing=True))
    assert r.verdict == "inconclusive"
    assert r.exit_code == EXIT_INCONCLUSIVE


# --------------------------------------------------------------------------
# Reading the server's own account of what happened
# --------------------------------------------------------------------------

def test_refusal_on_a_bare_integer_is_recognised_as_a_descriptor(tmp_path):
    log = tmp_path / "server.log"
    log.write_text(REFUSAL_LOG)
    ev = read_log(log)
    assert ev.refusals == ["399"]
    assert ev.refused_on_descriptor
    assert not ev.ffprobe_missing


def test_refusal_on_a_real_filename_is_not_a_descriptor(tmp_path):
    """A refusal naming an actual file is a different fault and must not be relabelled."""
    log = tmp_path / "server.log"
    log.write_text(REFUSAL_LOG.replace(": 399", ": cover.jpg"))
    ev = read_log(log)
    assert ev.refusals == ["cover.jpg"]
    assert not ev.refused_on_descriptor


def test_successful_probe_is_read_as_a_run(tmp_path):
    log = tmp_path / "server.log"
    log.write_text(SUCCESS_LOG)
    ev = read_log(log)
    assert ev.probe_runs
    assert not ev.refusals


def test_missing_ffprobe_is_detected(tmp_path):
    log = tmp_path / "server.log"
    log.write_text(NO_FFPROBE_LOG)
    assert read_log(log).ffprobe_missing


def test_absent_log_is_not_treated_as_evidence(tmp_path):
    ev = read_log(tmp_path / "nope.log")
    assert not ev.refusals and not ev.ffprobe_missing and not ev.probe_runs


# --------------------------------------------------------------------------
# Size, reported beside the claim rather than folded into it
# --------------------------------------------------------------------------

def test_size_of_a_descriptor_symlink_is_named_as_such():
    r = report(claimed_files=1, claimed_bytes=64, mode="control")
    assert "descriptor symlink" in r.size_note
    # It is still a claim: the row exists. The size is a separate defect.
    assert r.verdict == "claimed"
    assert r.exit_code == EXIT_PASS


def test_matching_size_says_so():
    r = report(claimed_files=1, claimed_bytes=2416, mode="control")
    assert r.size_note == "matches disk"


def test_multi_file_descriptor_sizes_are_still_recognised():
    """40 rows of 64 bytes is the same defect as one row of 64, not an unrelated total."""
    r = report(claimed_files=40, on_disk_files=40, claimed_bytes=2560,
               on_disk_bytes=223520, mode="control")
    assert "descriptor symlink" in r.size_note


def test_wrong_size_that_is_not_a_descriptor_is_reported_plainly():
    r = report(claimed_files=1, claimed_bytes=1234, mode="control")
    assert "1,234 recorded against 2,416 on disk" == r.size_note


def test_unclaimed_file_has_no_size_to_judge():
    assert report(claimed_files=0).size_note == "n/a"


# --------------------------------------------------------------------------
# End to end through main()
# --------------------------------------------------------------------------

def write_fixture(tmp_path: pathlib.Path, *, claimed: bool, size: int | None,
                  log_text: str) -> dict:
    library = tmp_path / "library"
    (library / "A Book").mkdir(parents=True)
    audio = library / "A Book" / "A Book.m4b"
    audio.write_bytes(b"x" * 2416)

    manifest = library / "manifest.json"
    manifest.write_text(json.dumps({
        "entries": [{
            "path": "A Book/A Book.m4b", "kind": "book", "belongs_to_asin": ASIN,
            "true_title": "A Book", "layout": "title-only",
            "tag_state": "correct-with-asin",
        }]
    }))

    db = tmp_path / "listenarr.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE AudiobookFiles (Id INTEGER PRIMARY KEY, "
                 "AudiobookId INTEGER, Path TEXT, Size INTEGER)")
    if claimed:
        conn.execute("INSERT INTO AudiobookFiles (AudiobookId, Path, Size) VALUES (1, ?, ?)",
                     (str(audio), size))
    conn.commit()
    conn.close()

    log = tmp_path / "server.log"
    log.write_text(log_text)
    return {"library": library, "manifest": manifest, "db": db, "log": log}


def run_main(fx: dict, mode: str, json_out: pathlib.Path | None = None) -> int:
    argv = ["--manifest", str(fx["manifest"]), "--library", str(fx["library"]),
            "--db", str(fx["db"]), "--book-id", "1", "--asin", ASIN,
            "--mode", mode, "--log", str(fx["log"])]
    if json_out:
        argv += ["--json", str(json_out)]
    return main(argv)


def test_main_fails_when_the_fallback_did_not_claim(tmp_path, capsys):
    fx = write_fixture(tmp_path, claimed=False, size=None, log_text=REFUSAL_LOG)
    assert run_main(fx, "fallback") == EXIT_FAIL
    out = capsys.readouterr().out
    assert "UNCLAIMED" in out
    assert "REFUSED on a bare descriptor name: 399" in out
    # The refusal never surfaced as a scan issue, and the report has to say so.
    assert "diagnostic NONE" in out


def test_main_passes_when_the_fallback_claimed(tmp_path, capsys):
    fx = write_fixture(tmp_path, claimed=True, size=2416, log_text=SUCCESS_LOG)
    assert run_main(fx, "fallback") == EXIT_PASS
    out = capsys.readouterr().out
    assert "CLAIMED" in out
    assert "matches disk" in out


def test_main_reports_a_claimed_row_carrying_a_descriptor_size(tmp_path, capsys):
    fx = write_fixture(tmp_path, claimed=True, size=64, log_text=SUCCESS_LOG)
    assert run_main(fx, "control") == EXIT_PASS
    assert "descriptor symlink" in capsys.readouterr().out


def test_main_writes_json_carrying_the_verdict(tmp_path):
    fx = write_fixture(tmp_path, claimed=False, size=None, log_text=REFUSAL_LOG)
    out = tmp_path / "result.json"
    run_main(fx, "fallback", json_out=out)
    payload = json.loads(out.read_text())
    assert payload["verdict"] == "unclaimed"
    assert payload["claimed_files"] == 0
    assert payload["on_disk_bytes"] == 2416


def test_main_is_inconclusive_without_a_database(tmp_path):
    fx = write_fixture(tmp_path, claimed=False, size=None, log_text=REFUSAL_LOG)
    fx["db"].unlink()
    assert run_main(fx, "fallback") == EXIT_INCONCLUSIVE
