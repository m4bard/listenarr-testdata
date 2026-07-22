"""fetch_ffprobe pulls the ffprobe binary out of a build archive, cross-platform.

Offline: tiny fixture archives stand in for the real downloads. The property that matters is that it
finds ffprobe by basename wherever it sits (nested in a tar, top-level in a zip, named .exe), and
that a sha256 mismatch raises rather than extracting an unverified binary.
"""
from __future__ import annotations

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

from fetch_ffprobe import ChecksumError, extract_ffprobe, fetch


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
