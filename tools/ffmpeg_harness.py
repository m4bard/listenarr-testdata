#!/usr/bin/env python3
"""Provision a pinned, sha256-verified ffmpeg-family binary — the reference implementation.

This is the harness listenarr-testdata runs, and the one the Listenarr ffprobe proposal points at:
the intent is that if the maintainer OKs it, this same logic is ported to C# in Listenarr. It is
parameterized two ways:

  * SOURCE — ``"johnvansickle"`` (Listenarr's current Linux source) or ``"jellyfin"``. The choice is
    real: johnvansickle's static builds exist only for Linux, which is exactly why macOS and Windows
    run other, personal sources today — and why macOS currently ships **no ffprobe at all**.
    jellyfin-ffmpeg is the one org-maintained source that covers *every* platform Listenarr ships.
    So "keep johnvansickle, it's what you already use" is the status-quo (Linux-only) path;
    "jellyfin" is the full-cross-platform path. The harness supports both and forces neither.

  * BINARY — ``"ffmpeg"`` (to CREATE audio; what listenarr-testdata needs to synthesize fixtures)
    or ``"ffprobe"`` (to READ metadata; the only ffmpeg-family binary Listenarr runs). Both sources
    ship both binaries inside a *single* archive, so one pinned, checksum-verified download serves
    either — you just extract a different member.

This is deployment/provisioning tooling, not app code: listenarr-testdata runs it to pull ffmpeg,
and Listenarr can run the *same script* from its Docker entrypoint to drop a pinned, verified
ffprobe into ``<config>/ffmpeg/ffprobe`` before the app starts (the app then finds it and skips its
own unpinned download). No C# port required.

Safety contract (the whole point of pinning): the archive is verified against a recorded sha256
**before** extraction — a mismatch RAISES and never extracts an unverified binary. ``verify_source``
re-downloads live and re-checks the pins, so an upstream re-cut goes red and gets re-pinned
deliberately rather than silently trusted.
"""
from __future__ import annotations

import hashlib
import pathlib
import platform
import tarfile
import tempfile
import urllib.request
import zipfile
from dataclasses import dataclass

ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_CACHE = ROOT / "build" / "ffmpeg-cache"

_JF_BASE = (
    "https://github.com/jellyfin/jellyfin-ffmpeg/releases/download/"
    "v7.1.4-3/jellyfin-ffmpeg_7.1.4-3_portable_{asset}"
)


@dataclass(frozen=True)
class Archive:
    """One pinned, verifiable release archive. It contains both ffmpeg and ffprobe.

    ``member`` is a template with a ``{binary}`` placeholder (filled with e.g. ``ffmpeg`` or
    ``ffprobe.exe``): johnvansickle nests binaries under a versioned directory, jellyfin keeps them
    flat at the archive root. ``sha256`` is the whole-archive hash, verified before extraction.
    """

    url: str
    sha256: str
    member: str


# The source matrix. johnvansickle is Linux-only — that coverage gap is the crux of the macOS bug
# and the reason jellyfin (every platform) is the full-compat answer. Both ship ffmpeg AND ffprobe,
# so the same pin serves either binary.
#
# johnvansickle: ffmpeg-7.0.2 static. jellyfin: v7.1.4-3 portable. Archive sha256 captured
# 2026-07-22; `verify_source` re-checks them (johnvansickle rolls its "release" build, so its
# pins drift by design).
SOURCES: dict[str, dict[str, Archive]] = {
    "johnvansickle": {
        "linux-x64": Archive(
            "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz",
            "abda8d77ce8309141f83ab8edf0596834087c52467f6badf376a6a2a4c87cf67",
            "ffmpeg-7.0.2-amd64-static/{binary}",
        ),
        "linux-arm64": Archive(
            "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-arm64-static.tar.xz",
            "f4149bb2b0784e30e99bdda85471c9b5930d3402014e934a5098b41d0f7201b1",
            "ffmpeg-7.0.2-arm64-static/{binary}",
        ),
    },
    "jellyfin": {
        "linux-x64": Archive(_JF_BASE.format(asset="linux64-gpl.tar.xz"),
                             "cab9ff40a47e4232d231e4eb7e4e85fabfeec56c6905266bc94291fc0881f83f",
                             "{binary}"),
        "linux-arm64": Archive(_JF_BASE.format(asset="linuxarm64-gpl.tar.xz"),
                               "77e4b5d044ab73e1f26c9aadaa5d6014d1782500bf2c29afb3ab81f5bea98b1f",
                               "{binary}"),
        "osx-x64": Archive(_JF_BASE.format(asset="mac64-gpl.tar.xz"),
                           "943f78e94d2760d3925fc0d9cc15f8329b11dbcdae7b0fd0d225b64e5a1aae29",
                           "{binary}"),
        "win-x64": Archive(_JF_BASE.format(asset="win64-clang-gpl.zip"),
                           "113adeb702683c38be40a65d859f8ef7ffb07bae9df16dfb6c3df5ac3d95ef3c",
                           "{binary}"),
    },
}

DEFAULT_SOURCE = "jellyfin"


class ChecksumError(RuntimeError):
    """A downloaded archive did not match its pinned sha256 — raised, never swallowed."""


class UnsupportedTarget(RuntimeError):
    """The chosen source has no build for this RID (e.g. johnvansickle on macOS/Windows).

    This is not a harness failure — it is the coverage gap itself, surfaced honestly: it's precisely
    why a Linux-only source can't be Listenarr's single cross-platform answer.
    """


def host_rid() -> str:
    """Map the current machine to a Listenarr runtime identifier."""
    system, machine = platform.system().lower(), platform.machine().lower()
    if system == "linux":
        if machine in ("x86_64", "amd64"):
            return "linux-x64"
        if machine in ("aarch64", "arm64"):
            return "linux-arm64"
    elif system == "darwin":
        return "osx-x64"  # Listenarr ships osx-x64 only; on Apple Silicon it runs under Rosetta
    elif system == "windows":
        return "win-x64"
    raise UnsupportedTarget(f"no RID mapping for {system}/{machine}")


def _sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_and_extract(archive: pathlib.Path, arc: Archive, binary: str,
                       dest: pathlib.Path) -> pathlib.Path:
    """Verify ``archive`` against its pin, then extract ``binary`` to ``dest``.

    Split from the download so the safety contract — a hash mismatch RAISES, never extracts — is
    testable offline. Handles both ``.tar.xz`` (johnvansickle, jellyfin non-Windows) and ``.zip``
    (jellyfin Windows).
    """
    actual = _sha256(archive)
    if actual != arc.sha256:
        raise ChecksumError(
            f"sha256 mismatch for {arc.url}\n  expected {arc.sha256}\n  actual   {actual}\n"
            "The pinned build changed or the download is corrupt; re-verify and re-pin."
        )
    member = arc.member.format(binary=binary)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as zf:
            entry = next((m for m in zf.namelist()
                          if pathlib.PurePosixPath(m).name == member
                          or m == member), None)
            if entry is None:
                raise UnsupportedTarget(f"{member} not found in {arc.url}")
            dest.write_bytes(zf.read(entry))
    else:
        with tarfile.open(archive, "r:xz") as tar:
            try:
                info = tar.getmember(member)
            except KeyError:
                raise UnsupportedTarget(f"{member} not found in {arc.url}") from None
            extracted = tar.extractfile(info)
            if extracted is None:
                raise UnsupportedTarget(f"{member} is not a regular file in {arc.url}")
            with extracted:
                dest.write_bytes(extracted.read())
    dest.chmod(0o755)  # harmless on Windows
    return dest


def provision(binary: str, source: str = DEFAULT_SOURCE, rid: str | None = None,
              cache_dir: pathlib.Path | None = None) -> pathlib.Path:
    """Return a cached, verified ``binary`` (``ffmpeg``/``ffprobe``) for ``rid``, downloading once.

    The archive is fetched to a temp file, verified against its pin, and the requested binary
    extracted into the cache. A cached binary is reused; bust the cache by deleting ``cache_dir``.
    """
    rid = rid or host_rid()
    try:
        arc = SOURCES[source][rid]
    except KeyError:
        available = ", ".join(sorted(SOURCES.get(source, {}))) or "(unknown source)"
        raise UnsupportedTarget(
            f"source {source!r} has no build for {rid!r} (has: {available})"
        ) from None

    binext = ".exe" if rid.startswith("win") else ""
    binfile = f"{binary}{binext}"
    cache = cache_dir or DEFAULT_CACHE
    dest = cache / source / rid / binfile
    if dest.exists() and dest.stat().st_size > 0:
        return dest

    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        archive = pathlib.Path(td) / "download"
        urllib.request.urlretrieve(arc.url, archive)  # https release asset, hash-checked below
        return verify_and_extract(archive, arc, binfile, dest)


def verify_source(source: str = DEFAULT_SOURCE) -> list[tuple[str, bool, str]]:
    """Re-download each pinned archive for ``source`` and check it still matches its sha256.

    Returns one ``(rid, matches, actual_sha256)`` per RID. A mismatch means the upstream build was
    re-cut (johnvansickle rolls its "release"; jellyfin assets are immutable per tag) — re-pin
    deliberately rather than trust the new bytes silently.
    """
    results: list[tuple[str, bool, str]] = []
    with tempfile.TemporaryDirectory() as td:
        for rid, arc in sorted(SOURCES[source].items()):
            archive = pathlib.Path(td) / f"{rid}"
            urllib.request.urlretrieve(arc.url, archive)
            actual = _sha256(archive)
            archive.unlink(missing_ok=True)
            results.append((rid, actual == arc.sha256, actual))
    return results


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--binary", choices=("ffmpeg", "ffprobe"), default="ffprobe")
    ap.add_argument("--source", choices=sorted(SOURCES), default=DEFAULT_SOURCE)
    ap.add_argument("--rid", help="override the target RID (default: host)")
    ap.add_argument("--verify", action="store_true",
                    help="re-download each --source pin, check sha256; non-zero on drift")
    args = ap.parse_args()

    if args.verify:
        drifted = []
        for rid, ok, actual in verify_source(args.source):
            print(f"{rid}: {'OK' if ok else 'DRIFT'}  {actual}")
            if not ok:
                drifted.append(rid)
        if drifted:
            print(f"\nPIN DRIFT in {args.source}: {', '.join(drifted)} — re-verify and re-pin.")
        return 1 if drifted else 0

    placed = provision(args.binary, args.source, args.rid)
    print(f"provisioned {args.source} {args.binary} -> {placed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
