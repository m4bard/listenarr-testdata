"""Contract tests for the chapter-grouping verdict.

`validate_chapter_grouping.sh` reports whether Library Import gathered a multi-file book into one
scan item. It is verdict-bearing, so the failure modes that matter are not "does it count" but:

* false PASS            - a folder exploded into one item per file and the tool reports success
* false FAIL            - the folder grouped correctly and the tool calls it exploded
* false PASS via a dead control
                        - every case behaves identically, so the check could not have distinguished
                          a filename convention from "multi-file books never group here", and it
                          reports success anyway
* false FAIL via silence
                        - the scan indexed nothing, or lost files before grouping, and the tool
                          reads the missing items as a grouping fault rather than saying it cannot
                          tell (this is the shape of Listenarr#822)

The last two are the ones that bite. A check whose control is broken has stopped measuring the
thing it names, and a check that cannot see the files must not be able to return either 0 or 1.
Inconclusive is a distinct exit code so neither can be mistaken for an answer.
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

from chapter_grouping_report import (
    EXIT_FAIL,
    EXIT_INCONCLUSIVE,
    EXIT_PASS,
    EXPLODED,
    GROUPED,
    MISSING,
    PARTIAL,
    case_of,
    judge,
    title_tags,
)

CONTAINER_ROOT = "/audiobooks"


def item(case: str, *names: str) -> dict:
    """One scan item covering the named files inside a case's book folder."""
    folder = f"{CONTAINER_ROOT}/{case}/An Author/A Book"
    sources = [f"{folder}/{name}" for name in names]
    return {
        "fullPath": sources[0],
        "sourceFiles": sources,
        "bookFolder": folder,
        "fileCount": len(sources),
    }


def write_case(library: pathlib.Path, case: str, structure: str, files: list[str],
               title: str | None) -> None:
    """Write a per-case manifest in the shape generate_library.py emits."""
    entries = [{
        "path": f"{case}/An Author/A Book/{name}",
        "kind": "book",
        "structure": structure,
        "tag_state": "no-tags" if title is None else "correct-no-asin",
        "tags_written": {} if title is None else {"title": title, "album": "A Book"},
    } for name in files]
    directory = library / case
    directory.mkdir(parents=True)
    (directory / "manifest.json").write_text(json.dumps({"entries": entries}), encoding="utf-8")


def run(library: pathlib.Path, items: list[dict], *controls: str) -> subprocess.CompletedProcess:
    items_path = library.parent / "items.json"
    items_path.write_text(json.dumps({"status": "Completed", "items": items}), encoding="utf-8")
    argv = [sys.executable, str(ROOT / "tools" / "chapter_grouping_report.py"),
            "--library", str(library), "--items", str(items_path),
            "--container-root", CONTAINER_ROOT]
    for control in controls:
        argv += ["--control", control]
    return subprocess.run(argv, capture_output=True, text=True)


@pytest.fixture
def library(tmp_path: pathlib.Path) -> pathlib.Path:
    """A control that groups and a subject that is free to do either."""
    root = tmp_path / "library"
    root.mkdir()
    write_case(root, "ctl", "multi-part",
               ["A Book - Part 01.mp3", "A Book - Part 02.mp3"], None)
    write_case(root, "sub", "paren-index",
               ["A Book (1).mp3", "A Book (2).mp3"], None)
    return root


def grouped_items() -> list[dict]:
    return [
        item("ctl", "A Book - Part 01.mp3", "A Book - Part 02.mp3"),
        item("sub", "A Book (1).mp3", "A Book (2).mp3"),
    ]


def exploded_subject_items() -> list[dict]:
    return [
        item("ctl", "A Book - Part 01.mp3", "A Book - Part 02.mp3"),
        item("sub", "A Book (1).mp3"),
        item("sub", "A Book (2).mp3"),
    ]


# --- the verdict itself -----------------------------------------------------------------

def test_one_item_covering_every_file_is_grouped(library):
    result = run(library, grouped_items(), "ctl")
    assert result.returncode == EXIT_PASS, result.stdout
    assert "PASS" in result.stdout


def test_one_item_per_file_is_a_failure(library):
    result = run(library, exploded_subject_items(), "ctl")
    assert result.returncode == EXIT_FAIL, result.stdout
    assert "exploded" in result.stdout


def test_a_broken_control_cannot_report_a_failure(library):
    """The subject exploded, but so did the control, so the run proves nothing about names."""
    items = [
        item("ctl", "A Book - Part 01.mp3"),
        item("ctl", "A Book - Part 02.mp3"),
        item("sub", "A Book (1).mp3"),
        item("sub", "A Book (2).mp3"),
    ]
    result = run(library, items, "ctl")
    assert result.returncode == EXIT_INCONCLUSIVE, result.stdout
    assert "control" in result.stdout


def test_a_control_that_matches_nothing_is_inconclusive(library):
    result = run(library, grouped_items(), "no-such-case")
    assert result.returncode == EXIT_INCONCLUSIVE, result.stdout


# --- coverage is checked before grouping ------------------------------------------------

def test_a_scan_that_indexed_nothing_is_inconclusive_not_a_grouping_failure(library):
    result = run(library, [], "ctl")
    assert result.returncode == EXIT_INCONCLUSIVE, result.stdout
    assert "822" in result.stdout


def test_files_lost_before_grouping_are_inconclusive(library):
    result = run(library, [item("sub", "A Book (1).mp3", "A Book (2).mp3")], "ctl")
    assert result.returncode == EXIT_INCONCLUSIVE, result.stdout
    assert "2 of 4" in result.stdout


def test_an_empty_library_cannot_report_a_pass(tmp_path):
    empty = tmp_path / "library"
    empty.mkdir()
    result = run(empty, grouped_items(), "ctl")
    assert result.returncode == EXIT_INCONCLUSIVE, result.stdout


# --- classification units ---------------------------------------------------------------

def test_judge_names_each_outcome():
    case = {"case": "sub", "structure": "paren-index", "tag_state": "no-tags",
            "title_tags": "none", "files_on_disk": 4, "example": "A Book (1).mp3"}
    assert judge(case, [item("sub", *[f"A Book ({n}).mp3" for n in range(1, 5)])])[
        "observed"] == GROUPED
    assert judge(case, [item("sub", f"A Book ({n}).mp3") for n in range(1, 5)])[
        "observed"] == EXPLODED
    assert judge(case, [item("sub", f"A Book ({n}).mp3") for n in range(1, 3)])[
        "observed"] == PARTIAL
    assert judge(case, [])["observed"] == MISSING


def test_one_item_that_covers_only_some_files_is_partial_not_grouped():
    """An item count of one is not enough; it has to account for the whole folder."""
    case = {"case": "sub", "structure": "paren-index", "tag_state": "no-tags",
            "title_tags": "none", "files_on_disk": 4, "example": "A Book (1).mp3"}
    assert judge(case, [item("sub", "A Book (1).mp3")])["observed"] == PARTIAL


def test_case_of_reads_the_top_level_directory_under_the_root():
    assert case_of("/audiobooks/sub/An Author/A Book/x.mp3", "/audiobooks") == "sub"
    assert case_of("/audiobooks/sub/An Author/A Book/x.mp3", "/audiobooks/") == "sub"
    assert case_of("/elsewhere/sub/x.mp3", "/audiobooks") == ""


def test_title_tags_separates_absent_shared_and_per_file_titles():
    assert title_tags([{"tags_written": {}}, {"tags_written": {}}]) == "none"
    assert title_tags([{"tags_written": {"title": "A Book"}},
                       {"tags_written": {"title": "A Book"}}]) == "book title"
    assert title_tags([{"tags_written": {"title": "Chapter 1"}},
                       {"tags_written": {"title": "Chapter 2"}}]) == "per-chapter"
