"""The ffprobe provisioner: a pinned, verified binary dropped in before the container boots.

The load-bearing property is a safety contract: a build whose hash does not match the pin must be
REFUSED, never provisioned. A test harness that silently ran an unverified ffprobe would defeat the
entire point of pinning. These tests run offline — a tiny fixture archive stands in for the real
download.
"""
from __future__ import annotations

import lzma
import pathlib
import stat
import sys
import tarfile

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

from ffprobe_provisioner import (
    PINS,
    ChecksumError,
    Pin,
    ensure_ffprobe,
    host_arch,
    provision_config,
    verify_and_extract,
)


def _make_archive(tmp_path: pathlib.Path, member: str, payload: bytes) -> pathlib.Path:
    """A minimal .tar.xz containing one file at ``member`` — stands in for a real ffprobe build."""
    inner = tmp_path / "inner.tar"
    with tarfile.open(inner, "w") as tar:
        data = tmp_path / "payload"
        data.write_bytes(payload)
        tar.add(data, arcname=member)
    archive = tmp_path / "build.tar.xz"
    archive.write_bytes(lzma.compress(inner.read_bytes()))
    inner.unlink()
    return archive


def _sha256(path: pathlib.Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


class TestHostArch:
    def test_known_archs_map(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for machine, expected in [("x86_64", "amd64"), ("amd64", "amd64"),
                                  ("aarch64", "arm64"), ("arm64", "arm64")]:
            monkeypatch.setattr("platform.machine", lambda m=machine: m)
            assert host_arch() == expected

    def test_unknown_arch_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("platform.machine", lambda: "riscv64")
        with pytest.raises(ValueError):
            host_arch()

    def test_both_shipped_archs_are_pinned(self) -> None:
        # The Listenarr Linux image ships amd64 + arm64; both must be pinned or provisioning a
        # real host would fall through to the download race this module exists to remove.
        assert set(PINS) == {"amd64", "arm64"}


@pytest.mark.contract
class TestChecksumContract:
    """A hash mismatch must RAISE and extract nothing — the pin's whole purpose."""

    def test_a_mismatch_raises_and_extracts_nothing(self, tmp_path: pathlib.Path) -> None:
        archive = _make_archive(tmp_path, "pkg/ffprobe", b"not really ffprobe")
        wrong_pin = Pin(url="https://example/x.tar.xz", sha256="0" * 64, member="pkg/ffprobe")
        dest = tmp_path / "out" / "ffprobe"
        with pytest.raises(ChecksumError) as exc:
            verify_and_extract(archive, wrong_pin, dest)
        assert "mismatch" in str(exc.value)
        assert not dest.exists()  # nothing was written on a bad hash

    def test_a_match_extracts_an_executable_binary(self, tmp_path: pathlib.Path) -> None:
        payload = b"#!/bin/sh\necho ffprobe\n"
        archive = _make_archive(tmp_path, "pkg/ffprobe", payload)
        good_pin = Pin(url="https://ex/x.tar.xz", sha256=_sha256(archive), member="pkg/ffprobe")
        dest = tmp_path / "out" / "ffprobe"
        result = verify_and_extract(archive, good_pin, dest)
        assert result == dest
        assert dest.read_bytes() == payload
        assert dest.stat().st_mode & stat.S_IXUSR  # extracted executable


class TestProvisioning:
    def test_provision_places_ffprobe_where_listenarr_looks(self, tmp_path: pathlib.Path) -> None:
        # Seed the cache so no download happens, then assert it lands at <config>/ffmpeg/ffprobe —
        # the exact path Listenarr checks (File.Exists) before falling back to a download.
        cache = tmp_path / "cache"
        (cache / "amd64").mkdir(parents=True)
        (cache / "amd64" / "ffprobe").write_bytes(b"cached-ffprobe")
        config = tmp_path / "config"
        dest = provision_config(config, arch="amd64", cache_dir=cache)
        assert dest == config / "ffmpeg" / "ffprobe"
        assert dest.read_bytes() == b"cached-ffprobe"
        assert dest.stat().st_mode & stat.S_IXUSR

    def test_a_warm_cache_is_reused_not_redownloaded(self, tmp_path: pathlib.Path) -> None:
        cache = tmp_path / "cache"
        (cache / "amd64").mkdir(parents=True)
        (cache / "amd64" / "ffprobe").write_bytes(b"warm")
        # A download would hit the network / fail offline; a warm cache just returns the binary.
        assert ensure_ffprobe(arch="amd64", cache_dir=cache).read_bytes() == b"warm"
