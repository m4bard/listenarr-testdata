"""The download-shaped fixture and the stub's reflect mode have to agree on one thing:
the relative file names. Listenarr rebuilds the content path from save_path plus those
names, so a fixture that writes `Book/1.mp3` and a stub that reports `1.mp3` would produce
a harness that fails for a reason with nothing to do with what is being measured.

These check that agreement, and that `.mp4` is emitted as a literal `.mp4` rather than
quietly normalised to `.m4b`, which is the whole point of adding it.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "tools"))

import generate_library as gen
import make_download_fixture as fixture
from qbittorrent_stub import ReflectedContent

META = gen.Meta(
    title="The Valley of Fear",
    authors=["Arthur Conan Doyle"],
    narrators=["John Bolen"],
    asin="B002UUFXKU",
    year="2006",
)


def test_mp4_is_a_first_class_container() -> None:
    assert "mp4" in gen.DIALECT_FOR_EXT
    assert gen.DIALECT_FOR_EXT["mp4"] == "mp4-atoms"
    assert gen.SilenceFactory.CODECS["mp4"] == ("aac", "mp4")


@pytest.mark.parametrize("ext", ["mp3", "m4b", "m4a", "flac", "mp4"])
def test_single_layout_writes_one_file_with_that_extension(
    tmp_path: pathlib.Path, ext: str
) -> None:
    built = fixture.build(tmp_path, ext, "single", 1, META)
    assert built["files"] == [f"The Valley of Fear.{ext}"]
    written = (tmp_path / built["entry"]).resolve()
    assert written.is_file()
    assert written.suffix == f".{ext}"
    assert built["total_bytes"] == written.stat().st_size


def test_folder_layout_names_files_relative_to_the_save_path(tmp_path: pathlib.Path) -> None:
    built = fixture.build(tmp_path, "mp3", "folder", 3, META)
    assert built["entry"] == "The Valley of Fear"
    assert built["files"] == [
        "The Valley of Fear/The Valley of Fear - Part 01.mp3",
        "The Valley of Fear/The Valley of Fear - Part 02.mp3",
        "The Valley of Fear/The Valley of Fear - Part 03.mp3",
    ]


def test_nested_layout_puts_the_audio_one_level_further_down(tmp_path: pathlib.Path) -> None:
    built = fixture.build(tmp_path, "mp3", "nested", 2, META)
    assert built["entry"] == "The Valley of Fear"
    assert all(name.startswith("The Valley of Fear/Disc 1/") for name in built["files"])


def test_the_silence_cache_is_not_left_behind_as_torrent_content(tmp_path: pathlib.Path) -> None:
    fixture.build(tmp_path, "mp3", "single", 1, META)
    assert sorted(child.name for child in tmp_path.iterdir()) == ["The Valley of Fear.mp3"]


def test_reflect_reports_a_single_file_torrent_as_a_bare_name(tmp_path: pathlib.Path) -> None:
    built = fixture.build(tmp_path, "mp3", "single", 1, META)
    reflected = ReflectedContent(tmp_path, "/downloads", built["entry"])
    assert [entry["name"] for entry in reflected.files_json()] == built["files"]
    assert reflected.total_size == built["total_bytes"]
    assert reflected.torrent("deadbeef", "stalledUP", 1.0)["save_path"] == "/downloads"


def test_reflect_reports_a_directory_torrent_with_its_top_level_folder(
    tmp_path: pathlib.Path,
) -> None:
    built = fixture.build(tmp_path, "mp3", "folder", 2, META)
    reflected = ReflectedContent(tmp_path, "/downloads", built["entry"])
    assert [entry["name"] for entry in reflected.files_json()] == built["files"]


def test_reflect_refuses_an_entry_that_is_not_there(tmp_path: pathlib.Path) -> None:
    with pytest.raises(FileNotFoundError):
        ReflectedContent(tmp_path, "/downloads", "no such entry")


def test_reflect_reports_sizes_read_from_disk_not_invented(tmp_path: pathlib.Path) -> None:
    built = fixture.build(tmp_path, "mp3", "folder", 2, META)
    reflected = ReflectedContent(tmp_path, "/downloads", built["entry"])
    for entry in reflected.files_json():
        assert entry["size"] == (tmp_path / str(entry["name"])).stat().st_size
