"""install_ffbinary pulls a binary from a listenarr-testdata release and verifies it before placing.

Offline: an injected fetcher writes a fixture release zip (built to the same shape package_ffbinary
ships — the binary plus a manifest.json recording its sha256). The load-bearing property is that a
binary whose bytes don't match the manifest sha256 is REFUSED and nothing is written.
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import sys
import zipfile
from collections.abc import Callable

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

from install_ffbinary import VerificationError, install, release_asset_url


def _release_zip(path: pathlib.Path, program: str, rid: str, payload: bytes,
                 manifest_sha: str | None = None) -> None:
    """A release bundle (<program>[.exe] + manifest.json). manifest_sha overrides the hash."""
    binfile = f"{program}{'.exe' if rid.startswith('win') else ''}"
    sha = manifest_sha if manifest_sha is not None else hashlib.sha256(payload).hexdigest()
    manifest = {"program": program, "artifacts": [{"rid": rid, "file": binfile, "sha256": sha}]}
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(binfile, payload)
        zf.writestr("manifest.json", json.dumps(manifest))


def _fetcher_serving(program: str, rid: str, payload: bytes, manifest_sha: str | None = None,
                     ) -> Callable[[str, pathlib.Path], None]:
    def fetch(url: str, dest: pathlib.Path) -> None:
        _release_zip(dest, program, rid, payload, manifest_sha)
    return fetch


def test_release_asset_url_latest_and_pinned() -> None:
    assert release_asset_url("o/r", "latest", "ffmpeg-linux-x64.zip") == \
        "https://github.com/o/r/releases/latest/download/ffmpeg-linux-x64.zip"
    assert release_asset_url("o/r", "v0.2.0", "ffmpeg-linux-x64.zip") == \
        "https://github.com/o/r/releases/download/v0.2.0/ffmpeg-linux-x64.zip"


def test_installs_and_verifies_then_places(tmp_path: pathlib.Path) -> None:
    payload = b"pretend-ffmpeg-binary"
    dest = tmp_path / "bin" / "ffmpeg"
    placed = install("ffmpeg", dest, rid="linux-x64",
                     fetcher=_fetcher_serving("ffmpeg", "linux-x64", payload))
    assert placed == dest
    assert dest.read_bytes() == payload


def test_windows_asset_and_exe_name(tmp_path: pathlib.Path) -> None:
    # rid win-x64 must fetch <program>-win-x64.zip and read ffprobe.exe from it.
    seen = {}

    def fetch(url: str, dest: pathlib.Path) -> None:
        seen["url"] = url
        _release_zip(dest, "ffprobe", "win-x64", b"win-probe")
    dest = tmp_path / "ffprobe.exe"
    install("ffprobe", dest, rid="win-x64", tag="v0.2.0", repo="o/r", fetcher=fetch)
    assert seen["url"].endswith("/download/v0.2.0/ffprobe-win-x64.zip")
    assert dest.read_bytes() == b"win-probe"


def test_tampered_binary_is_refused_and_nothing_written(tmp_path: pathlib.Path) -> None:
    # The zip's binary bytes don't match the sha256 its manifest records -> refuse, write nothing.
    wrong_sha = "0" * 64
    dest = tmp_path / "bin" / "ffmpeg"
    bad = _fetcher_serving("ffmpeg", "linux-x64", b"tampered", manifest_sha=wrong_sha)
    with pytest.raises(VerificationError):
        install("ffmpeg", dest, rid="linux-x64", fetcher=bad)
    assert not dest.exists()


def test_manifest_without_this_rid_raises(tmp_path: pathlib.Path) -> None:
    def fetch(url: str, dest: pathlib.Path) -> None:
        # A bundle whose manifest describes a different RID than the one we asked to install.
        _release_zip(dest, "ffmpeg", "linux-arm64", b"arm-bytes")
    dest = tmp_path / "ffmpeg"
    with pytest.raises(VerificationError):
        install("ffmpeg", dest, rid="linux-x64", fetcher=fetch)
    assert not dest.exists()
