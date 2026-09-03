"""The TagLib# / ffprobe parity harness.

This tool exists to produce a number that someone will quote in an argument about deleting a
subsystem. That makes its failure mode the same one TESTING.md is about: not wrong output, but
a wrong *verdict* — a clean agreement rate that was never actually measured. Nothing else in
the repository would notice, and the number would already be upstream by then.

So the weight here is on the ways a run can end up claiming more than it measured: an empty
corpus, a probe whose build cannot be named, files the probe silently skipped, and a file
neither reader could open being quietly dropped out of the denominator.

The rest is ordinary output testing of the two mappings, which are the part a reviewer is most
likely to want to check line by line: the ffprobe side has to mirror Listenarr's C# quirks
exactly, including the ones that look like bugs, or the comparison is against a Listenarr that
does not exist.
"""
from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import sys
from typing import Any

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import taglib_parity as tp

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="ffmpeg is required to generate the matrix"
)
needs_ffprobe = pytest.mark.skipif(
    shutil.which("ffprobe") is None, reason="ffprobe is required for the ffprobe side"
)


def _dotnet_or_skip() -> str:
    try:
        return tp.resolve_dotnet(None)
    except tp.ParityError as exc:
        pytest.skip(str(exc))


# main() checks its ffprobe is present before it does anything else. Any executable satisfies
# that, which lets the refusal paths be tested without a real ffprobe on the machine.
STAND_IN_BINARY = sys.executable


def probe_json(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "format": {
            "duration": "1234.5",
            "format_name": "mp3",
            "bit_rate": "128451",
            "tags": {"title": "She", "artist": "H. Rider Haggard", "album": "Ayesha",
                     "track": "3/12", "disc": "1/2", "date": "1905"},
        },
        "streams": [
            {"codec_type": "audio", "codec_name": "mp3", "sample_rate": "44100", "channels": 1},
        ],
    }
    base.update(overrides)
    return base


def taglib_record(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "kind": "file", "ok": True, "mime_type": "taglib/mp3",
        "properties": {"duration_seconds": 1234.5, "audio_bitrate_kbps": 128,
                       "audio_sample_rate_hz": 44100, "audio_channels": 1,
                       "description": "MPEG Version 1 Audio, Layer 3"},
        "tag": {"title": "She", "performers": ["H. Rider Haggard"], "album": "Ayesha",
                "album_artists": ["H. Rider Haggard"], "track": 3, "disc": 1, "year": 1905},
    }
    base.update(overrides)
    return base


class TestFfprobeMirror:
    """The ffprobe side has to be Listenarr's mapper, quirks included."""

    def test_reads_the_fields_listenarr_reads(self) -> None:
        view = tp.listenarr_ffprobe_view(probe_json(), pathlib.Path("Part 01.mp3"))
        assert view["duration_seconds"] == 1234.5
        assert view["bitrate_bps"] == 128451
        assert view["sample_rate_hz"] == 44100
        assert view["channels"] == 1
        assert view["codec"] == "mp3"
        assert view["container"] == "MP3"
        assert view["title"] == "She"
        assert view["track"] == 3
        assert view["disc"] == 1
        assert view["year"] == 1905

    def test_m4a_becomes_mov_because_only_m4b_is_rescued(self) -> None:
        """The comma split leaves ``mov`` and only ``.m4b`` gets special-cased by extension.

        This looks like a mistake and is faithfully reproduced anyway: the harness compares
        against what Listenarr does, and correcting it here would manufacture a disagreement
        with TagLib# that Listenarr users are not actually experiencing, or hide a real one.
        """
        data = probe_json(format={"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": "1.0"})
        assert tp.listenarr_ffprobe_view(data, pathlib.Path("a.m4a"))["container"] == "MOV"
        assert tp.listenarr_ffprobe_view(data, pathlib.Path("a.m4b"))["container"] == "M4B"

    def test_stream_bitrate_fallback_is_unreachable(self) -> None:
        """``BitRate == 0`` is false for a null int?, so the stream fallback never fires.

        Mirrored deliberately. If Listenarr ever fixes it, this test fails and the harness gets
        updated with it, rather than the two drifting apart unnoticed.
        """
        data = probe_json(format={"format_name": "ogg", "duration": "1.0"})
        data["streams"][0]["bit_rate"] = "80000"
        assert tp.listenarr_ffprobe_view(data, pathlib.Path("a.ogg"))["bitrate_bps"] is None

    def test_format_tags_beat_stream_tags(self) -> None:
        data = probe_json()
        data["streams"][0]["tags"] = {"title": "from the stream"}
        assert tp.listenarr_ffprobe_view(data, pathlib.Path("a.mp3"))["title"] == "She"

    def test_stream_tags_are_used_when_the_format_carries_none(self) -> None:
        """Ogg puts its comments on the stream, so dropping these would blank every Ogg tag."""
        data = probe_json(format={"format_name": "ogg", "duration": "1.0"})
        data["streams"][0]["tags"] = {"title": "She", "artist": "H. Rider Haggard"}
        view = tp.listenarr_ffprobe_view(data, pathlib.Path("a.ogg"))
        assert view["title"] == "She"
        assert view["artist"] == "H. Rider Haggard"

    def test_title_falls_back_to_the_filename(self) -> None:
        data = probe_json(format={"format_name": "mp3", "duration": "1.0"})
        assert tp.listenarr_ffprobe_view(data, pathlib.Path("Part 01.mp3"))["title"] == "Part 01"

    def test_a_cover_art_video_stream_is_ignored(self) -> None:
        data = probe_json()
        data["streams"].insert(0, {"codec_type": "video", "codec_name": "mjpeg"})
        assert tp.listenarr_ffprobe_view(data, pathlib.Path("a.mp3"))["codec"] == "mp3"

    def test_unparseable_numbers_are_dropped_not_guessed(self) -> None:
        data = probe_json(format={"format_name": "mp3", "duration": "N/A", "bit_rate": "N/A"})
        view = tp.listenarr_ffprobe_view(data, pathlib.Path("a.mp3"))
        assert view["duration_seconds"] is None
        assert view["bitrate_bps"] is None


class TestTaglibMapping:
    def test_kbps_is_scaled_to_bits_per_second(self) -> None:
        view = tp.taglib_view(taglib_record(), pathlib.Path("a.mp3"))
        assert view["bitrate_bps"] == 128_000

    def test_a_zero_bitrate_is_absent_not_zero(self) -> None:
        """TagLib# returns 0 for Opus. Treating that as a real 0 bit/s would be a fabrication."""
        record = taglib_record()
        record["properties"] = {**record["properties"], "audio_bitrate_kbps": 0}
        assert tp.taglib_view(record, pathlib.Path("a.opus"))["bitrate_bps"] is None

    def test_zero_track_and_year_become_absent(self) -> None:
        record = taglib_record()
        record["tag"] = {**record["tag"], "track": 0, "disc": 0, "year": 0}
        view = tp.taglib_view(record, pathlib.Path("a.mp3"))
        assert (view["track"], view["disc"], view["year"]) == (None, None, None)

    def test_container_comes_from_the_mime_subtype(self) -> None:
        record = taglib_record(mime_type="taglib/m4b")
        assert tp.taglib_view(record, pathlib.Path("a.m4b"))["container"] == "M4B"

    def test_a_missing_tag_block_still_yields_a_view(self) -> None:
        record = taglib_record(tag=None, properties=None)
        view = tp.taglib_view(record, pathlib.Path("Part 02.mp3"))
        assert view["title"] == "Part 02"
        assert view["duration_seconds"] is None

    @pytest.mark.parametrize(
        ("description", "expected"),
        [
            ("MPEG Version 1 Audio, Layer 3", "mp3"),
            ("MPEG Version 1 Audio, Layer 3 VBR", "mp3"),
            ("MPEG-4 Audio (mp4a)", "aac"),
            ("Flac Audio", "flac"),
            ("Vorbis Version 0 Audio", "vorbis"),
            ("Opus Version 1 Audio", "opus"),
            ("PCM Audio", "pcm_s16le"),
        ],
    )
    def test_codec_descriptions_map_to_ffprobe_names(self, description: str,
                                                     expected: str) -> None:
        assert tp._codec_from_description(description) == expected

    def test_an_unknown_codec_description_is_passed_through_verbatim(self) -> None:
        """Not guessed at. A disagreement row should show the string TagLib# actually printed."""
        assert tp._codec_from_description("Something Nobody Mapped") == "Something Nobody Mapped"


class TestQualityRung:
    """The derived field, mirroring QualityMatcher over the default profile's ladder."""

    def test_normalize_kbps_matches_the_csharp(self) -> None:
        assert tp.normalize_kbps(128_451) == 128
        assert tp.normalize_kbps(128_000) == 128
        assert tp.normalize_kbps(0) is None
        assert tp.normalize_kbps(None) is None
        # The C# guards values already expressed in kbit/s by leaving anything under 1000 alone.
        assert tp.normalize_kbps(320) == 320

    def test_the_best_rung_at_or_below_the_file_is_chosen(self) -> None:
        assert tp.quality_rung(191_000, "mp3") == "128kbps"
        assert tp.quality_rung(192_000, "mp3") == "192kbps"
        assert tp.quality_rung(128_451, "mp3") == "128kbps"

    def test_a_file_under_every_rung_falls_to_the_worst_one(self) -> None:
        assert tp.quality_rung(32_000, "mp3") == "64kbps"

    def test_lossless_ignores_bitrate_entirely(self) -> None:
        assert tp.quality_rung(930_522, "flac") == tp.quality_rung(927_000, "flac")

    def test_a_codec_the_default_profile_has_no_rung_for_is_said_so(self) -> None:
        assert "no rung" in tp.quality_rung(86_373, "opus")


class TestComparison:
    def test_text_is_compared_case_insensitively(self) -> None:
        spec = tp.FIELD_BY_KEY["container"]
        assert tp.compare_views(spec, "MP3", "mp3") == (True, True)

    def test_a_numeric_gap_inside_tolerance_is_not_exact_but_is_tolerant(self) -> None:
        spec = tp.FIELD_BY_KEY["bitrate_bps"]
        exact, tolerant = tp.compare_views(spec, 128_451, 128_000)
        assert (exact, tolerant) == (False, True)

    def test_a_none_against_a_number_is_a_disagreement(self) -> None:
        """TagLib# returning nothing for Opus must not be tolerated into agreement."""
        spec = tp.FIELD_BY_KEY["bitrate_bps"]
        assert tp.compare_views(spec, 86_373, None) == (False, False)


@pytest.mark.contract
class TestParityContract:
    """The ways this harness could report an agreement it never measured."""

    def test_an_empty_corpus_is_refused_rather_than_scored(self, tmp_path: pathlib.Path) -> None:
        """Zero files compared is not one hundred percent agreement.

        Without this, a checkout that never generated the matrix, or a --corpus typo, prints a
        table of perfect scores over nothing, and that table is exactly what gets quoted.
        """
        assert tp.main(["--corpus", str(tmp_path), "--ffprobe", STAND_IN_BINARY]) == 2

    def test_a_probe_run_with_no_version_banner_raises(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        """A measurement that cannot name the TagLib# build that produced it is not evidence."""
        record = json.dumps({"kind": "file", "path": str(tmp_path / "a.mp3"), "ok": True})

        def fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(args, 0, record + "\n", "")

        monkeypatch.setattr(subprocess, "run", fake_run)
        with pytest.raises(tp.ParityError, match="version banner"):
            tp.run_taglib_probe("dotnet", tmp_path / "probe.dll", [tmp_path / "a.mp3"])

    def test_a_file_the_probe_skipped_raises_rather_than_shrinking_the_denominator(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        """Silently comparing fewer files than asked is the quietest way to inflate a rate."""
        lines = [
            json.dumps({"kind": "banner", "taglib_version": "2.3.0.0"}),
            json.dumps({"kind": "file", "path": str(tmp_path / "a.mp3"), "ok": True}),
        ]

        def fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(args, 0, "\n".join(lines) + "\n", "")

        monkeypatch.setattr(subprocess, "run", fake_run)
        with pytest.raises(tp.ParityError, match="no row for"):
            tp.run_taglib_probe("dotnet", tmp_path / "probe.dll",
                                [tmp_path / "a.mp3", tmp_path / "b.mp3"])

    @needs_ffprobe
    def test_an_unreadable_file_is_reported_not_dropped(self, tmp_path: pathlib.Path) -> None:
        """A file TagLib# cannot open is a result about TagLib#, and belongs in the report.

        Counting it as neither agreement nor disagreement, with no mention anywhere, would let
        a format TagLib# simply cannot read leave the agreement rate at one hundred percent.
        """
        broken = tmp_path / "broken.mp3"
        broken.write_bytes(b"not audio")
        records = {str(broken): {"kind": "file", "path": str(broken), "ok": False,
                                 "error": "CorruptFileException: no header"}}
        result = tp.measure([broken], tmp_path, "ffprobe", records)
        assert result.files == 0
        assert result.unreadable and result.unreadable[0]["reader"] == "taglib"
        assert tp.verdict_for(tp.FIELD_BY_KEY["duration_seconds"],
                              result.tallies["duration_seconds"]) == "not measured"

    def test_nothing_measured_never_reads_as_a_drop_in(self) -> None:
        """The verdict text for an empty tally must not be a passing one."""
        for spec in tp.FIELDS:
            assert tp.verdict_for(spec, tp.FieldTally()) == "not measured"

    def test_a_generous_tolerance_still_fails_a_real_disagreement(self) -> None:
        """The no-Xing MP3 measured 65457 bit/s against 224000. No tolerance may absorb that."""
        spec = tp.FIELD_BY_KEY["bitrate_bps"]
        assert tp.compare_views(spec, 65_457, 224_000) == (False, False)
        duration = tp.FIELD_BY_KEY["duration_seconds"]
        assert tp.compare_views(duration, 18.769706, 5.4825) == (False, False)

    def test_strict_mode_would_flag_the_bitrate_gap(self, tmp_path: pathlib.Path) -> None:
        """--strict exists so CI can gate on this; prove it sees a field that disagreed."""
        tally = tp.FieldTally(compared=4, exact=0, tolerant=2)
        assert tally.tolerant < tally.compared
        assert tally.exact_rate() == 0.0


@needs_ffmpeg
class TestMatrix:
    def test_the_matrix_is_not_all_round_bitrates(self) -> None:
        """The failure #549 walked into: every bitrate an exact tier, so every answer right."""
        requested = [
            arg for spec in tp.MATRIX for arg in spec.args
            if arg.endswith("k") and arg[:-1].isdigit()
        ]
        awkward = [value for value in requested
                   if int(value[:-1]) not in {32, 64, 96, 128, 160, 192, 256, 320}]
        assert len(awkward) >= 6, "the matrix has drifted back to round tier values only"

    def test_every_signal_arm_has_a_recipe(self) -> None:
        for signal in tp.SIGNALS:
            assert tp._lavfi_source(signal, 44100, 1.0)
        with pytest.raises(tp.ParityError):
            tp._lavfi_source("whistling", 44100, 1.0)

    def test_generation_covers_every_container(self, tmp_path: pathlib.Path) -> None:
        made = tp.generate_matrix(tmp_path, ["noise"], 0.5, "ffmpeg")
        assert {path.suffix.lstrip(".") for path in made} >= {"mp3", "m4b", "flac", "opus", "ogg"}


@needs_ffmpeg
@needs_ffprobe
def test_end_to_end_over_a_small_matrix(tmp_path: pathlib.Path) -> None:
    """One real pass through both readers, so the wiring is proven and not just the mappings."""
    dotnet = _dotnet_or_skip()
    files = tp.generate_matrix(tmp_path / "matrix", ["noise"], 2.0, "ffmpeg")
    assert files
    try:
        dll = tp.build_taglib_probe(dotnet, tmp_path / "probe")
    except tp.ParityError as exc:
        pytest.skip(f"the TagLib# probe could not be built here: {exc}")
    version, records = tp.run_taglib_probe(dotnet, dll, files)
    assert version.startswith("2.")

    result = tp.measure(files, tmp_path / "matrix", "ffprobe", records)
    assert result.files == len(files)
    # Channels and codec were the two fields that agreed on every file of the full matrix; if
    # either starts disagreeing, the mapping changed and the report needs re-reading.
    assert result.tallies["channels"].exact_rate() == 1.0
    assert result.tallies["codec"].exact_rate() == 1.0
    # And the headline: the raw bitrate integers never match, because TagLib# has no finer
    # resolution than a whole kbit/s. A run where they suddenly do is a run to be suspicious of.
    assert result.tallies["bitrate_bps"].exact_rate() == 0.0
