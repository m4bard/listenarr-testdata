#!/usr/bin/env python3
"""Package ffprobe as a discrete, per-platform artifact set with a sha256 manifest.

Listenarr executes exactly one ffmpeg-family binary — ffprobe — to read audio metadata
(``ffprobe -v quiet -print_format json -show_format -show_streams``). It never spawns an
ffmpeg binary: the only file mutation it performs is ASIN tag-writing, and that goes through
the managed TagLibSharp library, not ffmpeg. So the install only needs ffprobe, yet today it
downloads a whole ffmpeg archive (76-122 MB) at first boot and digs the one binary out of it.

This packages just ffprobe for each RID Listenarr ships (linux-x64, linux-arm64, win-x64,
osx-x64), from a single maintained source (jellyfin-ffmpeg), and emits a manifest recording the
sha256 and size of every artifact. The result is a small, pinned, verifiable set the build can
bundle instead of fetching an unpinned whole-ffmpeg archive per platform at runtime.

Adjustable for the future: the *only* thing that would ever need the full ffmpeg binary is
re-encoding/transcode, which Listenarr does not do. If that changes, extend ``WANTED_BINARIES``
below to also pull ``ffmpeg`` and the loop packages both — the source archive already contains it.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sys
import tempfile
import urllib.request
from collections.abc import Callable

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from fetch_ffprobe import FFPROBE_NAMES, ChecksumError, fetch

__all__ = ["PINS", "TARGETS", "ChecksumError", "package", "record_artifact", "verify_pins"]

# The binaries to pull out of each source archive. ffprobe only today (Listenarr reads metadata
# and nothing else). To also bundle ffmpeg for future re-encode support, add its names here.
WANTED_BINARIES = FFPROBE_NAMES

# jellyfin-ffmpeg: the one org-maintained, GitHub-hosted, versioned, sha256-checksummed source
# that covers every RID Listenarr targets. GPL-only upstream, which is license-clean for an
# AGPL-3.0 host, so no LGPL variant is needed. Mapping is Listenarr RID -> release asset.
DEFAULT_VERSION = "7.1.4-3"
DEFAULT_BASE = (
    "https://github.com/jellyfin/jellyfin-ffmpeg/releases/download/"
    "v{version}/jellyfin-ffmpeg_{version}_"
)

# Listenarr's shipped RIDs (from its csproj). It has no osx-arm64 RID — Apple Silicon runs the
# osx-x64 build under Rosetta — but macarm64 is available upstream if that RID is ever added.
TARGETS = [
    {"rid": "linux-x64", "asset": "portable_linux64-gpl.tar.xz", "binext": ""},
    {"rid": "linux-arm64", "asset": "portable_linuxarm64-gpl.tar.xz", "binext": ""},
    {"rid": "osx-x64", "asset": "portable_mac64-gpl.tar.xz", "binext": ""},
    {"rid": "win-x64", "asset": "portable_win64-clang-gpl.zip", "binext": ".exe"},
]

# sha256 of each release ARCHIVE (not the extracted binary), keyed by Listenarr RID. Pinning the
# archive means the download is verified BEFORE extraction — fetch_ffprobe.fetch checks this hash
# and raises ChecksumError without ever unpacking a tampered or rolled build. This release ships no
# per-asset checksum sidecar (no .sha256sum assets exist), so these were captured by downloading
# each archive once and hashing it. The --verify mode re-fetches the live archives and re-checks
# them against these pins to catch upstream drift — the same pin-and-verify discipline the
# provisioner and corpus use.
# jellyfin/jellyfin-ffmpeg v7.1.4-3 archive sha256, captured 2026-07-22.
PINS: dict[str, str] = {
    "linux-x64": "cab9ff40a47e4232d231e4eb7e4e85fabfeec56c6905266bc94291fc0881f83f",
    "linux-arm64": "77e4b5d044ab73e1f26c9aadaa5d6014d1782500bf2c29afb3ab81f5bea98b1f",
    "osx-x64": "943f78e94d2760d3925fc0d9cc15f8329b11dbcdae7b0fd0d225b64e5a1aae29",
    "win-x64": "113adeb702683c38be40a65d859f8ef7ffb07bae9df16dfb6c3df5ac3d95ef3c",
}


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

    ``archive_sha256`` is the verified pin of the source archive (the download that fetch checked
    before extraction); ``sha256`` is the hash of the extracted ffprobe binary itself. Recording
    both lets a consumer re-verify the provenance chain end to end.
    """
    return {
        "rid": rid,
        "asset": asset,
        "file": binary.name,
        "archive_sha256": archive_sha256,
        "sha256": _sha256(binary),
        "bytes": binary.stat().st_size,
    }


# A fetcher downloads the asset, verifies it against the pinned archive sha256 (third arg; ``None``
# skips verification) and extracts the wanted binary to ``dest``, returning its path. Defaults to
# fetch_ffprobe.fetch; tests inject an offline stand-in.
Fetcher = Callable[[str, pathlib.Path, str | None], pathlib.Path]


def package(
    outdir: pathlib.Path,
    version: str = DEFAULT_VERSION,
    base: str = DEFAULT_BASE,
    targets: list[dict[str, str]] = TARGETS,
    fetcher: Fetcher | None = None,
    pins: dict[str, str] = PINS,
) -> dict[str, object]:
    """Fetch and lay out the per-RID ffprobe artifacts under ``outdir``; return the manifest.

    Each archive is verified against its pinned sha256 BEFORE extraction, so a rolled or tampered
    build raises ChecksumError and no artifact is written.
    """
    base_url = base.format(version=version)
    do_fetch: Fetcher = fetcher or fetch

    artifacts: list[dict[str, object]] = []
    for t in targets:
        rid = t["rid"]
        pin = pins.get(rid)
        dest = outdir / rid / f"ffprobe{t['binext']}"
        do_fetch(f"{base_url}{t['asset']}", dest, pin)
        artifacts.append(record_artifact(dest, rid, t["asset"], pin or ""))

    manifest: dict[str, object] = {
        "source": "jellyfin/jellyfin-ffmpeg",
        "version": version,
        "binaries": sorted(WANTED_BINARIES),
        "artifacts": artifacts,
    }
    (outdir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


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
    ap.add_argument("--version", default=DEFAULT_VERSION,
                    help=f"jellyfin-ffmpeg release version (default {DEFAULT_VERSION})")
    ap.add_argument("--verify", action="store_true",
                    help="re-fetch the live release archives and check them against PINS; "
                         "prints OK/DRIFT per RID and exits non-zero on any drift")
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
    manifest = package(args.out, version=args.version)
    artifacts = manifest["artifacts"]
    assert isinstance(artifacts, list)
    total = sum(int(a["bytes"]) for a in artifacts)
    for a in artifacts:
        print(f"  {a['rid']:<12} {a['file']:<12} {int(a['bytes']):>12,} B  {a['sha256']}")
    print(f"packaged {len(artifacts)} artifacts, {total:,} B total -> {args.out}/manifest.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
