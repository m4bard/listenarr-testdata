#!/usr/bin/env python3
"""Install a pinned ffmpeg-family binary from a listenarr-testdata release.

Detect the host RID, download the matching ``<program>-<rid>.zip`` from a GitHub release (a pinned
tag or the latest), verify the extracted binary against the ``manifest.json`` sha256 shipped inside
the zip, and drop it into place. GitHub release asset URLs are stable, so this is a reliable,
self-contained provisioning path that consumes exactly the pinned artifact a release published — no
live upstream fetch, no unpinned rolling build.

Trust model: the binary is checked against the manifest packaged in the same zip, which catches
transit corruption. The release itself is built by package_ffbinary from archives verified against
their pinned sha256 before extraction, so the published artifact is trustworthy at its source; pass
``--tag`` to pin a specific release rather than tracking ``latest``.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sys
import tempfile
import urllib.request
import zipfile
from collections.abc import Callable

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from ffmpeg_harness import host_rid

DEFAULT_REPO = "m4bard/listenarr-testdata"


class VerificationError(RuntimeError):
    """The downloaded binary did not match the sha256 its manifest records."""


def release_asset_url(repo: str, tag: str, asset: str) -> str:
    """Stable GitHub release asset URL. ``tag='latest'`` resolves to the newest release."""
    if tag == "latest":
        return f"https://github.com/{repo}/releases/latest/download/{asset}"
    return f"https://github.com/{repo}/releases/download/{tag}/{asset}"


def _expected_sha(manifest: dict[str, object], rid: str) -> str:
    artifacts = manifest.get("artifacts", [])
    assert isinstance(artifacts, list)
    for a in artifacts:
        assert isinstance(a, dict)
        if a.get("rid") == rid:
            return str(a["sha256"])
    raise VerificationError(f"manifest has no artifact for rid {rid!r}")


# A fetcher writes the release zip at ``url`` to ``dest``; defaults to urlretrieve.
Fetcher = Callable[[str, pathlib.Path], None]


def _default_fetch(url: str, dest: pathlib.Path) -> None:
    urllib.request.urlretrieve(url, dest)  # https release asset, verified after extraction below


def install(
    program: str,
    dest: pathlib.Path,
    rid: str | None = None,
    tag: str = "latest",
    repo: str = DEFAULT_REPO,
    fetcher: Fetcher | None = None,
) -> pathlib.Path:
    """Download the release bundle for (program, RID), verify it, and place it at ``dest``."""
    rid = rid or host_rid()
    binfile = f"{program}{'.exe' if rid.startswith('win') else ''}"
    asset = f"{program}-{rid}.zip"
    do_fetch: Fetcher = fetcher or _default_fetch

    with tempfile.TemporaryDirectory() as td:
        zpath = pathlib.Path(td) / asset
        do_fetch(release_asset_url(repo, tag, asset), zpath)
        with zipfile.ZipFile(zpath) as zf:
            manifest: dict[str, object] = json.loads(zf.read("manifest.json"))
            expected = _expected_sha(manifest, rid)
            data = zf.read(binfile)

    actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        raise VerificationError(
            f"{binfile} from {asset} does not match its manifest sha256\n"
            f"  expected {expected}\n  actual   {actual}")

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    dest.chmod(0o755)  # harmless on Windows
    return dest


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--program", choices=("ffprobe", "ffmpeg"), default="ffprobe")
    ap.add_argument("--dest", type=pathlib.Path, required=True, help="write the binary here")
    ap.add_argument("--rid", help="override the target RID (default: host)")
    ap.add_argument("--tag", default="latest", help="release tag to pull, or 'latest' (default)")
    ap.add_argument("--repo", default=DEFAULT_REPO, help=f"owner/repo (default {DEFAULT_REPO})")
    args = ap.parse_args()
    placed = install(args.program, args.dest, rid=args.rid, tag=args.tag, repo=args.repo)
    rid = args.rid or host_rid()
    print(f"installed {args.program} ({rid}) from {args.repo}@{args.tag} -> {placed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
