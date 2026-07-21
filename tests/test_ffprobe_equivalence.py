"""The ffprobe differential-equivalence check — the gate for swapping/updating the ffmpeg source.

Two safety properties matter, and both are contracts: it must FLAG a change in any field Listenarr
actually reads (or an update could silently change behaviour), and it must IGNORE differences in the
rest of ffprobe's output (build strings, encoder tags, demuxer lists) so it doesn't cry wolf. These
run offline by feeding canned ffprobe JSON through the extraction + compare logic.
"""
from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import ffprobe_equivalence as eq


def test_functional_view_takes_exactly_the_fields_listenarr_reads() -> None:
    probe_json = {
        # size / nb_streams are outside the read set and must be ignored.
        "format": {"duration": "60.0", "format_name": "mov,mp4,m4a", "bit_rate": "128000",
                   "tags": {"title": "She"}, "size": "999", "nb_streams": 1},
        "streams": [
            {"codec_type": "video", "codec_name": "mjpeg"},  # cover art stream — must be ignored
            {"codec_type": "audio", "sample_rate": "22050", "channels": 1, "bit_rate": "128000",
             "codec_name": "aac", "tags": {"language": "eng"}, "profile": "LC"},  # profile ignored
        ],
    }
    view = eq.functional_view(probe_json)
    assert view == {
        "format.duration": "60.0", "format.format_name": "mov,mp4,m4a",
        "format.bit_rate": "128000", "format.tags": {"title": "She"},
        "stream.sample_rate": "22050", "stream.channels": 1, "stream.bit_rate": "128000",
        "stream.codec_name": "aac", "stream.tags": {"language": "eng"},
    }


@pytest.mark.contract
class TestEquivalenceContract:
    def _fake_probe(self, monkeypatch: pytest.MonkeyPatch,
                    by_binary: dict[str, dict]) -> None:
        def fake(ffprobe: str, file: pathlib.Path) -> dict:
            return by_binary[ffprobe]
        monkeypatch.setattr(eq, "probe", fake)

    def test_identical_functional_fields_are_equivalent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        j = {"format": {"duration": "1.0", "tags": {"title": "x"}},
             "streams": [{"codec_type": "audio", "codec_name": "aac"}]}
        self._fake_probe(monkeypatch, {"A": j, "B": json_copy(j)})
        assert eq.compare("A", "B", [pathlib.Path("s.m4b")]) == []

    def test_a_change_in_a_read_field_is_flagged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        base = {"format": {"duration": "1.0"},
                "streams": [{"codec_type": "audio", "codec_name": "aac"}]}
        cand = {"format": {"duration": "1.0"},
                "streams": [{"codec_type": "audio", "codec_name": "mp3"}]}
        self._fake_probe(monkeypatch, {"A": base, "B": cand})
        diffs = eq.compare("A", "B", [pathlib.Path("s.m4b")])
        assert len(diffs) == 1
        assert diffs[0].field == "stream.codec_name"
        assert (diffs[0].baseline, diffs[0].candidate) == ("aac", "mp3")

    def test_noise_outside_the_read_fields_is_ignored(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Two builds' outputs differ in fields Listenarr never reads (size, nb_streams, start_pts,
        # start_time). Flagging these would block every equivalent update, so they must be ignored.
        base = {"format": {"duration": "1.0", "size": "1000", "nb_streams": 1},
                "streams": [{"codec_type": "audio", "codec_name": "aac", "start_pts": 0,
                             "start_time": "0.000000"}]}
        cand = {"format": {"duration": "1.0", "size": "1050", "nb_streams": 1},
                "streams": [{"codec_type": "audio", "codec_name": "aac", "start_pts": 1024,
                             "start_time": "0.046440"}]}
        self._fake_probe(monkeypatch, {"A": base, "B": cand})
        assert eq.compare("A", "B", [pathlib.Path("s.m4b")]) == []


def json_copy(obj: dict) -> dict:
    import json
    copied: dict = json.loads(json.dumps(obj))
    return copied
