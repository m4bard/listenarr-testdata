"""package_ffprobe lays out a per-RID ffprobe artifact set + sha256 manifest.

Offline: an injected fetcher stands in for the network download, writing a distinct payload per
RID so we can prove each artifact is recorded with its own hash and size, and that the manifest
maps every Listenarr RID to the jellyfin-ffmpeg asset it came from.
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import sys
from collections.abc import Callable

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import zipfile

from package_ffprobe import (
    PINS,
    TARGETS,
    ChecksumError,
    bundle_zips,
    package,
    record_artifact,
)


def _fake_fetcher_factory(
    payloads: dict[str, bytes],
) -> Callable[[str, pathlib.Path, str | None], pathlib.Path]:
    """A fetcher that writes a per-URL payload to dest, imitating download+extract offline."""
    def fetch(url: str, dest: pathlib.Path, sha256: str | None = None) -> pathlib.Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        # The URL is base + asset, so match by the asset it ends with.
        payload = next((p for a, p in payloads.items() if url.endswith(a)), b"default-binary")
        dest.write_bytes(payload)
        return dest
    return fetch


def test_bundle_zips_carry_the_binary_and_manifest(tmp_path: pathlib.Path) -> None:
    payloads = {t["asset"]: f"ffprobe-for-{t['rid']}".encode() for t in TARGETS}
    manifest = package(tmp_path, fetcher=_fake_fetcher_factory(payloads))

    zips = bundle_zips(tmp_path, manifest)
    assert {z.name for z in zips} == {f"ffprobe-{t['rid']}.zip" for t in TARGETS}

    # The Windows bundle must carry ffprobe.exe (not ffprobe) plus the verifiable manifest.
    win = tmp_path / "ffprobe-win-x64.zip"
    with zipfile.ZipFile(win) as zf:
        names = set(zf.namelist())
        assert names == {"ffprobe.exe", "manifest.json"}
        assert zf.read("ffprobe.exe") == b"ffprobe-for-win-x64"
        assert json.loads(zf.read("manifest.json"))["source"] == "jellyfin/jellyfin-ffmpeg"

    # A non-Windows bundle carries the extensionless binary.
    lin = tmp_path / "ffprobe-linux-x64.zip"
    with zipfile.ZipFile(lin) as zf:
        assert "ffprobe" in zf.namelist()


def test_packages_every_rid_with_its_own_hash(tmp_path: pathlib.Path) -> None:
    payloads = {t["asset"]: f"ffprobe-for-{t['rid']}".encode() for t in TARGETS}
    manifest = package(tmp_path, fetcher=_fake_fetcher_factory(payloads))

    artifacts = manifest["artifacts"]
    assert isinstance(artifacts, list)
    rids = {a["rid"] for a in artifacts}
    assert rids == {t["rid"] for t in TARGETS}

    for art in artifacts:
        placed = tmp_path / art["rid"] / art["file"]
        assert placed.exists()
        expected = hashlib.sha256(f"ffprobe-for-{art['rid']}".encode()).hexdigest()
        assert art["sha256"] == expected
        assert art["bytes"] == len(f"ffprobe-for-{art['rid']}".encode())
        # The manifest records the verified archive pin alongside the extracted-binary hash.
        assert art["archive_sha256"] == PINS[art["rid"]]


def test_windows_artifact_keeps_exe_extension(tmp_path: pathlib.Path) -> None:
    manifest = package(tmp_path, fetcher=_fake_fetcher_factory({}))
    artifacts = manifest["artifacts"]
    assert isinstance(artifacts, list)
    win = next(a for a in artifacts if a["rid"] == "win-x64")
    assert win["file"] == "ffprobe.exe"
    assert (tmp_path / "win-x64" / "ffprobe.exe").exists()


def test_manifest_is_written_and_self_describing(tmp_path: pathlib.Path) -> None:
    package(tmp_path, fetcher=_fake_fetcher_factory({}))
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert manifest["source"] == "jellyfin/jellyfin-ffmpeg"
    assert manifest["binaries"] == ["ffprobe", "ffprobe.exe"]
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
    manifest = package(outdir, targets=[], fetcher=_fake_fetcher_factory({}))
    assert manifest["artifacts"] == []
    written = json.loads((outdir / "manifest.json").read_text())
    assert written["artifacts"] == []
    assert written["source"] == "jellyfin/jellyfin-ffmpeg"


def test_wrong_pin_raises_and_writes_no_artifact(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # With the real fetcher, a wrong archive pin must fail the sha256 check BEFORE extraction, so
    # package() raises and leaves no artifact and no manifest behind.
    def fake_urlretrieve(url: str, dest: str) -> None:
        pathlib.Path(dest).write_bytes(b"not-the-real-archive")
    monkeypatch.setattr("urllib.request.urlretrieve", fake_urlretrieve)

    wrong_pins = {t["rid"]: "0" * 64 for t in TARGETS}
    with pytest.raises(ChecksumError):
        package(tmp_path, pins=wrong_pins)

    first = TARGETS[0]
    assert not (tmp_path / first["rid"] / "ffprobe").exists()
    assert not (tmp_path / "manifest.json").exists()
