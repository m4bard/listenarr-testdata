"""End-to-end generation: real ffmpeg audio, real embedded tags, a manifest that cannot lie.

These are the tests that would catch a regression a unit test cannot see — because they
inspect the tree that actually lands on disk, and read the tags back with the same tool
(ffprobe) that the scanner under test uses.
"""
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
from generate_library import generate

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe are required to synthesize and read back audio",
)

LIMIT = 12  # enough books to cross every axis; small enough to keep the suite quick


def build(scenario_key: str, out: pathlib.Path, seed: int = 1,
          limit: int | None = LIMIT) -> dict:
    return generate(cases.SCENARIOS_BY_KEY[scenario_key], out, seed, limit)


def ffprobe_tags(path: pathlib.Path) -> dict[str, str]:
    """Read tags exactly the way the scanner under test reads them."""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format_tags",
         "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    )
    tags: dict[str, str] = json.loads(result.stdout).get("format", {}).get("tags", {})
    return tags


class TestDeterminism:
    def test_the_same_seed_produces_a_byte_identical_tree(self, tmp_path: pathlib.Path) -> None:
        first, second = tmp_path / "a", tmp_path / "b"
        build("mixed-reality", first)
        build("mixed-reality", second)

        files_a = sorted(p.relative_to(first) for p in first.rglob("*") if p.is_file())
        files_b = sorted(p.relative_to(second) for p in second.rglob("*") if p.is_file())
        assert files_a == files_b

        for rel in files_a:
            assert (first / rel).read_bytes() == (second / rel).read_bytes(), \
                f"{rel} differs between two runs at the same seed"

    def test_a_different_seed_produces_a_different_library(
        self, tmp_path: pathlib.Path
    ) -> None:
        # mixed-reality draws its tag states from a weighted distribution. If the seed did
        # not move the draw, --seed would be decorative.
        one = build("mixed-reality", tmp_path / "one", seed=1)
        two = build("mixed-reality", tmp_path / "two", seed=2)
        states_one = [e["tag_state"] for e in one["entries"]]
        states_two = [e["tag_state"] for e in two["entries"]]
        assert states_one != states_two


class TestManifestIsAnAnswerKey:
    @pytest.mark.parametrize("scenario", [s.key for s in cases.SCENARIOS if s.key != "scale"])
    def test_the_manifest_reconciles_exactly_with_the_disk(
        self, scenario: str, tmp_path: pathlib.Path
    ) -> None:
        # The manifest is the only reason a generated tree is evidence rather than a pile of
        # files. If it claims a file that is not there, or misses one that is, every
        # pass/fail it produces downstream is worthless.
        manifest = build(scenario, tmp_path)
        claimed = [e["path"] for e in manifest["entries"]]

        assert len(claimed) == len(set(claimed)), "a path is claimed twice"

        for rel in claimed:
            assert (tmp_path / rel).exists(), f"manifest claims {rel}, which is not on disk"

        on_disk = {
            str(p.relative_to(tmp_path))
            for p in tmp_path.rglob("*")
            if p.is_file() and p.name != "manifest.json"
        }
        assert on_disk == set(claimed), \
            f"unclaimed files on disk: {sorted(on_disk - set(claimed))[:5]}"

    def test_every_book_entry_names_a_real_corpus_book(self, tmp_path: pathlib.Path) -> None:
        manifest = build("mixed-reality", tmp_path)
        corpus = {b["asin"] for b in
                  json.loads((ROOT / "corpus" / "corpus.json").read_text())["books"]}
        for entry in manifest["entries"]:
            if entry["kind"] == "book":
                assert entry["belongs_to_asin"] in corpus

    def test_every_entry_carries_an_expected_outcome(self, tmp_path: pathlib.Path) -> None:
        manifest = build("lying-tags", tmp_path)
        for entry in manifest["entries"]:
            assert entry["expect"], f"{entry['path']} declares no expected outcome"

    def test_no_asin_is_ever_invented(self, tmp_path: pathlib.Path) -> None:
        # Every ASIN written into a tag — including the deliberately WRONG ones — must be an
        # ASIN that Audnex actually resolved.
        real = {b["asin"] for b in
                json.loads((ROOT / "corpus" / "corpus.json").read_text())["books"]}
        for scenario in ("mixed-reality", "lying-tags", "tag-dialects"):
            manifest = build(scenario, tmp_path / scenario)
            for entry in manifest["entries"]:
                for key, value in entry.get("tags_written", {}).items():
                    if "ASIN" in key.upper():
                        assert value in real, f"{value} is not a verified corpus ASIN"


class TestTagsReallyLand:
    def test_ffprobe_sees_the_asin_in_every_dialect(self, tmp_path: pathlib.Path) -> None:
        # ExtractAsin upstream carries twenty-odd spellings and nothing proves it reads them
        # all. This is what a scanner would have to find, per container.
        manifest = build("tag-dialects", tmp_path)
        seen: set[str] = set()
        for entry in manifest["entries"]:
            if entry["dialect"] == "none" or entry["kind"] != "book":
                continue
            tags = ffprobe_tags(tmp_path / entry["path"])
            upper = {k.upper(): v for k, v in tags.items()}
            assert "ASIN" in upper, \
                f"no ASIN surfaced by ffprobe for dialect {entry['dialect']}: {list(tags)}"
            assert upper["ASIN"] == entry["belongs_to_asin"]
            seen.add(entry["dialect"])
        assert {"mp4-atoms", "id3v23", "id3v24", "vorbis"} <= seen

    def test_a_no_tags_file_really_carries_no_metadata(self, tmp_path: pathlib.Path) -> None:
        manifest = build("mixed-reality", tmp_path, limit=None)
        untagged = [e for e in manifest["entries"] if e["tag_state"] == "no-tags"]
        assert untagged, "mixed-reality must produce some untagged files"
        for entry in untagged:
            tags = ffprobe_tags(tmp_path / entry["path"])
            # Container-level brands are not metadata; title/artist/album must be absent.
            assert not {k.lower() for k in tags} & {"title", "artist", "album"}
            assert entry["tags_written"] == {}

    def test_a_lying_tag_really_disagrees_with_its_folder(self, tmp_path: pathlib.Path) -> None:
        # The point of the whole exercise. If the tag on disk agreed with the folder, the
        # lying-tags scenario would be testing nothing at all.
        manifest = build("lying-tags", tmp_path)
        lied = 0
        for entry in manifest["entries"]:
            if entry["tag_state"] not in ("wrong-title-same-author", "wrong-author",
                                          "colliding-title"):
                continue
            tags = ffprobe_tags(tmp_path / entry["path"])
            folder = entry["path"]
            disagrees = (
                (tags.get("title", "") != entry["true_title"])
                or all(a not in tags.get("artist", "") for a in entry["true_authors"])
            )
            assert disagrees, f"{folder} was supposed to carry a lying tag but does not"
            # ...and the FOLDER still names the right book.
            assert entry["true_title"] not in ("", None)
            lied += 1
        assert lied, "lying-tags produced no lying tags"

    def test_a_wrong_asin_points_at_a_different_book(self, tmp_path: pathlib.Path) -> None:
        manifest = build("lying-tags", tmp_path)
        wrong = [e for e in manifest["entries"] if e["tag_state"] == "wrong-asin"]
        assert wrong
        for entry in wrong:
            tags = ffprobe_tags(tmp_path / entry["path"])
            upper = {k.upper(): v for k, v in tags.items()}
            assert upper.get("ASIN") != entry["belongs_to_asin"], \
                "a definitive signal pointing at the wrong book is the whole test"


class TestHazardsAreSafeToGenerate:
    def test_no_generated_path_escapes_the_library_root(self, tmp_path: pathlib.Path) -> None:
        # SECURITY, and the reason the hazard axis exists. A tag containing '../../../../etc'
        # is attacker-controlled input reaching a rename target. Generating it must not
        # escape, and neither must anything downstream.
        out = tmp_path / "lib"
        manifest = build("rename-hazards", out)
        root = out.resolve()
        for entry in manifest["entries"]:
            resolved = (out / entry["path"]).resolve()
            assert resolved.is_relative_to(root), f"{entry['path']} escaped the library root"
        for path in out.rglob("*"):
            assert path.resolve().is_relative_to(root)

    def test_the_traversal_hazard_reaches_the_tag_but_not_the_path(
        self, tmp_path: pathlib.Path
    ) -> None:
        out = tmp_path / "lib"
        manifest = build("rename-hazards", out)
        traversal = [e for e in manifest["entries"] if e["hazard"] == "path-traversal"]
        assert traversal, "the traversal hazard must actually be generated"
        for entry in traversal:
            assert ".." not in pathlib.PurePosixPath(entry["path"]).parts
            tags = ffprobe_tags(out / entry["path"])
            assert "../" in tags.get("title", ""), \
                "the hazard must survive INTO the tag, or nothing is being tested"

    def test_every_hazard_is_actually_represented(self, tmp_path: pathlib.Path) -> None:
        # A hazard that silently fails to generate is a hazard nobody tests.
        manifest = build("rename-hazards", tmp_path, limit=None)
        produced = {e["hazard"] for e in manifest["entries"] if e["hazard"]}
        declared = set(cases.SCENARIOS_BY_KEY["rename-hazards"].extras["hazards"])
        assert declared - produced == set(), f"never generated: {declared - produced}"

    def test_no_component_exceeds_the_filesystem_byte_limit(
        self, tmp_path: pathlib.Path
    ) -> None:
        manifest = build("rename-hazards", tmp_path, limit=None)
        for entry in manifest["entries"]:
            for part in pathlib.PurePosixPath(entry["path"]).parts:
                assert len(part.encode()) <= 255, f"{part[:40]}... is over 255 bytes"

    def test_the_collision_twins_are_two_distinct_files(self, tmp_path: pathlib.Path) -> None:
        # A case collision or an NFC/NFD collision needs two sides. On ext4 they are two
        # files; on APFS or NTFS they would be one, and one book would overwrite the other.
        manifest = build("rename-hazards", tmp_path, limit=None)
        for hazard in ("case-collision", "unicode-normalization"):
            twins = [e for e in manifest["entries"]
                     if e["hazard"] == hazard and e["hazard_twin"]]
            assert twins, f"{hazard} produced no twin"
            for twin in twins:
                assert (tmp_path / twin["path"]).exists()


class TestMultiFileBooks:
    def test_every_part_of_a_book_links_to_one_book(self, tmp_path: pathlib.Path) -> None:
        manifest = build("multi-file-books", tmp_path)
        by_book: dict[tuple, list] = {}
        for entry in manifest["entries"]:
            if entry["kind"] == "book":
                by_book.setdefault((entry["belongs_to_asin"], entry["structure"]), []).append(
                    entry
                )
        multipart = {k: v for k, v in by_book.items() if len(v) > 1}
        assert multipart, "multi-file-books must produce books with several files"
        for (asin, _structure), parts in multipart.items():
            assert all(p["belongs_to_asin"] == asin for p in parts)
            assert {p["part"] for p in parts} == set(range(1, parts[0]["of"] + 1))

    def test_the_multi_disc_case_puts_parts_in_separate_subfolders(
        self, tmp_path: pathlib.Path
    ) -> None:
        # The interesting one: the files share no direct parent, so CalculateBasePath has to
        # climb — and climbing one level too far swallows a sibling book.
        manifest = build("multi-file-books", tmp_path)
        discs = [e for e in manifest["entries"] if e["structure"] == "multi-disc"]
        assert discs
        parents = {str(pathlib.PurePosixPath(e["path"]).parent) for e in discs}
        assert len(parents) > 1
        assert any("CD" in p for p in parents)


class TestClutter:
    def test_clutter_is_generated_and_is_not_attributed_to_a_book(
        self, tmp_path: pathlib.Path
    ) -> None:
        manifest = build("clutter", tmp_path)
        clutter = [e for e in manifest["entries"] if e["kind"] == "clutter"]
        assert clutter
        assert all(e["belongs_to_asin"] is None for e in clutter)
        kinds = {e["clutter_kind"] for e in clutter}
        assert set(cases.CLUTTER) == kinds

    def test_the_zero_byte_file_is_really_zero_bytes(self, tmp_path: pathlib.Path) -> None:
        # ffprobe returns nothing for it. It must not crash or hang the scan.
        manifest = build("clutter", tmp_path)
        empties = [e for e in manifest["entries"] if e.get("clutter_kind") == "zero-byte"]
        assert empties
        for entry in empties:
            assert (tmp_path / entry["path"]).stat().st_size == 0

    def test_the_corrupt_file_has_an_audio_extension_but_is_not_audio(
        self, tmp_path: pathlib.Path
    ) -> None:
        manifest = build("clutter", tmp_path)
        corrupt = [e for e in manifest["entries"] if e.get("clutter_kind") == "corrupt-audio"]
        assert corrupt
        for entry in corrupt:
            path = tmp_path / entry["path"]
            assert path.suffix in (".m4b", ".mp3", ".flac")
            probe = subprocess.run(
                ["ffprobe", "-v", "error", "-show_format", str(path)],
                capture_output=True, text=True,
            )
            assert probe.returncode != 0, "the corrupt file must not parse as audio"

    def test_sidecars_are_written_where_the_parser_reads_them(
        self, tmp_path: pathlib.Path
    ) -> None:
        manifest = build("clutter", tmp_path)
        sidecars = [e for e in manifest["entries"] if e.get("clutter_kind") == "sidecars"]
        names = {pathlib.PurePosixPath(e["path"]).name for e in sidecars}
        assert {"desc.txt", "reader.txt"} <= names


class TestScenarioCoverage:
    def test_every_scenario_generates_something(self, tmp_path: pathlib.Path) -> None:
        for scenario in cases.SCENARIOS:
            if scenario.key == "scale":
                continue  # covered separately; it is a volume test, not a variety one
            manifest = build(scenario.key, tmp_path / scenario.key)
            assert manifest["files"] > 0, f"{scenario.key} generated no audio files"

    def test_the_series_work_key_pairs_survive_into_the_library(
        self, tmp_path: pathlib.Path
    ) -> None:
        # The headline finding, end to end: two distinct ASINs, one series slot, both present
        # in the generated tree as separate books.
        manifest = build("series-work-key", tmp_path, limit=None)
        slots: dict[tuple, set] = {}
        for entry in manifest["entries"]:
            if entry["true_series_asin"] and entry["true_series_position"]:
                key = (entry["true_series_asin"], entry["true_series_position"])
                slots.setdefault(key, set()).add(entry["belongs_to_asin"])
        shared = {k: v for k, v in slots.items() if len(v) > 1}
        assert len(shared) >= 4, \
            f"expected the four work-key pairs to share a series slot, got {len(shared)}"
