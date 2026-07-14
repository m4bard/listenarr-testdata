"""Tag-state transforms: "the tags lie", modelled.

The rule these tests exist to enforce: a transform either happens or is honestly reported as
not having happened. A generator that claims a book carries a colliding title when it does
not would produce a conformance suite whose failures mean nothing.
"""
from __future__ import annotations

import json
import pathlib
import random
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "corpus"))

import cases
from generate_library import (
    HAZARDS_BY_KEY,
    Meta,
    apply_hazard,
    apply_tag_state,
    author_variant,
    base_title,
    claim,
    colliding_title_book,
    load_corpus,
    numeral_variant,
    same_author_other_book,
    twin_differs,
)

CORPUS = load_corpus()
BY_ASIN = {b["asin"]: b for b in CORPUS}
ALL_ASINS = {b["asin"] for b in CORPUS}


def book(asin: str) -> dict:
    return BY_ASIN[asin]


@pytest.fixture
def rng() -> random.Random:
    return random.Random(1)


class TestTagStates:
    def test_correct_with_asin_carries_the_real_asin(self, rng: random.Random) -> None:
        princess = book("B008DFUGCQ")
        meta, _path, applied = apply_tag_state("correct-with-asin", princess, CORPUS, rng)
        assert applied and meta is not None
        assert meta.asin == "B008DFUGCQ"
        assert meta.title == princess["title"]

    def test_correct_no_asin_strips_it(self, rng: random.Random) -> None:
        # An entire real library can contain zero ASINs. This is the common case, not an edge.
        meta, _path, applied = apply_tag_state("correct-no-asin", book("B008DFUGCQ"), CORPUS, rng)
        assert applied and meta is not None
        assert meta.asin is None
        assert meta.title == "A Princess of Mars"

    def test_no_tags_writes_nothing(self, rng: random.Random) -> None:
        meta, _path, applied = apply_tag_state("no-tags", book("B008DFUGCQ"), CORPUS, rng)
        assert applied
        assert meta is None  # absence of tags is not evidence against the folder

    def test_wrong_title_same_author_keeps_the_author(self, rng: random.Random) -> None:
        # The single most common disagreement in a real library, and the one where the author
        # check provides no discrimination whatsoever.
        princess = book("B008DFUGCQ")
        meta, _path, applied = apply_tag_state("wrong-title-same-author", princess, CORPUS, rng)
        assert applied and meta is not None
        assert meta.title != princess["title"]
        assert meta.authors == princess["authors"]
        assert meta.asin is None  # a lying tag must not also carry a definitive signal

    def test_wrong_author_changes_the_author(self, rng: random.Random) -> None:
        princess = book("B008DFUGCQ")
        meta, _path, applied = apply_tag_state("wrong-author", princess, CORPUS, rng)
        assert applied and meta is not None
        assert not set(meta.authors) & set(princess["authors"])

    def test_wrong_asin_borrows_a_real_asin_and_never_invents_one(
        self, rng: random.Random
    ) -> None:
        # The cardinal rule of this repository: a plausible B0XXXXXXXX is trivial to
        # hallucinate and impossible to spot by eye. Every ASIN we write is one Audnex
        # actually resolved — including the wrong ones.
        for target in CORPUS[:30]:
            meta, _path, applied = apply_tag_state("wrong-asin", target, CORPUS, rng)
            assert applied and meta is not None
            assert meta.asin in ALL_ASINS
            assert meta.asin != target["asin"]

    def test_colliding_title_is_a_real_containment_collision(self, rng: random.Random) -> None:
        # The Haggard trap: folder says 'She', tag says 'She And Allan'. Bidirectional
        # containment attributes both to one work; the author check agrees, so it cannot
        # arbitrate.
        #
        # Note the canonical title is 'She: A History of Adventure' — Audnex folds the
        # subtitle in and leaves the subtitle field null. The collision is therefore only
        # visible on the BASE title, which is also the only comparison a scanner can make
        # against a folder, because no human names a folder 'She: A History of Adventure'.
        she = book("B004YWTD30")
        assert she["title"] == "She: A History of Adventure"
        assert base_title(she["title"]) == "She"

        meta, path, applied = apply_tag_state("colliding-title", she, CORPUS, rng)
        assert applied and meta is not None
        assert meta.title == "She And Allan"          # a DIFFERENT work
        assert path.title == she["title"]             # the folder still names the right one
        assert set(meta.authors) == set(she["authors"])  # same author: no discrimination

    def test_subtitled_title_is_a_true_match(self, rng: random.Random) -> None:
        # Must survive any fix aimed at colliding-title. This is the false-negative guard:
        # folder 'She', tag 'She: A History of Adventure' — the same work.
        she = book("B004YWTD30")
        meta, path, applied = apply_tag_state("subtitled-title", she, CORPUS, rng)
        assert applied and meta is not None
        assert meta.title == "She: A History of Adventure"
        assert path.title == "She"
        assert base_title(meta.title) == path.title

    def test_numeral_variant_is_the_same_work(self, rng: random.Random) -> None:
        verne = next(b for b in CORPUS
                     if "Twenty Thousand Leagues" in b["title"])
        meta, _path, applied = apply_tag_state("numeral-variant", verne, CORPUS, rng)
        assert applied and meta is not None
        assert "20,000" in meta.title

    def test_translator_as_author_credits_the_translator(self, rng: random.Random) -> None:
        dostoevsky = next(b for b in CORPUS
                          if any("Dostoevsky" in a for a in b["authors"]))
        meta, _path, applied = apply_tag_state("translator-as-author", dostoevsky, CORPUS, rng)
        assert applied and meta is not None
        assert meta.authors == ["Constance Garnett"]

    def test_an_inapplicable_state_reports_itself_as_not_applied(
        self, rng: random.Random
    ) -> None:
        # A book with no subtitle — in the field OR folded into the title — cannot express
        # subtitled-title. The generator must say so rather than silently record a transform
        # that did not happen.
        plain = next(b for b in CORPUS if not b["subtitle"] and ":" not in b["title"])
        _meta, _path, applied = apply_tag_state("subtitled-title", plain, CORPUS, rng)
        assert applied is False

    def test_unknown_state_is_an_error(self, rng: random.Random) -> None:
        with pytest.raises(ValueError, match="unknown tag state"):
            apply_tag_state("not-a-state", book("B008DFUGCQ"), CORPUS, rng)

    @pytest.mark.parametrize("state", [s.key for s in cases.TAG_STATES])
    def test_the_folder_always_tells_the_truth(self, state: str, rng: random.Random) -> None:
        # THE invariant of the whole matrix. Every expectation in cases.py is written as what
        # a correct scanner should do *given that the folder identifies the book correctly*,
        # so only the tags may lie. If the folder carried the lie too there would be no
        # disagreement left to detect, and a scanner with no conflict handling at all would
        # pass every lying-tags case.
        for target in CORPUS[:40]:
            _tags, path, _applied = apply_tag_state(state, target, CORPUS, rng)
            assert path.authors == target["authors"]
            assert path.asin == target["asin"]
            # subtitled-title is the sole exception, and only in the safe direction: the
            # folder drops the subtitle. It never names a different work.
            assert path.title in (target["title"], base_title(target["title"]))


class TestAuthorVariants:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("H. G. Wells", "H.G. Wells"),               # initials spacing
            ("Émile Zola", "Emile Zola"),                # diacritic folding
            ("Fyodor Dostoevsky", "Fyodor Dostoyevsky"), # transliteration
            ("Mark Twain", "Samuel Clemens"),            # pseudonym -> real name
            ("Saki", "Hector Hugh Munro"),               # pseudonym -> real name
        ],
    )
    def test_known_variants(self, name: str, expected: str) -> None:
        assert author_variant(name) == expected

    def test_diacritics_fold_even_without_a_table_entry(self) -> None:
        # The generic fallback: any accented name has a real, credited unaccented spelling.
        assert author_variant("Sigrid Undsét") == "Sigrid Undset"

    def test_a_plain_ascii_name_has_no_variant(self) -> None:
        assert author_variant("Jane Austen") is None

    def test_numeral_variant_returns_none_when_there_is_none(self) -> None:
        assert numeral_variant("Persuasion") is None


class TestCorpusRelationships:
    def test_same_author_other_book_is_a_different_book(self) -> None:
        princess = book("B008DFUGCQ")
        other = same_author_other_book(princess, CORPUS)
        assert other is not None
        assert other["asin"] != princess["asin"]
        assert other["title"] != princess["title"]
        assert set(other["authors"]) & set(princess["authors"])

    def test_colliding_title_book_finds_the_haggard_collision(self) -> None:
        she = book("B004YWTD30")
        collision = colliding_title_book(she, CORPUS)
        assert collision is not None
        assert "she" in collision["title"].lower()
        assert collision["title"].lower() != "she"


class TestHazards:
    def test_path_traversal_is_written_into_the_tag_verbatim(self) -> None:
        # SECURITY. The tag is attacker-controlled input, and it is what a renamer
        # interpolates into a target path. The hazard must reach the tag intact or we are
        # not testing anything.
        spec = HAZARDS_BY_KEY["path-traversal"]
        meta = Meta(title="She", authors=["H. Rider Haggard"])
        assert apply_hazard(spec, meta).title == "../../../../etc/listenarr"

    def test_a_hazard_does_not_mutate_the_metadata_it_was_given(self) -> None:
        spec = HAZARDS_BY_KEY["colon"]
        meta = Meta(title="She", authors=["H. Rider Haggard"])
        apply_hazard(spec, meta)
        assert meta.title == "She"  # the truth must survive for the manifest to record it

    def test_empty_field_hazard_targets_the_author(self) -> None:
        spec = HAZARDS_BY_KEY["empty-field"]
        result = apply_hazard(spec, Meta(title="She", authors=["H. Rider Haggard"]))
        assert result.authors == [""]
        assert result.title == "She"

    def test_case_twin_differs_for_a_cased_title(self) -> None:
        spec = HAZARDS_BY_KEY["case-collision"]
        assert twin_differs(spec, Meta(title="The Sign of Four", authors=["Doyle"]))

    def test_case_twin_does_not_differ_for_a_caseless_title(self) -> None:
        # A CJK title has no uppercase. Emitting the twin anyway would write the same path
        # twice and silently overwrite the first — a collision needs two distinguishable
        # sides, or it is not a collision.
        spec = HAZARDS_BY_KEY["case-collision"]
        assert not twin_differs(spec, Meta(title="杜子春", authors=["芥川 龍之介"]))

    def test_nfc_nfd_twin_differs_only_when_there_is_something_to_decompose(self) -> None:
        spec = HAZARDS_BY_KEY["unicode-normalization"]
        assert twin_differs(spec, Meta(title="Bílá nemoc", authors=["Karel Čapek"]))
        assert not twin_differs(spec, Meta(title="She", authors=["H. Rider Haggard"]))


class TestClaim:
    def test_an_uncontested_path_is_taken_as_is(self, tmp_path: pathlib.Path) -> None:
        owner: dict = {}
        got, moved = claim(tmp_path / "book.m4b", ("A", 0, 0), owner, book("B008DFUGCQ"),
                           reuse=False)
        assert got == tmp_path / "book.m4b"
        assert moved is False

    def test_two_editions_of_one_work_do_not_overwrite_each_other(
        self, tmp_path: pathlib.Path
    ) -> None:
        # The Barsoom pair: two ASINs, same work, identical path under any layout with no
        # year in it. Left alone the second write destroys the first.
        owner: dict = {}
        first, _ = claim(tmp_path / "A Princess of Mars.m4b", ("B008DFUGCQ", 0, 0), owner,
                         book("B008DFUGCQ"), reuse=False)
        second, moved = claim(tmp_path / "A Princess of Mars.m4b", ("B071YLS9YL", 0, 0), owner,
                              book("B071YLS9YL"), reuse=False)
        assert moved is True
        assert first != second
        assert second.suffix == ".m4b"

    def test_a_books_own_folder_is_reusable_by_its_own_files(
        self, tmp_path: pathlib.Path
    ) -> None:
        # A multi-part book's several files all share one folder. That is not a collision.
        owner: dict = {}
        unit = ("B008DFUGCQ", 0, 0)
        first, _ = claim(tmp_path / "Barsoom", unit, owner, book("B008DFUGCQ"), reuse=True)
        second, moved = claim(tmp_path / "Barsoom", unit, owner, book("B008DFUGCQ"),
                              reuse=True)
        assert first == second
        assert moved is False

    def test_a_file_is_never_reusable(self, tmp_path: pathlib.Path) -> None:
        # Nothing may write the same file twice, not even the same book.
        owner: dict = {}
        unit = ("B008DFUGCQ", 0, 0)
        first, _ = claim(tmp_path / "part.mp3", unit, owner, book("B008DFUGCQ"), reuse=False)
        second, moved = claim(tmp_path / "part.mp3", unit, owner, book("B008DFUGCQ"),
                              reuse=False)
        assert first != second
        assert moved is True


class TestCorpusIntegrity:
    """The corpus is machine-verified by build_corpus.py. These assert it stayed that way."""

    def test_every_asin_is_shaped_like_an_asin(self) -> None:
        for entry in CORPUS:
            assert len(entry["asin"]) == 10
            assert entry["asin"].startswith("B0")

    def test_no_duplicate_asins(self) -> None:
        assert len({b["asin"] for b in CORPUS}) == len(CORPUS)

    @pytest.mark.parametrize(
        ("work", "asin_a", "asin_b"),
        [
            ("A Princess of Mars", "B008DFUGCQ", "B071YLS9YL"),
            ("20,000 Leagues", "B01FKWL15A", "B076HSP1FT"),
            ("The Wonderful Wizard of Oz", "B007BR5KZA", "B002V5CJM4"),
            ("The Three Musketeers", "B002UZJF4U", "B002V0RG8G"),
        ],
    )
    def test_the_work_key_finding(self, work: str, asin_a: str, asin_b: str) -> None:
        # Two distinct book ASINs sharing one series ASIN and one position. The book ASIN is
        # a manifestation id; (series_asin, position) is the stable work key. This is the
        # argument behind the SeriesAsin work upstream, and it must be reproducible from the
        # corpus rather than taken on trust.
        first, second = book(asin_a), book(asin_b)
        assert first["asin"] != second["asin"]
        assert first["series_asin"] == second["series_asin"] is not None
        assert first["series_position"] == second["series_position"] is not None

    def test_sherlock_positions_really_do_skip(self) -> None:
        # Publication order and series position genuinely diverge: the short-story
        # collections occupy slots 4 and 6, so the novels run 1, 2, 3, 5, 7. Any code that
        # assumes contiguous positions is wrong about the real world.
        canon = sorted(
            b["series_position"] for b in CORPUS
            if b["series"] == "Sherlock Holmes" and b["series_position"]
        )
        assert canon == ["1", "2", "3", "5", "7"]

    def test_series_position_is_a_string_and_is_not_always_a_number(self) -> None:
        # decimal.TryParse silently discards a real, present position, after which naming
        # cannot tell it from "no position at all".
        positions = [b["series_position"] for b in CORPUS if b["series_position"]]
        assert all(isinstance(p, str) for p in positions)
        assert any(not p.replace(".", "").isdigit() for p in positions), \
            "corpus must retain at least one non-numeric series position"

    def test_the_region_lock_proof_survives(self) -> None:
        # The same work, one ASIN per marketplace, each invisible from the other's
        # catalogue. An ASIN cannot be a work identifier.
        data = json.loads(
            (pathlib.Path(__file__).resolve().parent.parent / "corpus" / "corpus.json")
            .read_text(encoding="utf-8")
        )
        proofs = data["region_lock_proof"]
        assert len(proofs) >= 2
        for proof in proofs:
            visible = [r for r, v in proof["visibility"].items() if v == "ok"]
            assert visible == [proof["home_region"]], \
                f"{proof['asin']} is no longer region-locked: visible in {visible}"

    def test_byte_dense_titles_are_present(self) -> None:
        # The component-length hazard needs real evidence, not a synthetic string: a title
        # that is short in characters and long in bytes.
        dense = [b for b in CORPUS if len(b["title"].encode()) >= 2 * len(b["title"])]
        assert dense, "corpus must retain a Cyrillic or CJK title for the byte-length hazard"
