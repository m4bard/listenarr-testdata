#!/usr/bin/env python3
"""Package a pinned ffmpeg-family binary as a per-platform artifact set + sha256 manifest.

Which binary is a parameter: ``--program ffprobe`` (the one binary Listenarr runs, to read
metadata) or ``--program ffmpeg`` (what listenarr-testdata needs, to *create* fixture audio). Both
ride in the same pinned jellyfin archive, so the choice only changes which member is extracted and
how the output files are named (``<program>`` / ``<program>.exe`` / ``<program>-<rid>.zip``).

For each RID Listenarr ships (linux-x64, linux-arm64, win-x64, osx-x64) this extracts just that one
binary and emits a manifest recording the sha256 and size of every artifact. The result is a small,
pinned, verifiable set a release can bundle instead of fetching an unpinned whole-ffmpeg archive per
platform at runtime.

Extraction runs through ``ffmpeg_harness.provision`` — the single verified-extract path — so the
archive is checked against its pinned sha256 BEFORE anything is unpacked, and the version + pins are
never duplicated here (they live once in ``ffmpeg_harness.SOURCES["jellyfin"]``).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import shutil
import sys
import tempfile
import urllib.request
import zipfile
from collections.abc import Callable

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import ffmpeg_harness
from ffmpeg_harness import ChecksumError

__all__ = [
    "PINS", "TARGETS", "ChecksumError", "bundle_zips", "package", "record_artifact", "verify_pins",
]

_SOURCE = "jellyfin"  # the one source covering every RID; packaging always draws from it

# jellyfin-ffmpeg: the one org-maintained, GitHub-hosted, versioned, sha256-checksummed source
# covering every RID Listenarr targets. The version and per-RID pins are NOT duplicated here — they
# live once in ffmpeg_harness.SOURCES["jellyfin"], so a re-pin happens in exactly one place and the
# fixture-building ffmpeg and the packaged ffprobe can never drift apart.
DEFAULT_VERSION = "7.1.4-3"
DEFAULT_BASE = (
    "https://github.com/jellyfin/jellyfin-ffmpeg/releases/download/"
    "v{version}/jellyfin-ffmpeg_{version}_"
)

_JELLYFIN = ffmpeg_harness.SOURCES[_SOURCE]
_PREFIX = f"jellyfin-ffmpeg_{DEFAULT_VERSION}_"


def _asset_of(url: str) -> str:
    """The release-asset tail of a pinned jellyfin URL (what follows the versioned prefix)."""
    return url.rsplit(_PREFIX, 1)[-1]


# Listenarr's shipped RIDs, derived from the shared harness pins (which mirror its csproj). No
# osx-arm64 RID — Apple Silicon runs the osx-x64 build under Rosetta.
TARGETS = [
    {"rid": rid, "asset": _asset_of(arc.url), "binext": ".exe" if rid.startswith("win") else ""}
    for rid, arc in _JELLYFIN.items()
]

# sha256 of each release ARCHIVE (not the extracted binary), keyed by Listenarr RID, taken straight
# from the shared harness pins. Pinning the archive verifies each download BEFORE extraction. The
# --verify mode re-fetches the live archives and re-checks them against these pins to catch upstream
# drift — the same pin-and-verify discipline the provisioner and corpus use.
PINS: dict[str, str] = {rid: arc.sha256 for rid, arc in _JELLYFIN.items()}


def _sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def record_artifact(
    binary: pathlib.Path, rid: str, asset: str, archive_sha256: str = ""
) -> dict[str, object]:
    """Describe one packaged binary for the manifest: where it came from, its hashes and size.

    ``archive_sha256`` is the verified pin of the source archive (checked before extraction);
    ``sha256`` is the hash of the extracted binary itself. Recording both lets a consumer re-verify
    the provenance chain end to end.
    """
    return {
        "rid": rid,
        "asset": asset,
        "file": binary.name,
        "archive_sha256": archive_sha256,
        "sha256": _sha256(binary),
        "bytes": binary.stat().st_size,
    }


# A provider extracts a verified ``program`` (ffprobe/ffmpeg) for a (source, rid) into ``cache_dir``
# and returns its path. Defaults to ffmpeg_harness.provision; tests inject an offline stand-in.
Provider = Callable[[str, str, str, pathlib.Path], pathlib.Path]


def package(
    outdir: pathlib.Path,
    program: str = "ffprobe",
    targets: list[dict[str, str]] = TARGETS,
    provider: Provider | None = None,
) -> dict[str, object]:
    """Lay out the per-RID ``program`` artifacts under ``outdir``; return the manifest.

    Extraction runs through ffmpeg_harness.provision, which verifies each archive against its pinned
    sha256 BEFORE extraction — a rolled build raises ChecksumError and no artifact lands.
    """
    do_provision: Provider = provider or ffmpeg_harness.provision

    artifacts: list[dict[str, object]] = []
    with tempfile.TemporaryDirectory() as cache_str:
        cache = pathlib.Path(cache_str)
        for t in targets:
            rid = t["rid"]
            binfile = f"{program}{t['binext']}"
            got = do_provision(program, _SOURCE, rid, cache)
            dest = outdir / rid / binfile
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(got, dest)
            dest.chmod(0o755)  # harmless on Windows
            artifacts.append(record_artifact(dest, rid, t["asset"], PINS.get(rid, "")))

    manifest: dict[str, object] = {
        "source": "jellyfin/jellyfin-ffmpeg",
        "version": DEFAULT_VERSION,
        "program": program,
        "artifacts": artifacts,
    }
    # With no targets no per-RID dir is created, so ensure outdir exists before the manifest write.
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def bundle_zips(outdir: pathlib.Path, manifest: dict[str, object]) -> list[pathlib.Path]:
    """Emit one ``<program>-<rid>.zip`` per RID under ``outdir`` (binary + manifest.json).

    This is the shape a release ships: one platform-correct, pinned binary per RID, packaged with
    the manifest (archive + binary sha256) so a consumer can verify it offline. Listenarr's own
    per-platform release build would drop the matching zip's ffprobe into each platform bundle, so
    a native (non-Docker) install ships a working ffprobe and never runs the first-boot download.
    """
    program = str(manifest["program"])
    artifacts = manifest["artifacts"]
    assert isinstance(artifacts, list)
    manifest_bytes = (outdir / "manifest.json").read_bytes()
    zips: list[pathlib.Path] = []
    for a in artifacts:
        rid, fname = str(a["rid"]), str(a["file"])
        binary = outdir / rid / fname
        zpath = outdir / f"{program}-{rid}.zip"
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(binary, fname)
            zf.writestr("manifest.json", manifest_bytes)
        zips.append(zpath)
    return zips


def verify_pins(
    version: str = DEFAULT_VERSION,
    base: str = DEFAULT_BASE,
    targets: list[dict[str, str]] = TARGETS,
    pins: dict[str, str] = PINS,
) -> list[tuple[str, bool, str]]:
    """Re-download each live release archive and check it still matches its pinned sha256.

    The drift check: a changed hash means upstream re-cut the release (or the download is corrupt),
    so the pin is stale and must be re-recorded deliberately rather than silently trusted. Returns
    one ``(rid, matches, actual_sha256)`` per target.
    """
    base_url = base.format(version=version)
    results: list[tuple[str, bool, str]] = []
    with tempfile.TemporaryDirectory() as td:
        for t in targets:
            rid = t["rid"]
            expected = pins.get(rid, "")
            archive = pathlib.Path(td) / t["asset"]
            # immutable release asset
            urllib.request.urlretrieve(f"{base_url}{t['asset']}", archive)
            actual = _sha256(archive)
            archive.unlink(missing_ok=True)
            results.append((rid, actual == expected, actual))
    return results


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=pathlib.Path,
                    help="directory to write the per-RID artifacts and manifest.json into")
    ap.add_argument("--program", choices=("ffprobe", "ffmpeg"), default="ffprobe",
                    help="binary to package (default ffprobe — Listenarr's need)")
    ap.add_argument("--version", default=DEFAULT_VERSION,
                    help=f"jellyfin release version for --verify (default {DEFAULT_VERSION})")
    ap.add_argument("--verify", action="store_true",
                    help="re-fetch the live release archives and check them against PINS; "
                         "prints OK/DRIFT per RID and exits non-zero on any drift")
    ap.add_argument("--zip", action="store_true",
                    help="also emit per-platform <program>-<rid>.zip bundles (binary + manifest)")
    args = ap.parse_args()

    if args.verify:
        drifted = []
        for rid, ok, actual in verify_pins(version=args.version):
            print(f"{rid:<12} {'OK' if ok else 'DRIFT':<5}  {actual}")
            if not ok:
                drifted.append(rid)
        if drifted:
            print(f"\nPIN DRIFT: {', '.join(drifted)} changed upstream; re-verify and re-pin.")
        return 1 if drifted else 0

    if args.out is None:
        ap.error("--out is required unless --verify is given")
    manifest = package(args.out, program=args.program)
    artifacts = manifest["artifacts"]
    assert isinstance(artifacts, list)
    total = sum(int(a["bytes"]) for a in artifacts)
    for a in artifacts:
        print(f"  {a['rid']:<12} {a['file']:<12} {int(a['bytes']):>12,} B  {a['sha256']}")
    print(f"packaged {len(artifacts)} {args.program} artifacts, "
          f"{total:,} B total -> {args.out}/manifest.json")
    if args.zip:
        for z in bundle_zips(args.out, manifest):
            print(f"  bundled {z.name} ({z.stat().st_size:,} B)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
