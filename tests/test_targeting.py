"""--only-asin / --tag narrow the corpus to a minimal repro of one bug."""
from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "corpus"))

import cases
from generate_library import generate, load_corpus, select_books

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg required")


def _cli(*args: str) -> subprocess.CompletedProcess:
    # --ffmpeg-source system keeps the CLI offline (PATH ffmpeg); main() defaults to jellyfin,
    # which would download a pinned build, so the tests pin it to the no-download path explicitly.
    return subprocess.run(
        [sys.executable, str(ROOT / "tools" / "generate_library.py"),
         "--ffmpeg-source", "system", *args],
        capture_output=True, text=True,
    )


def test_select_by_asin_case_insensitive() -> None:
    got = select_books(load_corpus(), only_asins=["b00cq5waxw"])
    assert [b["asin"] for b in got] == ["B00CQ5WAXW"]


def test_select_by_tag_matches_any_of_them() -> None:
    got = select_books(load_corpus(), only_tags=["title-collision", "numeral"])
    assert got
    assert all({"title-collision", "numeral"} & set(b.get("tags", [])) for b in got)


def test_asin_and_tag_are_anded() -> None:
    corpus = load_corpus()
    got = select_books(corpus, only_asins=[corpus[0]["asin"]], only_tags=["definitely-not-a-tag"])
    assert got == []


def test_empty_selection_raises_before_generating(tmp_path: pathlib.Path) -> None:
    # A contract test: an impossible filter must fail loudly, not quietly produce an empty library.
    with pytest.raises(ValueError):
        generate(
            cases.SCENARIOS_BY_KEY["existing-library-adoption"], tmp_path / "x",
            seed=1, only_asins=["NOT-AN-ASIN"],
        )
    assert not (tmp_path / "x").exists()  # raised before touching disk


@needs_ffmpeg
def test_cli_only_asin_makes_a_single_book_repro(tmp_path: pathlib.Path) -> None:
    out = tmp_path / "lib"
    result = _cli("--only-asin", "B00CQ5WAXW", "--out", str(out))
    assert result.returncode == 0, result.stderr
    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["scenario"] == "existing-library-adoption"  # --only-asin alone defaults it
    book_asins = {e["belongs_to_asin"] for e in manifest["entries"] if e["kind"] == "book"}
    assert book_asins == {"B00CQ5WAXW"}


@needs_ffmpeg
def test_cli_tag_is_comma_splittable(tmp_path: pathlib.Path) -> None:
    out = tmp_path / "lib"
    result = _cli("--tag", "title-collision,numeral", "--out", str(out), "--limit", "4")
    assert result.returncode == 0, result.stderr
