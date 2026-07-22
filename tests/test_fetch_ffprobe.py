"""fetch_ffprobe pulls the ffprobe binary out of a build archive, cross-platform.

Offline: tiny fixture archives stand in for the real downloads. The property that matters is that it
finds ffprobe by basename wherever it sits (nested in a tar, top-level in a zip, named .exe), and
that a sha256 mismatch raises rather than extracting an unverified binary.
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

from fetch_ffprobe import ChecksumError, NoFfprobeError, extract_ffprobe, fetch


def _tar_xz(tmp: pathlib.Path, member: str, payload: bytes) -> pathlib.Path:
    inner = io.BytesIO()
    with tarfile.open(fileobj=inner, mode="w") as tf:
        info = tarfile.TarInfo(member)
        info.size = len(payload)
        tf.addfile(info, io.BytesIO(payload))
    archive = tmp / "build.tar.xz"
    archive.write_bytes(lzma.compress(inner.getvalue()))
    return archive


def _zip(tmp: pathlib.Path, member: str, payload: bytes) -> pathlib.Path:
    archive = tmp / "build.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr(member, payload)
    return archive


def test_extracts_ffprobe_nested_in_a_tarball(tmp_path: pathlib.Path) -> None:
    archive = _tar_xz(tmp_path, "ffmpeg-7.0.2-amd64-static/ffprobe", b"the-binary")
    dest = extract_ffprobe(archive, tmp_path / "out" / "ffprobe")
    assert dest.read_bytes() == b"the-binary"
    assert dest.stat().st_mode & stat.S_IXUSR


def test_extracts_ffprobe_exe_from_a_zip(tmp_path: pathlib.Path) -> None:
    archive = _zip(tmp_path, "ffprobe.exe", b"windows-binary")
    dest = extract_ffprobe(archive, tmp_path / "out" / "ffprobe.exe")
    assert dest.read_bytes() == b"windows-binary"


def test_ignores_ffmpeg_and_finds_only_ffprobe(tmp_path: pathlib.Path) -> None:
    # A build ships ffmpeg alongside ffprobe; we must pick ffprobe, not the first binary.
    archive = tmp_path / "both.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("bin/ffmpeg", b"not-this")
        zf.writestr("bin/ffprobe", b"this-one")
    dest = extract_ffprobe(archive, tmp_path / "ffprobe")
    assert dest.read_bytes() == b"this-one"


def test_an_archive_without_ffprobe_reports_it(tmp_path: pathlib.Path) -> None:
    # Exactly Listenarr's current macOS source: an ffmpeg archive with no ffprobe. The harness must
    # say so clearly (it's a real finding), not throw a stray StopIteration.
    archive = _zip(tmp_path, "ffmpeg", b"only-ffmpeg-here")
    with pytest.raises(NoFfprobeError):
        extract_ffprobe(archive, tmp_path / "ffprobe")


def test_sha256_mismatch_raises_and_does_not_extract(tmp_path: pathlib.Path,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    archive = _tar_xz(tmp_path, "pkg/ffprobe", b"payload")

    def fake_urlretrieve(url: str, dest: str) -> None:
        pathlib.Path(dest).write_bytes(archive.read_bytes())
    monkeypatch.setattr("urllib.request.urlretrieve", fake_urlretrieve)

    out = tmp_path / "out" / "ffprobe"
    with pytest.raises(ChecksumError):
        fetch("https://example/x.tar.xz", out, sha256="0" * 64)
    assert not out.exists()


def test_matching_sha256_verifies_and_extracts(tmp_path: pathlib.Path,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    # The positive path: a correct sha256 passes verification and the binary is extracted.
    archive = _tar_xz(tmp_path, "pkg/ffprobe", b"verified-payload")
    good_sha = hashlib.sha256(archive.read_bytes()).hexdigest()

    def fake_urlretrieve(url: str, dest: str) -> None:
        pathlib.Path(dest).write_bytes(archive.read_bytes())
    monkeypatch.setattr("urllib.request.urlretrieve", fake_urlretrieve)

    out = tmp_path / "out" / "ffprobe"
    dest = fetch("https://example/x.tar.xz", out, sha256=good_sha)
    assert dest == out
    assert out.read_bytes() == b"verified-payload"


def test_corrupt_tarball_raises_readerror(tmp_path: pathlib.Path) -> None:
    # A truncated/garbage tar must fail cleanly as a tarfile.ReadError, not a stray error.
    archive = tmp_path / "build.tar.xz"
    archive.write_bytes(b"this is not a valid tar.xz archive at all")
    with pytest.raises(tarfile.ReadError):
        extract_ffprobe(archive, tmp_path / "out" / "ffprobe")


def test_corrupt_zip_raises_badzipfile(tmp_path: pathlib.Path) -> None:
    # A corrupt .zip must fail cleanly as a zipfile.BadZipFile.
    archive = tmp_path / "build.zip"
    archive.write_bytes(b"PK\x03\x04 but the rest is garbage, not a real zip")
    with pytest.raises(zipfile.BadZipFile):
        extract_ffprobe(archive, tmp_path / "out" / "ffprobe.exe")
