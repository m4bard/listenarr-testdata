"""package_ffbinary lays out a per-RID <program> artifact set + manifest, for ffprobe or ffmpeg.

Offline: an injected provider stands in for ffmpeg_harness.provision, writing a distinct payload
per (program, RID) so we can prove each artifact is recorded with its own hash and size, that the
manifest names the program, and that the bundle/zip naming follows the program. The one
network-touching path (real provision + verify-before-extract) uses a monkeypatched download.
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import sys
import zipfile

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

from package_ffbinary import (
    PINS,
    TARGETS,
    ChecksumError,
    bundle_zips,
    package,
    record_artifact,
)


def _fake_provider(program: str, source: str, rid: str, cache_dir: pathlib.Path) -> pathlib.Path:
    """Stand in for ffmpeg_harness.provision: write a per-(program, rid) payload and return it."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    binext = ".exe" if rid.startswith("win") else ""
    out = cache_dir / f"{program}{binext}--{rid}"
    out.write_bytes(f"{program}-for-{rid}".encode())
    return out


def test_bundle_zips_carry_the_binary_and_manifest(tmp_path: pathlib.Path) -> None:
    manifest = package(tmp_path, program="ffprobe", provider=_fake_provider)

    zips = bundle_zips(tmp_path, manifest)
    assert {z.name for z in zips} == {f"ffprobe-{t['rid']}.zip" for t in TARGETS}

    # The Windows bundle must carry ffprobe.exe (not ffprobe) plus the verifiable manifest.
    win = tmp_path / "ffprobe-win-x64.zip"
    with zipfile.ZipFile(win) as zf:
        assert set(zf.namelist()) == {"ffprobe.exe", "manifest.json"}
        assert zf.read("ffprobe.exe") == b"ffprobe-for-win-x64"
        assert json.loads(zf.read("manifest.json"))["program"] == "ffprobe"

    # A non-Windows bundle carries the extensionless binary.
    with zipfile.ZipFile(tmp_path / "ffprobe-linux-x64.zip") as zf:
        assert "ffprobe" in zf.namelist()


def test_ffmpeg_program_names_the_binary_and_the_zips(tmp_path: pathlib.Path) -> None:
    # The same tool, one word different: --program ffmpeg names every output for ffmpeg.
    manifest = package(tmp_path, program="ffmpeg", provider=_fake_provider)
    assert manifest["program"] == "ffmpeg"

    artifacts = manifest["artifacts"]
    assert isinstance(artifacts, list)
    win_art = next(a for a in artifacts if a["rid"] == "win-x64")
    assert win_art["file"] == "ffmpeg.exe"
    assert (tmp_path / "win-x64" / "ffmpeg.exe").exists()

    zips = bundle_zips(tmp_path, manifest)
    assert {z.name for z in zips} == {f"ffmpeg-{t['rid']}.zip" for t in TARGETS}
    with zipfile.ZipFile(tmp_path / "ffmpeg-win-x64.zip") as zf:
        assert set(zf.namelist()) == {"ffmpeg.exe", "manifest.json"}
        assert zf.read("ffmpeg.exe") == b"ffmpeg-for-win-x64"


def test_packages_every_rid_with_its_own_hash(tmp_path: pathlib.Path) -> None:
    manifest = package(tmp_path, program="ffprobe", provider=_fake_provider)

    artifacts = manifest["artifacts"]
    assert isinstance(artifacts, list)
    assert {a["rid"] for a in artifacts} == {t["rid"] for t in TARGETS}

    for art in artifacts:
        placed = tmp_path / art["rid"] / art["file"]
        assert placed.exists()
        payload = f"ffprobe-for-{art['rid']}".encode()
        assert art["sha256"] == hashlib.sha256(payload).hexdigest()
        assert art["bytes"] == len(payload)
        # The manifest records the verified archive pin alongside the extracted-binary hash.
        assert art["archive_sha256"] == PINS[art["rid"]]


def test_manifest_is_written_and_self_describing(tmp_path: pathlib.Path) -> None:
    package(tmp_path, program="ffprobe", provider=_fake_provider)
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert manifest["source"] == "jellyfin/jellyfin-ffmpeg"
    assert manifest["program"] == "ffprobe"
    assert manifest["version"] == "7.1.4-3"
    assert len(manifest["artifacts"]) == len(TARGETS)


def test_record_artifact_reports_hash_and_size(tmp_path: pathlib.Path) -> None:
    binary = tmp_path / "ffprobe"
    binary.write_bytes(b"payload-bytes")
    rec = record_artifact(binary, "linux-x64", "portable_linux64-gpl.tar.xz", "deadbeef")
    assert rec["sha256"] == hashlib.sha256(b"payload-bytes").hexdigest()
    assert rec["bytes"] == len(b"payload-bytes")
    assert rec["rid"] == "linux-x64"
    assert rec["archive_sha256"] == "deadbeef"


def test_empty_targets_writes_empty_manifest(tmp_path: pathlib.Path) -> None:
    # With no targets, no per-RID dir is created; package() must still create outdir and write a
    # manifest with an empty artifacts list rather than FileNotFoundError-ing on the write.
    outdir = tmp_path / "out"
    manifest = package(outdir, targets=[], provider=_fake_provider)
    assert manifest["artifacts"] == []
    written = json.loads((outdir / "manifest.json").read_text())
    assert written["artifacts"] == []
    assert written["source"] == "jellyfin/jellyfin-ffmpeg"


def test_bad_archive_bytes_raise_and_write_nothing(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The real extraction path (default provider = ffmpeg_harness.provision) downloads then verifies
    # against the pinned sha256 BEFORE unpacking. Feed it garbage -> mismatch -> ChecksumError, and
    # nothing lands in outdir.
    def fake_urlretrieve(url: str, dest: str) -> None:
        pathlib.Path(dest).write_bytes(b"not-the-real-archive")
    monkeypatch.setattr("urllib.request.urlretrieve", fake_urlretrieve)

    with pytest.raises(ChecksumError):
        package(tmp_path)  # default provider

    first = TARGETS[0]
    assert not (tmp_path / first["rid"] / "ffprobe").exists()
    assert not (tmp_path / "manifest.json").exists()
