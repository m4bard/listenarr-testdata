"""ffmpeg_harness: the shared, source- and binary-agnostic provisioner both ffprobe_provisioner and
package_ffprobe now delegate to.

The load-bearing property is the safety contract: an archive whose sha256 does not match its pin is
REFUSED and nothing is written. Around that, these tests exercise every real branch of member
selection (flat vs nested tar member, zip dispatch, wrong member) and the offline guards
(unsupported target, warm cache, host RID mapping) — all without touching the network. Tiny fixture
archives with REAL sha256s stand in for the pinned downloads, following the test_fetch_ffprobe
pattern.
"""
from __future__ import annotations

import hashlib
import io
import lzma
import pathlib
import stat
import sys
import tarfile
import zipfile

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

from ffmpeg_harness import (
    Archive,
    ChecksumError,
    UnsupportedTarget,
    host_rid,
    provision,
    verify_and_extract,
)


def _tar_xz(tmp: pathlib.Path, members: dict[str, bytes]) -> pathlib.Path:
    """A .tar.xz holding each ``name -> payload`` — stands in for a real ffmpeg-family archive."""
    inner = io.BytesIO()
    with tarfile.open(fileobj=inner, mode="w") as tf:
        for name, payload in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            tf.addfile(info, io.BytesIO(payload))
    archive = tmp / "build.tar.xz"
    archive.write_bytes(lzma.compress(inner.getvalue()))
    return archive


def _zip(tmp: pathlib.Path, members: dict[str, bytes]) -> pathlib.Path:
    archive = tmp / "build.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        for name, payload in members.items():
            zf.writestr(name, payload)
    return archive


def _sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class TestChecksumContract:
    def test_mismatch_raises_and_writes_nothing(self, tmp_path: pathlib.Path) -> None:
        archive = _tar_xz(tmp_path, {"ffmpeg": b"not really ffmpeg"})
        arc = Archive(url="https://example/x.tar.xz", sha256="0" * 64, member="{binary}")
        dest = tmp_path / "out" / "ffmpeg"
        with pytest.raises(ChecksumError) as exc:
            verify_and_extract(archive, arc, "ffmpeg", dest)
        assert "mismatch" in str(exc.value)
        assert not dest.exists()  # nothing written on a bad hash


class TestFlatMemberSelection:
    """jellyfin-style flat member ``{binary}``: one archive holds both binaries at its root."""

    def test_extracts_ffmpeg_from_flat_tar(self, tmp_path: pathlib.Path) -> None:
        archive = _tar_xz(tmp_path, {"ffmpeg": b"ffmpeg-bytes", "ffprobe": b"ffprobe-bytes"})
        arc = Archive(url="https://ex/x.tar.xz", sha256=_sha256(archive), member="{binary}")
        dest = tmp_path / "out" / "ffmpeg"
        result = verify_and_extract(archive, arc, "ffmpeg", dest)
        assert result == dest
        assert dest.read_bytes() == b"ffmpeg-bytes"
        assert dest.stat().st_mode & stat.S_IXUSR

    def test_extracts_ffprobe_from_the_same_archive(self, tmp_path: pathlib.Path) -> None:
        # Same fixture, different requested binary — proves member selection picks the right one.
        archive = _tar_xz(tmp_path, {"ffmpeg": b"ffmpeg-bytes", "ffprobe": b"ffprobe-bytes"})
        arc = Archive(url="https://ex/x.tar.xz", sha256=_sha256(archive), member="{binary}")
        dest = tmp_path / "out" / "ffprobe"
        verify_and_extract(archive, arc, "ffprobe", dest)
        assert dest.read_bytes() == b"ffprobe-bytes"


class TestNestedMemberSelection:
    """johnvansickle-style nested member ``pkg/{binary}`` under a versioned directory."""

    def test_extracts_from_nested_tar(self, tmp_path: pathlib.Path) -> None:
        archive = _tar_xz(tmp_path, {"pkg/ffmpeg": b"nested-mpeg", "pkg/ffprobe": b"nested-probe"})
        arc = Archive(url="https://ex/x.tar.xz", sha256=_sha256(archive), member="pkg/{binary}")
        dest = tmp_path / "out" / "ffmpeg"
        verify_and_extract(archive, arc, "ffmpeg", dest)
        assert dest.read_bytes() == b"nested-mpeg"


class TestZipDispatch:
    def test_extracts_exe_from_zip_by_basename(self, tmp_path: pathlib.Path) -> None:
        # jellyfin Windows ships a .zip; the member is matched by basename wherever it sits.
        archive = _zip(tmp_path, {"bin/ffmpeg.exe": b"win-ffmpeg", "bin/ffprobe.exe": b"win-probe"})
        arc = Archive(url="https://ex/x.zip", sha256=_sha256(archive), member="{binary}")
        dest = tmp_path / "out" / "ffprobe.exe"
        verify_and_extract(archive, arc, "ffprobe.exe", dest)
        assert dest.read_bytes() == b"win-probe"


class TestMissingMember:
    def test_absent_member_raises_unsupported_target(self, tmp_path: pathlib.Path) -> None:
        # A source archive that does not carry the requested binary (e.g. an ffmpeg-only build) must
        # surface clearly as UnsupportedTarget, not a stray error. The zip branch does this by name
        # lookup returning nothing; the fixture ships ffmpeg only and we ask for ffprobe.
        archive = _zip(tmp_path, {"bin/ffmpeg.exe": b"only-ffmpeg"})
        arc = Archive(url="https://ex/x.zip", sha256=_sha256(archive), member="{binary}")
        dest = tmp_path / "out" / "ffprobe.exe"
        with pytest.raises(UnsupportedTarget):
            verify_and_extract(archive, arc, "ffprobe.exe", dest)
        assert not dest.exists()

    def test_absent_member_in_tar_also_raises_unsupported_target(
        self, tmp_path: pathlib.Path
    ) -> None:
        # The tar branch must match the zip branch: an absent member is a clean UnsupportedTarget,
        # not a raw KeyError from tarfile.getmember. (Regression guard for that asymmetry.)
        archive = _tar_xz(tmp_path, {"ffmpeg": b"only-ffmpeg"})
        arc = Archive(url="https://ex/x.tar.xz", sha256=_sha256(archive), member="{binary}")
        dest = tmp_path / "out" / "ffprobe"
        with pytest.raises(UnsupportedTarget):
            verify_and_extract(archive, arc, "ffprobe", dest)
        assert not dest.exists()


class TestProvisionGuards:
    def test_unsupported_target_fails_before_any_download(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # johnvansickle is Linux-only: an osx-x64 request must fail on the SOURCES lookup, before
        # a single byte is fetched. Make any download attempt an outright test failure.
        def no_network(*_a: object, **_k: object) -> None:
            raise AssertionError("provision must not download for an unsupported target")
        monkeypatch.setattr("urllib.request.urlretrieve", no_network)
        with pytest.raises(UnsupportedTarget):
            provision("ffprobe", source="johnvansickle", rid="osx-x64", cache_dir=tmp_path)

    def test_warm_cache_is_returned_without_downloading(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def no_network(*_a: object, **_k: object) -> None:
            raise AssertionError("a warm cache must not trigger a download")
        monkeypatch.setattr("urllib.request.urlretrieve", no_network)
        dest = tmp_path / "jellyfin" / "linux-x64" / "ffmpeg"
        dest.parent.mkdir(parents=True)
        dest.write_bytes(b"cached-ffmpeg")
        got = provision("ffmpeg", source="jellyfin", rid="linux-x64", cache_dir=tmp_path)
        assert got == dest
        assert got.read_bytes() == b"cached-ffmpeg"


class TestHostRid:
    def test_known_platforms_map(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cases = [
            ("Linux", "x86_64", "linux-x64"),
            ("Linux", "aarch64", "linux-arm64"),
            ("Darwin", "arm64", "osx-x64"),  # Listenarr ships osx-x64; Apple Silicon runs Rosetta
            ("Windows", "AMD64", "win-x64"),
        ]
        for system, machine, expected in cases:
            monkeypatch.setattr("platform.system", lambda s=system: s)
            monkeypatch.setattr("platform.machine", lambda m=machine: m)
            assert host_rid() == expected

    def test_unknown_platform_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("platform.system", lambda: "Plan9")
        monkeypatch.setattr("platform.machine", lambda: "risc")
        with pytest.raises(UnsupportedTarget):
            host_rid()
