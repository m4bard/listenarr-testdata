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
from collections.abc import Callable

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from fetch_ffprobe import FFPROBE_NAMES, fetch

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


def _sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def record_artifact(binary: pathlib.Path, rid: str, asset: str) -> dict[str, object]:
    """Describe one packaged binary for the manifest: where it came from, its hash and size."""
    return {
        "rid": rid,
        "asset": asset,
        "file": binary.name,
        "sha256": _sha256(binary),
        "bytes": binary.stat().st_size,
    }


# A fetcher downloads the asset and extracts the wanted binary to ``dest``, returning its path.
# Defaults to fetch_ffprobe.fetch; tests inject an offline stand-in.
Fetcher = Callable[[str, pathlib.Path], pathlib.Path]


def package(
    outdir: pathlib.Path,
    version: str = DEFAULT_VERSION,
    base: str = DEFAULT_BASE,
    targets: list[dict[str, str]] = TARGETS,
    fetcher: Fetcher | None = None,
) -> dict[str, object]:
    """Fetch and lay out the per-RID ffprobe artifacts under ``outdir``; return the manifest."""
    base_url = base.format(version=version)
    do_fetch: Fetcher = fetcher or (lambda url, dest: fetch(url, dest))

    artifacts: list[dict[str, object]] = []
    for t in targets:
        dest = outdir / t["rid"] / f"ffprobe{t['binext']}"
        do_fetch(f"{base_url}{t['asset']}", dest)
        artifacts.append(record_artifact(dest, t["rid"], t["asset"]))

    manifest: dict[str, object] = {
        "source": "jellyfin/jellyfin-ffmpeg",
        "version": version,
        "binaries": sorted(WANTED_BINARIES),
        "artifacts": artifacts,
    }
    (outdir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=pathlib.Path, required=True,
                    help="directory to write the per-RID artifacts and manifest.json into")
    ap.add_argument("--version", default=DEFAULT_VERSION,
                    help=f"jellyfin-ffmpeg release version (default {DEFAULT_VERSION})")
    args = ap.parse_args()
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
