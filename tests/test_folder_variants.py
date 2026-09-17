"""Folder variants: the FOLDER disagrees with the record, and only the folder.

The property every test here defends is the one that makes a variant library usable as an
answer key. The corpus record and the embedded tags must keep the canonical spelling, the
path must carry the variant one, and `belongs_to_asin` must still name the book that really
owns the file. Move any of those and a scan that guesses right is indistinguishable from one
that guesses wrong.

The second property is scope. A layout is uniform across a library; these cases need one book
in variant form beside a sibling that is not, because a matcher loosened enough to reach the
variant folder is also loose enough to reach its neighbour.
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
from generate_library import (
    FOLDER_VARIANTS_BY_KEY,
    Meta,
    apply_folder_variant,
    author_doctorate,
    author_generational,
    author_honorific,
    author_initials,
    author_uncommon_credential,
    drop_leading_article,
    folder_variant_for,
    generate,
    load_corpus,
    parse_folder_variants,
)

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg required")

# The two pairs the PR #784 repro uses. In each, the FIRST ASIN is the one whose folder moves
# and the second is the sibling that stays put.
HOUND, SIGN = "B0036HXZCO", "B0036I51QQ"          # Doyle: the record keeps 'The'
AVONLEA, GABLES = "B002V8L2UQ", "B073JR7W68"      # Montgomery, credited two different ways

BY_ASIN = {b["asin"]: b for b in load_corpus()}


class TestTransforms:
    def test_drops_a_leading_article(self) -> None:
        assert drop_leading_article("The Hound of the Baskervilles") \
            == "Hound of the Baskervilles"
        assert drop_leading_article("A Princess of Mars") == "Princess of Mars"
        assert drop_leading_article("An Ideal Husband") == "Ideal Husband"

    def test_leaves_an_internal_article_and_a_bare_one_alone(self) -> None:
        assert drop_leading_article("Anne of Green Gables") == "Anne of Green Gables"
        assert drop_leading_article("Theatre") == "Theatre"   # not the word 'The'
        assert drop_leading_article("The") == "The"           # nothing left to keep

    def test_initialises_given_names(self) -> None:
        assert author_initials("Lucy Maud Montgomery") == "L. M. Montgomery"
        assert author_initials("Arthur Conan Doyle") == "A. C. Doyle"

    def test_an_already_initialised_name_is_unchanged(self) -> None:
        assert author_initials("L. M. Montgomery") == "L. M. Montgomery"
        assert author_initials("Homer") == "Homer"

    def test_credits_the_folder_with_a_post_nominal(self) -> None:
        assert author_doctorate("Arthur Conan Doyle") == "Arthur Conan Doyle, PhD"
        assert author_generational("Arthur Conan Doyle") == "Arthur Conan Doyle Jr"
        assert author_uncommon_credential("Arthur Conan Doyle") == "Arthur Conan Doyle, CFP"

    def test_the_honorific_leads_rather_than_trails(self) -> None:
        assert author_honorific("Arthur Conan Doyle") == "Dr. Arthur Conan Doyle"
        assert author_honorific("Dr. Arthur Conan Doyle") == "Dr. Arthur Conan Doyle"

    def test_a_credential_already_present_is_not_doubled(self) -> None:
        # Otherwise the manifest would claim a disagreement one credential wide when the
        # folder and the record actually differ by two.
        assert author_doctorate("Jane Doe, PhD") == "Jane Doe, PhD"
        assert author_generational("Martin King JR") == "Martin King JR"

    def test_no_variant_leaves_a_folder_ending_in_a_dot(self) -> None:
        """A trailing '.' is its own path hazard and would confound an attribution result."""
        for transform in (author_doctorate, author_generational,
                          author_uncommon_credential, author_honorific):
            assert not transform("Arthur Conan Doyle").endswith(".")


class TestApplication:
    def test_reports_whether_it_actually_differed(self) -> None:
        spec = FOLDER_VARIANTS_BY_KEY["drop-leading-article"]
        varied, differs = apply_folder_variant(spec, Meta.truth(BY_ASIN[HOUND]))
        assert differs and varied.title == "Hound of the Baskervilles"
        unchanged, differs = apply_folder_variant(spec, Meta.truth(BY_ASIN[GABLES]))
        assert not differs and unchanged.title == "Anne of Green Gables"

    def test_does_not_mutate_the_metadata_it_was_given(self) -> None:
        # The same Meta goes on to build the tags. Aliasing its author list would put the
        # variant spelling into the tags too, and the disagreement would vanish.
        truth = Meta.truth(BY_ASIN[AVONLEA])
        apply_folder_variant(FOLDER_VARIANTS_BY_KEY["author-initials"], truth)
        assert truth.authors == ["Lucy Maud Montgomery"]


class TestSelection:
    def test_bare_key_covers_every_book(self) -> None:
        parsed = parse_folder_variants(["drop-leading-article"])
        assert parsed == [("drop-leading-article", set())]
        assert folder_variant_for(parsed, BY_ASIN[SIGN]) is not None

    def test_a_scoped_key_covers_only_its_asins(self) -> None:
        parsed = parse_folder_variants([f"drop-leading-article:{HOUND.lower()}"])
        assert folder_variant_for(parsed, BY_ASIN[HOUND]) is not None
        assert folder_variant_for(parsed, BY_ASIN[SIGN]) is None

    def test_an_unknown_key_raises_before_anything_is_generated(self) -> None:
        with pytest.raises(ValueError, match="unknown folder variant"):
            parse_folder_variants(["no-such-variant:B0036HXZCO"])


@needs_ffmpeg
class TestGeneratedLibrary:
    def _generate(self, out: pathlib.Path, asins: list[str], variant: str) -> dict:
        return generate(
            cases.SCENARIOS_BY_KEY["existing-library-adoption"], out, seed=1,
            layout_override="author-title", only_asins=asins,
            folder_variants=parse_folder_variants([variant]),
        )

    def test_the_article_case_moves_one_folder_and_leaves_the_sibling(
        self, tmp_path: pathlib.Path
    ) -> None:
        manifest = self._generate(
            tmp_path / "lib", [HOUND, SIGN], f"drop-leading-article:{HOUND}")
        by_asin = {e["belongs_to_asin"]: e for e in manifest["entries"]}

        target = by_asin[HOUND]
        assert target["path"] == (
            "Arthur Conan Doyle/Hound of the Baskervilles/Hound of the Baskervilles.m4b")
        assert target["folder_variant"] == "drop-leading-article"
        # The record and the tags keep the article; only the path dropped it.
        assert target["true_title"] == "The Hound of the Baskervilles"
        assert target["tags_written"]["title"] == "The Hound of the Baskervilles"

        sibling = by_asin[SIGN]
        assert sibling["path"].startswith("Arthur Conan Doyle/The Sign of Four/")
        assert sibling["folder_variant"] is None

        assert (tmp_path / "lib" / target["path"]).is_file()
        assert (tmp_path / "lib" / sibling["path"]).is_file()

    def test_the_initials_case_collects_both_books_under_one_author_folder(
        self, tmp_path: pathlib.Path
    ) -> None:
        manifest = self._generate(
            tmp_path / "lib", [AVONLEA, GABLES], f"author-initials:{AVONLEA}")
        by_asin = {e["belongs_to_asin"]: e for e in manifest["entries"]}

        assert by_asin[AVONLEA]["path"].startswith("L. M. Montgomery/Anne of Avonlea/")
        assert by_asin[AVONLEA]["folder_variant"] == "author-initials"
        assert by_asin[AVONLEA]["true_authors"] == ["Lucy Maud Montgomery"]
        assert by_asin[AVONLEA]["tags_written"]["artist"] == "Lucy Maud Montgomery"

        # The sibling was already credited in the initialised form, so it needed no transform
        # to land in the same folder — which is exactly what makes the folder shared, and the
        # scan root for either book full of the other book's audio.
        assert by_asin[GABLES]["path"].startswith("L. M. Montgomery/Anne of Green Gables/")
        assert by_asin[GABLES]["folder_variant"] is None

    def test_the_post_nominal_case_credits_one_author_folder_and_not_the_sibling(
        self, tmp_path: pathlib.Path
    ) -> None:
        manifest = self._generate(
            tmp_path / "lib", [HOUND, SIGN], f"author-postnominal:{HOUND}")
        by_asin = {e["belongs_to_asin"]: e for e in manifest["entries"]}

        target = by_asin[HOUND]
        assert target["path"].startswith(
            "Arthur Conan Doyle, PhD/The Hound of the Baskervilles/")
        assert target["folder_variant"] == "author-postnominal"
        # The record and the tags keep the plain name; only the path carries the credential,
        # so a scan that links this file did so by tolerating the folder and not by reading
        # the tags.
        assert target["true_authors"] == ["Arthur Conan Doyle"]
        assert target["tags_written"]["artist"] == "Arthur Conan Doyle"
        # The title segment is untouched, which is what keeps the author half separable from
        # the article half when a result comes back.
        assert target["true_title"] == "The Hound of the Baskervilles"

        sibling = by_asin[SIGN]
        assert sibling["path"].startswith("Arthur Conan Doyle/The Sign of Four/")
        assert sibling["folder_variant"] is None

        assert (tmp_path / "lib" / target["path"]).is_file()
        assert (tmp_path / "lib" / sibling["path"]).is_file()

    @pytest.mark.parametrize(("key", "folder"), [
        ("author-generational", "Arthur Conan Doyle Jr"),
        ("author-postnominal-uncommon", "Arthur Conan Doyle, CFP"),
        ("author-honorific", "Dr. Arthur Conan Doyle"),
    ])
    def test_each_credential_shape_lands_on_disk(
        self, tmp_path: pathlib.Path, key: str, folder: str
    ) -> None:
        manifest = self._generate(tmp_path / "lib", [HOUND, SIGN], f"{key}:{HOUND}")
        by_asin = {e["belongs_to_asin"]: e for e in manifest["entries"]}
        target = by_asin[HOUND]
        assert target["path"].startswith(f"{folder}/The Hound of the Baskervilles/")
        assert target["folder_variant"] == key
        assert target["true_authors"] == ["Arthur Conan Doyle"]
        assert (tmp_path / "lib" / target["path"]).is_file()
        assert by_asin[SIGN]["path"].startswith("Arthur Conan Doyle/The Sign of Four/")

    def test_a_variant_the_book_cannot_express_is_recorded_as_absent(
        self, tmp_path: pathlib.Path
    ) -> None:
        # 'Anne of Green Gables' has no leading article. The manifest may not claim otherwise.
        manifest = self._generate(
            tmp_path / "lib", [GABLES], f"drop-leading-article:{GABLES}")
        entry = manifest["entries"][0]
        assert entry["folder_variant"] is None
        assert entry["expect_folder_variant"] is None
        assert entry["path"].startswith("L. M. Montgomery/Anne of Green Gables/")


@needs_ffmpeg
def test_cli_accepts_a_scoped_variant(tmp_path: pathlib.Path) -> None:
    out = tmp_path / "lib"
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "generate_library.py"),
         "--ffmpeg-source", "system", "--layout", "author-title",
         "--only-asin", f"{HOUND},{SIGN}",
         "--folder-variant", f"drop-leading-article:{HOUND}",
         "--out", str(out)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    manifest = json.loads((out / "manifest.json").read_text())
    varied = [e for e in manifest["entries"] if e["folder_variant"]]
    assert [e["belongs_to_asin"] for e in varied] == [HOUND]


def test_cli_rejects_an_unknown_variant(tmp_path: pathlib.Path) -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "generate_library.py"),
         "--ffmpeg-source", "system", "--folder-variant", "nonsense",
         "--out", str(tmp_path / "lib")],
        capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert "unknown folder variant" in result.stderr
    assert not (tmp_path / "lib").exists()


def test_every_declared_variant_is_implemented() -> None:
    """cases.py declares the axis; generate_library.py implements it. They must agree."""
    assert {v.key for v in cases.FOLDER_VARIANTS} == set(FOLDER_VARIANTS_BY_KEY)


def test_the_attribution_runner_advertises_the_flag() -> None:
    """vet-against.sh checks forwarded flags against the runner's --help before it builds.

    A flag that works but is undocumented is rejected there after the clone, so the usage
    text is load-bearing rather than decorative.
    """
    runner = ROOT / "tools" / "validate_scan_attribution.sh"
    help_text = subprocess.run([str(runner), "--help"], capture_output=True, text=True).stdout
    assert "--folder-variant" in help_text
