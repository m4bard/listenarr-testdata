"""The fixture set's contract.

These fixtures are handed to someone else to write tests against, so the thing that must not
break is the correspondence between what the manifest claims and what is actually embedded in
the files. A fixture set that quietly stops carrying an identifier would turn their passing
test into a test of nothing.

The ffprobe-backed checks are skipped when ffmpeg is unavailable rather than silently passing,
because "we could not look" and "we looked and it was fine" are different results.
"""
from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "tools"))

from make_tag_fixtures import CASES, FixtureError, build, main, pick_books

ROOT = pathlib.Path(__file__).resolve().parents[1]
HAS_FFMPEG = shutil.which("ffmpeg") is not None


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> tuple[pathlib.Path, dict]:
    if not HAS_FFMPEG:
        pytest.skip("ffmpeg is required to synthesize the fixture audio")
    out = tmp_path_factory.mktemp("fixtures") / "tag-adoption"
    manifest = build(out, ROOT / "build" / "ffmpeg-cache", "ffmpeg")
    return out, manifest


def probe_identifiers(path: pathlib.Path) -> dict[str, str]:
    """Every identifier-ish tag ffprobe surfaces, from format and stream scopes alike."""
    raw = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams",
         str(path)],
        capture_output=True, text=True, check=True,
    ).stdout
    doc = json.loads(raw)
    found: dict[str, str] = {}
    for scope in ("format", "streams"):
        value = doc.get(scope)
        for section in value if isinstance(value, list) else [value] if value else []:
            for key, val in (section.get("tags") or {}).items():
                if "asin" in key.lower() or "cdek" in key.lower():
                    found[key] = val
    return found


class TestCorpusDiscipline:
    def test_every_asin_used_is_real(self) -> None:
        """No invented identifiers. Each one must resolve in the committed corpus."""
        corpus = json.loads((ROOT / "corpus" / "corpus.json").read_text())["books"]
        known = {book["asin"] for book in corpus}
        for book in pick_books(corpus, 2):
            assert book["asin"] in known

    def test_the_two_books_are_distinct(self) -> None:
        """The disagree case is meaningless if both files claim the same book."""
        corpus = json.loads((ROOT / "corpus" / "corpus.json").read_text())["books"]
        first, second = pick_books(corpus, 2)
        assert first["asin"] != second["asin"]

    def test_selection_is_deterministic(self) -> None:
        corpus = json.loads((ROOT / "corpus" / "corpus.json").read_text())["books"]
        first = [b["asin"] for b in pick_books(corpus, 2)]
        assert first == [b["asin"] for b in pick_books(corpus, 2)]

    def test_refuses_a_corpus_too_small_to_disagree(self) -> None:
        with pytest.raises(FixtureError):
            pick_books([{"asin": "B000000001", "title": "only one"}], 2)


@pytest.mark.contract
class TestManifestMatchesTheFiles:
    def test_every_case_directory_exists(self, built: tuple[pathlib.Path, dict]) -> None:
        out, manifest = built
        assert len(manifest["cases"]) == len(CASES)
        for case in manifest["cases"]:
            assert (out / case["key"]).is_dir()

    def test_tagged_files_really_carry_that_identifier(
        self, built: tuple[pathlib.Path, dict]
    ) -> None:
        """The claim under test: if the manifest says a file carries an ASIN, ffprobe finds it."""
        out, manifest = built
        for case in manifest["cases"]:
            for entry in case["files"]:
                found = probe_identifiers(out / case["key"] / entry["file"])
                if entry["asin"] is None:
                    assert not found, f"{case['key']}/{entry['file']} should carry nothing"
                else:
                    assert found, f"{case['key']}/{entry['file']} carries no identifier"
                    assert set(found.values()) == {entry["asin"]}

    def test_mixed_dialect_case_really_uses_different_spellings(
        self, built: tuple[pathlib.Path, dict]
    ) -> None:
        """Otherwise it is just the same-dialect case with more steps."""
        out, manifest = built
        case = next(c for c in manifest["cases"] if c["key"] == "agree-mixed-dialect")
        spellings = [
            frozenset(probe_identifiers(out / case["key"] / entry["file"]))
            for entry in case["files"]
        ]
        assert len(set(spellings)) == len(spellings)

    def test_disagree_case_really_disagrees(self, built: tuple[pathlib.Path, dict]) -> None:
        out, manifest = built
        case = next(c for c in manifest["cases"] if c["key"] == "disagree")
        values = {
            value
            for entry in case["files"]
            for value in probe_identifiers(out / case["key"] / entry["file"]).values()
        }
        assert len(values) == 2

    def test_lone_file_case_has_exactly_one_file(self, built: tuple[pathlib.Path, dict]) -> None:
        """The whole point of the case is that nothing else is present to disagree."""
        out, manifest = built
        case = next(c for c in manifest["cases"] if c["key"] == "partial-lone-file")
        present = [p for p in (out / case["key"]).iterdir() if p.is_file()]
        assert len(present) == 1


@pytest.mark.contract
class TestRebuildIsClean:
    """A second run must replace the set, not merge into it.

    This is the gap worth caring about. If a stale file survives a rebuild, `untagged` and the
    `partial-` cases quietly stop being what they claim, and a test written against them passes
    for the wrong reason.
    """

    def test_stale_files_do_not_survive_a_rebuild(self, tmp_path: pathlib.Path) -> None:
        if not HAS_FFMPEG:
            pytest.skip("ffmpeg is required to synthesize the fixture audio")
        out = tmp_path / "tag-adoption"
        build(out, ROOT / "build" / "ffmpeg-cache", "ffmpeg")

        intruder = out / "untagged" / "Part 99.m4b"
        shutil.copyfile(out / "untagged" / "Part 01.m4b", intruder)
        stray_case = out / "not-a-case"
        stray_case.mkdir()
        (stray_case / "junk.m4b").write_bytes(b"junk")

        build(out, ROOT / "build" / "ffmpeg-cache", "ffmpeg")

        assert not intruder.exists()
        assert not stray_case.exists()

    def test_rebuild_produces_the_same_manifest(self, tmp_path: pathlib.Path) -> None:
        if not HAS_FFMPEG:
            pytest.skip("ffmpeg is required to synthesize the fixture audio")
        out = tmp_path / "tag-adoption"
        first = build(out, ROOT / "build" / "ffmpeg-cache", "ffmpeg")
        second = build(out, ROOT / "build" / "ffmpeg-cache", "ffmpeg")
        assert first == second


class TestCommandLine:
    def test_reports_every_case_and_exits_zero(
        self, tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        if not HAS_FFMPEG:
            pytest.skip("ffmpeg is required to synthesize the fixture audio")
        out = tmp_path / "tag-adoption"
        monkeypatch.setattr(
            sys, "argv",
            ["make_tag_fixtures.py", "--out", str(out),
             "--cache", str(ROOT / "build" / "ffmpeg-cache")],
        )
        assert main() == 0
        printed = capsys.readouterr().out
        for case in CASES:
            assert case.key in printed

    def test_a_corpus_too_small_exits_two_not_zero(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fail closed. An unbuildable fixture set must not look like a built one."""
        monkeypatch.setattr("make_tag_fixtures.load_corpus", lambda: [])
        monkeypatch.setattr(
            sys, "argv",
            ["make_tag_fixtures.py", "--out", str(tmp_path / "out")],
        )
        with pytest.raises(FixtureError):
            main()


@pytest.mark.contract
def test_script_exits_two_when_it_cannot_build(tmp_path: pathlib.Path) -> None:
    """Run as a real script, an unbuildable set must exit 2 and say why on stderr.

    Exit 0 with no fixtures would be the damaging outcome: a caller in a pipeline would carry
    on against a directory that is not there. Covered end to end rather than by calling main(),
    because the raise-to-exit-code mapping lives in the __main__ guard.
    """
    stage = tmp_path / "stage"
    (stage / "tools").mkdir(parents=True)
    (stage / "corpus").mkdir(parents=True)
    for name in ("generate_library.py", "make_tag_fixtures.py"):
        shutil.copyfile(ROOT / "tools" / name, stage / "tools" / name)
    shutil.copyfile(ROOT / "corpus" / "cases.py", stage / "corpus" / "cases.py")
    # One book cannot produce the disagree case, which needs two distinct ASINs.
    (stage / "corpus" / "corpus.json").write_text(json.dumps({
        "books": [{
            "asin": "B002UUFXKU", "title": "Only One", "authors": ["A"],
            "narrators": [], "series": None, "series_position": None,
            "release_date": None, "region": "us",
        }]
    }))

    result = subprocess.run(
        [sys.executable, str(stage / "tools" / "make_tag_fixtures.py"),
         "--out", str(tmp_path / "out")],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert "make_tag_fixtures" in result.stderr
    assert not (tmp_path / "out").exists()
