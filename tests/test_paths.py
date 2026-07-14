"""Path rendering: the layer where a bug does not merely mis-match, it loses a file.

Everything here is about the boundary between a metadata string (arbitrary, hostile,
attacker-controlled) and a filesystem path (constrained, and destructive to get wrong).
"""
from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "tools"))

from generate_library import (
    MAX_COMPONENT_BYTES,
    clamp_bytes,
    drop_empty,
    posix_component,
    posix_filename,
    render,
)


class TestClampBytes:
    def test_short_value_is_untouched(self) -> None:
        assert clamp_bytes("She") == "She"

    def test_clamps_on_bytes_not_characters(self) -> None:
        # The whole point of the component-length hazard: UTF-8 is variable-width, so a
        # length check written against len(str) passes a name the filesystem will reject.
        cyrillic = "Белые ночи"
        assert len(cyrillic) == 10
        assert len(cyrillic.encode()) == 19

        cjk = "杜子春"
        assert len(cjk) == 3
        assert len(cjk.encode()) == 9

    def test_never_splits_a_multibyte_character(self) -> None:
        # A cut mid-character would emit invalid UTF-8 and the write would fail — or worse,
        # succeed with a mojibake name that no later lookup can reproduce.
        value = "杜" * 200  # 600 bytes
        clamped = clamp_bytes(value)
        assert len(clamped.encode()) <= MAX_COMPONENT_BYTES
        assert clamped == "杜" * 85  # 255 // 3
        clamped.encode().decode()  # must round-trip

    def test_clamps_exactly_at_the_limit(self) -> None:
        assert len(clamp_bytes("a" * 300).encode()) == MAX_COMPONENT_BYTES


class TestPosixComponent:
    def test_separator_cannot_survive(self) -> None:
        # A '/' in a title must NOT silently create a nested directory.
        assert "/" not in posix_component("Frankenstein / The Modern Prometheus")

    def test_control_characters_are_removed(self) -> None:
        assert posix_component("Dracula\nBram\tStoker\r") == "DraculaBramStoker"

    def test_nul_is_removed(self) -> None:
        # A NUL truncates the path at the C level: the name written is not the name intended.
        assert "\x00" not in posix_component("She\x00and Allan")

    @pytest.mark.parametrize("value", ["", "   ", ".", "..", " . "])
    def test_unnameable_values_get_a_placeholder(self, value: str) -> None:
        # An empty component would produce '{Root}//{Title}'; '..' would climb.
        assert posix_component(value) == "_"

    def test_traversal_cannot_climb(self) -> None:
        assert posix_component("../../../../etc/passwd") == "..-..-..-..-etc-passwd"

    @pytest.mark.parametrize(
        "value",
        [
            "Moby-Dick; or, The Whale",       # colon-adjacent punctuation
            "She: A History of Adventure",    # colon
            "What Is Man?",                   # question mark
            "CON",                            # reserved on Windows
            "Trailing. ",                     # trailing dot and space
            ".hidden",                        # leading dot
            "Tom & Jerry $PATH `id` 100% #1", # shell metacharacters
            "R.U.R.",
        ],
    )
    def test_posix_legal_hazards_reach_disk_verbatim(self, value: str) -> None:
        # These are hazards on NTFS/APFS but perfectly legal on ext4. We write them for
        # real: a hazard that exists on disk is better evidence than one we merely describe.
        assert posix_component(value) == value

    def test_over_long_component_is_clamped(self) -> None:
        assert len(posix_component("Белые ночи " * 40).encode()) <= MAX_COMPONENT_BYTES


class TestPosixFilename:
    def test_extension_survives_a_clamp(self) -> None:
        # The bug this test exists for: clamping the name to 255 bytes AFTER appending the
        # extension truncates the extension away, leaving a file no scanner will look at.
        name = posix_filename("杜" * 300 + ".m4b")
        assert name.endswith(".m4b")
        assert len(name.encode()) <= MAX_COMPONENT_BYTES

    def test_whole_component_including_extension_is_within_the_limit(self) -> None:
        assert len(posix_filename("a" * 300 + ".flac").encode()) == MAX_COMPONENT_BYTES

    def test_a_slash_in_the_title_does_not_split_the_name(self) -> None:
        # PurePosixPath would read this as a directory. It is one filename.
        name = posix_filename("Frankenstein / The Modern Prometheus.m4b")
        assert "/" not in name
        assert name.endswith(".m4b")
        assert name.startswith("Frankenstein")

    def test_dotfile_keeps_its_leading_dot(self) -> None:
        assert posix_filename(".DS_Store") == ".DS_Store"

    def test_dots_in_the_stem_are_not_an_extension(self) -> None:
        assert posix_filename("R.U.R..m4b") == "R.U.R..m4b"


class TestDropEmpty:
    def test_missing_series_takes_its_separator_with_it(self) -> None:
        # A standalone book must render 'Austen - Persuasion', never 'Austen -  - Persuasion'.
        values = {"author": "Jane Austen", "series": "", "title": "Persuasion"}
        assert drop_empty("{author} - {series} - {title}", values) == "{author} - {title}"

    def test_present_series_is_left_alone(self) -> None:
        values = {"author": "Burroughs", "series": "Barsoom", "title": "A Princess of Mars"}
        assert drop_empty("{author} - {series} - {title}", values) == \
            "{author} - {series} - {title}"

    def test_empty_bracket_group_is_removed(self) -> None:
        values = {"title": "Persuasion", "series": "", "series_position": ""}
        assert drop_empty("{title} [{series} {series_position}]", values) == "{title}"

    def test_series_without_a_position_leaves_no_gap(self) -> None:
        # A real corpus case: a book with a series but no position (series-no-position).
        values = {"title": "Anna", "series": "Five Towns", "series_position": ""}
        assert drop_empty("{title} [{series} {series_position}]", values) == \
            "{title} [{series}]"

    def test_a_title_containing_a_dash_is_not_mangled(self) -> None:
        values = {"author": "Melville", "title": "Moby-Dick; or, The Whale", "series": ""}
        out = drop_empty("{author} - {title}", values)
        assert out == "{author} - {title}"


class TestRender:
    def test_a_book_opts_out_of_a_layout_it_cannot_express(self) -> None:
        # A standalone book has no {series} directory level to stand in, so it does not
        # appear in a series layout at all — it does not get a blank folder.
        standalone = {"author": "Jane Austen", "series": "", "series_position": "",
                      "title": "Persuasion", "year": "2011"}
        assert render("{author}/{series}/{title}", standalone) is None

    def test_series_layout_renders_for_a_series_book(self) -> None:
        book = {"author": "Edgar Rice Burroughs", "series": "Barsoom", "series_position": "1",
                "title": "A Princess of Mars", "year": "2012"}
        out = render("{author}/{series}/{year} - {title}", book)
        assert out is not None
        assert out.parts == ("Edgar Rice Burroughs", "Barsoom", "2012 - A Princess of Mars")

    def test_the_loose_layout_renders_to_no_components_at_all(self) -> None:
        # PurePosixPath() stringifies as '.', which is emphatically not the same as having a
        # folder — the loose layout's files sit at the library root.
        out = render("", {"title": "She"})
        assert out is not None
        assert out.parts == ()

    def test_no_rendered_component_can_escape_the_root(self) -> None:
        hostile = {"author": "../../etc", "series": "..", "series_position": "1",
                   "title": "../../../root/.ssh/authorized_keys", "year": "2011"}
        out = render("{author}/{title}", hostile)
        assert out is not None
        root = pathlib.Path("/library")
        resolved = (root / out).resolve()
        assert resolved.is_relative_to(root)
        assert ".." not in out.parts
