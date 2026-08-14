"""Contract tests for the mutation that breaks folder/filename agreement.

The mutation exists so each candidate door into the embedded-metadata pass can be measured on its
own. That only works if the manifest keeps describing the library after the rename, because the
manifest is the answer key: every byte total and file count downstream is read from it. A mutation
that renames on disk and leaves the manifest pointing at the old path produces a run that looks
like a scan shortfall and is really just a stale answer key.

So the invariant these tests care about most is not "the rename happened". It is "every path the
manifest still claims exists on disk afterwards".
"""
from __future__ import annotations

import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

from mismatch_mutate import FILE_STEM, FOLDER_SUFFIX, main

ASIN = "B000TESTED"
OTHER = "B000OTHERX"


def build_library(tmp_path: pathlib.Path, *, files: int = 1,
                  with_other: bool = False) -> pathlib.Path:
    library = tmp_path / "library"
    entries = []
    book_dir = library / "Arthur Conan Doyle" / "1914 - The Valley of Fear"
    book_dir.mkdir(parents=True)
    for n in range(1, files + 1):
        name = "The Valley of Fear.m4b" if files == 1 else f"The Valley of Fear - Part {n}.m4b"
        (book_dir / name).write_bytes(b"x" * (100 * n))
        entries.append({
            "path": f"Arthur Conan Doyle/1914 - The Valley of Fear/{name}",
            "kind": "book", "belongs_to_asin": ASIN,
            "true_title": "The Valley of Fear",
        })

    if with_other:
        other_dir = library / "Rudyard Kipling" / "1899 - Stalky and Co."
        other_dir.mkdir(parents=True)
        (other_dir / "Stalky and Co..m4b").write_bytes(b"y" * 50)
        entries.append({
            "path": "Rudyard Kipling/1899 - Stalky and Co./Stalky and Co..m4b",
            "kind": "book", "belongs_to_asin": OTHER,
            "true_title": "Stalky and Co.",
        })

    (library / "manifest.json").write_text(json.dumps({"entries": entries}))
    return library


def run(library: pathlib.Path, mutation: str, asin: str = ASIN) -> int:
    return main(["--library", str(library), "--asin", asin, "--mutation", mutation])


def manifest_paths(library: pathlib.Path, asin: str = ASIN) -> list[str]:
    data = json.loads((library / "manifest.json").read_text())
    return [e["path"] for e in data["entries"] if e.get("belongs_to_asin") == asin]


def assert_manifest_matches_disk(library: pathlib.Path) -> None:
    """The invariant. Every path the answer key still claims has to be on disk."""
    data = json.loads((library / "manifest.json").read_text())
    for entry in data["entries"]:
        assert (library / entry["path"]).is_file(), f"manifest points at a missing {entry['path']}"


# --------------------------------------------------------------------------
# Each mutation does what it says
# --------------------------------------------------------------------------

def test_file_mutation_renames_the_audio_and_keeps_the_folder(tmp_path):
    library = build_library(tmp_path)
    assert run(library, "file") == 0
    path = manifest_paths(library)[0]
    assert path.endswith(f"{FILE_STEM}01.m4b")
    assert "1914 - The Valley of Fear/" in path
    assert_manifest_matches_disk(library)


def test_folder_mutation_renames_the_directory_and_keeps_the_filename(tmp_path):
    library = build_library(tmp_path)
    assert run(library, "folder") == 0
    path = manifest_paths(library)[0]
    assert f"1914 - The Valley of Fear{FOLDER_SUFFIX}/" in path
    assert path.endswith("The Valley of Fear.m4b")
    assert_manifest_matches_disk(library)


def test_both_applies_each_of_them(tmp_path):
    library = build_library(tmp_path)
    assert run(library, "both") == 0
    path = manifest_paths(library)[0]
    assert f"1914 - The Valley of Fear{FOLDER_SUFFIX}/{FILE_STEM}01.m4b" in path
    assert_manifest_matches_disk(library)


def test_none_changes_nothing(tmp_path):
    library = build_library(tmp_path)
    before = manifest_paths(library)
    assert run(library, "none") == 0
    assert manifest_paths(library) == before
    assert_manifest_matches_disk(library)


# --------------------------------------------------------------------------
# The answer key stays honest
# --------------------------------------------------------------------------

def test_multi_file_book_keeps_every_path_resolvable(tmp_path):
    """Renaming several files in one folder must not collide or orphan an entry."""
    library = build_library(tmp_path, files=3)
    assert run(library, "both") == 0
    paths = manifest_paths(library)
    assert len(paths) == 3
    assert len(set(paths)) == 3, "two entries were renamed onto the same path"
    assert_manifest_matches_disk(library)


def test_other_books_are_left_alone(tmp_path):
    """The mutation is scoped to one book; a sibling book must survive untouched."""
    library = build_library(tmp_path, with_other=True)
    assert run(library, "both") == 0
    assert manifest_paths(library, OTHER) == [
        "Rudyard Kipling/1899 - Stalky and Co./Stalky and Co..m4b"]
    assert_manifest_matches_disk(library)


def test_folder_mutation_moves_the_directory_not_a_copy(tmp_path):
    library = build_library(tmp_path)
    run(library, "folder")
    author = library / "Arthur Conan Doyle"
    assert not (author / "1914 - The Valley of Fear").exists()
    assert (author / f"1914 - The Valley of Fear{FOLDER_SUFFIX}").is_dir()


# --------------------------------------------------------------------------
# Refusals, so a broken run is not mistaken for a mutated one
# --------------------------------------------------------------------------

def test_unknown_asin_is_refused(tmp_path):
    library = build_library(tmp_path)
    assert run(library, "both", asin="B000NOTHER") == 2


def test_missing_manifest_is_refused(tmp_path):
    library = build_library(tmp_path)
    (library / "manifest.json").unlink()
    assert run(library, "both") == 2


def test_folder_mutation_on_a_root_level_file_is_refused(tmp_path):
    """A loose file has no book directory to rename, and silently doing nothing would read as
    a mutation that was applied."""
    library = tmp_path / "library"
    library.mkdir()
    (library / "loose.m4b").write_bytes(b"x" * 10)
    (library / "manifest.json").write_text(json.dumps({"entries": [
        {"path": "loose.m4b", "kind": "book", "belongs_to_asin": ASIN}]}))
    with pytest.raises(SystemExit):
        run(library, "folder")
