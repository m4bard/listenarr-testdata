#!/usr/bin/env python3
"""Install a pinned ffmpeg-family binary from a listenarr-testdata release, verified against GitHub.

Detect the host RID, look up the release asset's sha256 from GitHub's public Releases API (the
authoritative ``digest`` field, fetched out-of-band from the download), fetch the matching
``<program>-<rid>.zip``, and refuse it unless its bytes match that digest. Then extract the binary
and re-check it against the ``manifest.json`` shipped inside the zip (defense in depth) before
dropping it into place. GitHub release asset URLs are stable and the digest endpoint is public
read-only, so this is a reliable, self-contained provisioning path.

Trust boundary: GitHub's digest confirms the download matches what GitHub stores (catching transit
or mirror corruption), not provenance — a release replaced by someone with push access would carry a
matching digest. Pin ``--tag`` to a specific release; stronger provenance needs signed attestations.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
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
    """A downloaded asset or binary did not match its expected sha256."""


def release_asset_url(repo: str, tag: str, asset: str) -> str:
    """Stable GitHub release asset URL. ``tag='latest'`` resolves to the newest release."""
    if tag == "latest":
        return f"https://github.com/{repo}/releases/latest/download/{asset}"
    return f"https://github.com/{repo}/releases/download/{tag}/{asset}"


def api_asset_digest(repo: str, tag: str, asset: str) -> str:
    """The sha256 GitHub records for a release asset, via the public Releases API.

    This is the out-of-band integrity anchor: it comes from ``api.github.com`` in a separate
    request, not from the downloaded bytes. Returns the bare hex (the ``sha256:`` prefix stripped).
    """
    ref = "latest" if tag == "latest" else f"tags/{tag}"
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/releases/{ref}",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "listenarr-testdata"},
    )
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req) as resp:  # public read-only endpoint
        release = json.loads(resp.read())
    for a in release.get("assets", []):
        if a.get("name") == asset:
            algo, _, hexpart = (a.get("digest") or "").partition(":")
            if algo != "sha256" or not hexpart:
                raise VerificationError(f"no usable sha256 digest for {asset} in {repo}@{tag}")
            return hexpart
    raise VerificationError(f"no asset {asset!r} in {repo}@{tag}")


def _sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _manifest_sha(manifest: dict[str, object], rid: str) -> str:
    artifacts = manifest.get("artifacts", [])
    assert isinstance(artifacts, list)
    for a in artifacts:
        assert isinstance(a, dict)
        if a.get("rid") == rid:
            return str(a["sha256"])
    raise VerificationError(f"manifest has no artifact for rid {rid!r}")


# Injectable seams so the two network paths (asset download, digest lookup) are testable offline.
Fetcher = Callable[[str, pathlib.Path], None]
DigestLookup = Callable[[str, str, str], str]


def _default_fetch(url: str, dest: pathlib.Path) -> None:
    urllib.request.urlretrieve(url, dest)  # https release asset, verified below


def install(
    program: str,
    dest: pathlib.Path,
    rid: str | None = None,
    tag: str = "latest",
    repo: str = DEFAULT_REPO,
    fetcher: Fetcher | None = None,
    digest_lookup: DigestLookup | None = None,
) -> pathlib.Path:
    """Fetch (program, RID)'s release zip, verify it against GitHub's digest, place the binary."""
    rid = rid or host_rid()
    binfile = f"{program}{'.exe' if rid.startswith('win') else ''}"
    asset = f"{program}-{rid}.zip"
    do_fetch: Fetcher = fetcher or _default_fetch
    resolve: DigestLookup = digest_lookup or api_asset_digest

    expected_zip = resolve(repo, tag, asset)  # authoritative, out-of-band from the API

    with tempfile.TemporaryDirectory() as td:
        zpath = pathlib.Path(td) / asset
        do_fetch(release_asset_url(repo, tag, asset), zpath)
        actual_zip = _sha256_file(zpath)
        if actual_zip != expected_zip:
            raise VerificationError(
                f"{asset} does not match GitHub's recorded sha256\n"
                f"  expected {expected_zip}\n  actual   {actual_zip}")
        with zipfile.ZipFile(zpath) as zf:
            manifest: dict[str, object] = json.loads(zf.read("manifest.json"))
            expected_bin = _manifest_sha(manifest, rid)
            data = zf.read(binfile)

    actual_bin = hashlib.sha256(data).hexdigest()
    if actual_bin != expected_bin:  # defense in depth: the binary vs its own manifest
        raise VerificationError(
            f"{binfile} does not match its manifest sha256 in {asset}\n"
            f"  expected {expected_bin}\n  actual   {actual_bin}")

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
