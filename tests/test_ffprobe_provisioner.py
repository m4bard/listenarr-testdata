"""The ffprobe provisioner: a thin wrapper that drops a pinned, verified ffprobe for the benchmark.

Since the refactor the provisioner delegates all downloading, pinning and the sha256-before-extract
safety contract to ``ffmpeg_harness`` (covered by test_ffmpeg_harness.py). What remains to test here
is the wrapper's own job: call the harness for ffprobe and place the result at the exact path
Listenarr checks — ``<config>/ffmpeg/ffprobe`` — executable. These tests run offline by
monkeypatching the harness so no download happens.
"""
from __future__ import annotations

import pathlib
import stat
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import ffmpeg_harness
from ffprobe_provisioner import DEFAULT_SOURCE, provision_config


def test_default_source_is_jellyfin() -> None:
    # jellyfin is the one source covering every platform Listenarr ships — the demonstrated path.
    assert DEFAULT_SOURCE == "jellyfin"


def test_provision_places_ffprobe_where_listenarr_looks(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Stub the harness so no download happens; assert the wrapper copies its result to
    # <config>/ffmpeg/ffprobe — the exact path Listenarr checks (File.Exists) before downloading.
    provisioned = tmp_path / "cache" / "ffprobe"
    provisioned.parent.mkdir(parents=True)
    provisioned.write_bytes(b"provisioned-ffprobe")

    def fake_provision(
        binary: str, source: str, rid: str | None, cache_dir: object
    ) -> pathlib.Path:
        assert binary == "ffprobe"
        return provisioned

    monkeypatch.setattr(ffmpeg_harness, "provision", fake_provision)

    config = tmp_path / "config"
    dest = provision_config(config, cache_dir=tmp_path / "cache")
    assert dest == config / "ffmpeg" / "ffprobe"
    assert dest.read_bytes() == b"provisioned-ffprobe"
    assert dest.stat().st_mode & stat.S_IXUSR  # placed executable


def test_source_and_rid_are_passed_through(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, object] = {}
    provisioned = tmp_path / "ffprobe"
    provisioned.write_bytes(b"x")

    def fake_provision(
        binary: str, source: str, rid: str | None, cache_dir: object
    ) -> pathlib.Path:
        seen.update(binary=binary, source=source, rid=rid)
        return provisioned

    monkeypatch.setattr(ffmpeg_harness, "provision", fake_provision)

    provision_config(tmp_path / "config", source="johnvansickle", rid="linux-arm64")
    assert seen == {"binary": "ffprobe", "source": "johnvansickle", "rid": "linux-arm64"}
