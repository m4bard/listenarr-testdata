"""install_ffbinary pulls a release asset, verifies it against GitHub's digest, then places it.

Offline: an injected digest_lookup stands in for the GitHub API and a fetcher writes a fixture zip
(the shape package_ffbinary ships — binary + manifest.json). Load-bearing: a zip whose bytes don't
match GitHub's recorded digest is REFUSED, and a binary that doesn't match its in-zip manifest is
REFUSED — either way nothing is written.
"""
from __future__ import annotations

import hashlib
import io
import json
import pathlib
import sys
import zipfile
from collections.abc import Callable

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

from install_ffbinary import VerificationError, api_asset_digest, install, release_asset_url


def _zip_bytes(program: str, rid: str, payload: bytes, manifest_sha: str | None = None) -> bytes:
    binfile = f"{program}{'.exe' if rid.startswith('win') else ''}"
    sha = manifest_sha if manifest_sha is not None else hashlib.sha256(payload).hexdigest()
    manifest = {"program": program, "artifacts": [{"rid": rid, "file": binfile, "sha256": sha}]}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(binfile, payload)
        zf.writestr("manifest.json", json.dumps(manifest))
    return buf.getvalue()


def _serving(
    zip_bytes: bytes, digest: str | None = None,
) -> tuple[Callable[[str, pathlib.Path], None], Callable[[str, str, str], str]]:
    """A (fetcher, digest_lookup) pair; digest overrides GitHub's reported sha."""
    reported = digest if digest is not None else hashlib.sha256(zip_bytes).hexdigest()

    def fetch(url: str, dest: pathlib.Path) -> None:
        dest.write_bytes(zip_bytes)

    def lookup(repo: str, tag: str, asset: str) -> str:
        return reported

    return fetch, lookup


def test_release_asset_url_latest_and_pinned() -> None:
    assert release_asset_url("o/r", "latest", "ffmpeg-linux-x64.zip") == \
        "https://github.com/o/r/releases/latest/download/ffmpeg-linux-x64.zip"
    assert release_asset_url("o/r", "v0.2.0", "ffmpeg-linux-x64.zip") == \
        "https://github.com/o/r/releases/download/v0.2.0/ffmpeg-linux-x64.zip"


def test_installs_when_digest_and_manifest_match(tmp_path: pathlib.Path) -> None:
    z = _zip_bytes("ffmpeg", "linux-x64", b"pretend-ffmpeg")
    fetch, lookup = _serving(z)
    dest = tmp_path / "bin" / "ffmpeg"
    placed = install("ffmpeg", dest, rid="linux-x64", fetcher=fetch, digest_lookup=lookup)
    assert placed == dest
    assert dest.read_bytes() == b"pretend-ffmpeg"


def test_windows_asset_and_exe_name(tmp_path: pathlib.Path) -> None:
    z = _zip_bytes("ffprobe", "win-x64", b"win-probe")
    seen: dict[str, str] = {}

    def fetch(url: str, dest: pathlib.Path) -> None:
        seen["url"] = url
        dest.write_bytes(z)

    def lookup(repo: str, tag: str, asset: str) -> str:
        seen["asset"] = asset
        return hashlib.sha256(z).hexdigest()

    dest = tmp_path / "ffprobe.exe"
    install("ffprobe", dest, rid="win-x64", tag="v0.2.0", repo="o/r",
            fetcher=fetch, digest_lookup=lookup)
    assert seen["url"].endswith("/download/v0.2.0/ffprobe-win-x64.zip")
    assert seen["asset"] == "ffprobe-win-x64.zip"
    assert dest.read_bytes() == b"win-probe"


def test_zip_not_matching_github_digest_is_refused(tmp_path: pathlib.Path) -> None:
    # GitHub reports one sha; the bytes we downloaded hash to another -> refuse (the authoritative
    # out-of-band check), and write nothing.
    z = _zip_bytes("ffmpeg", "linux-x64", b"real-bytes")
    fetch, _ = _serving(z)
    _, wrong_lookup = _serving(z, digest="0" * 64)
    dest = tmp_path / "bin" / "ffmpeg"
    with pytest.raises(VerificationError):
        install("ffmpeg", dest, rid="linux-x64", fetcher=fetch, digest_lookup=wrong_lookup)
    assert not dest.exists()


def test_binary_not_matching_its_manifest_is_refused(tmp_path: pathlib.Path) -> None:
    # The zip matches GitHub's digest (authentic download), but the binary inside doesn't match the
    # sha its own manifest records -> defense-in-depth refusal.
    z = _zip_bytes("ffmpeg", "linux-x64", b"real-bytes", manifest_sha="0" * 64)
    fetch, lookup = _serving(z)  # digest = sha of this (internally inconsistent) zip
    dest = tmp_path / "bin" / "ffmpeg"
    with pytest.raises(VerificationError):
        install("ffmpeg", dest, rid="linux-x64", fetcher=fetch, digest_lookup=lookup)
    assert not dest.exists()


class _Resp:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _Resp:
        return self

    def __exit__(self, *a: object) -> None:
        return None


def test_api_asset_digest_parses_and_strips_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    body = json.dumps({"assets": [
        {"name": "ffmpeg-linux-x64.zip", "digest": "sha256:" + "a" * 64},
        {"name": "other.zip", "digest": "sha256:" + "b" * 64},
    ]}).encode()
    monkeypatch.setattr("urllib.request.urlopen", lambda req: _Resp(body))
    assert api_asset_digest("o/r", "v0.2.0", "ffmpeg-linux-x64.zip") == "a" * 64


def test_api_asset_digest_missing_asset_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("urllib.request.urlopen", lambda req: _Resp(b'{"assets": []}'))
    with pytest.raises(VerificationError):
        api_asset_digest("o/r", "latest", "ffmpeg-linux-x64.zip")
