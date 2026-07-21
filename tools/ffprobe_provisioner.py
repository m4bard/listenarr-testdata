#!/usr/bin/env python3
"""Provision a pinned, checksum-verified ffprobe into a Listenarr config dir before container start.

Why this exists. Listenarr downloads ffprobe on first boot into ``<config>/ffmpeg/ffprobe`` — an
*unpinned, unverified* rolling static build (its default source returns a rolling URL and a null
checksum). Two consequences bite a test harness:

  * a race — ``manual-import`` hard-fails with "Failed to extract metadata from file" if it runs
    during the first-boot window while ffprobe is still downloading (a scan tolerates it; import
    does not), and
  * non-determinism — the exact ffprobe binary varies run to run.

Dropping a known-good ffprobe at ``<config>/ffmpeg/ffprobe`` up front makes the app find it
(``File.Exists``) and skip the download entirely: no race, and the same binary every run.

The pin mirrors Listenarr's *current* source (johnvansickle static builds) so the harness matches
what real deployments run today, but fixed to a specific version + sha256 so it is reproducible.
The ``ffmpeg-drift`` GitHub workflow re-verifies the pin; johnvansickle rolls its "release" build,
so when the hash changes that job goes red and we re-pin deliberately — the same pin-and-verify
discipline the corpus uses for ASINs.
"""
from __future__ import annotations

import hashlib
import pathlib
import platform
import shutil
import tarfile
import urllib.request
from dataclasses import dataclass

ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_CACHE = ROOT / "build" / "ffprobe-cache"


@dataclass(frozen=True)
class Pin:
    """A fixed, verifiable ffprobe artifact for one architecture."""

    url: str
    sha256: str
    member: str  # path of the ffprobe binary inside the archive


# Pinned to ffmpeg-7.0.2 static (johnvansickle) — the same build Listenarr pulls today, so the
# harness exercises the deployment reality. Verified by download; the drift workflow re-checks it.
PINS: dict[str, Pin] = {
    "amd64": Pin(
        url="https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz",
        sha256="abda8d77ce8309141f83ab8edf0596834087c52467f6badf376a6a2a4c87cf67",
        member="ffmpeg-7.0.2-amd64-static/ffprobe",
    ),
    "arm64": Pin(
        url="https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-arm64-static.tar.xz",
        sha256="f4149bb2b0784e30e99bdda85471c9b5930d3402014e934a5098b41d0f7201b1",
        member="ffmpeg-7.0.2-arm64-static/ffprobe",
    ),
}


class ChecksumError(RuntimeError):
    """A downloaded artifact did not match its pinned sha256.

    Raised — never swallowed — so a rolled or tampered build fails loudly instead of silently
    provisioning an unverified binary. This is the whole point of pinning.
    """


def host_arch() -> str:
    """The pin key for the current machine. Raises on an architecture we do not pin."""
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        return "amd64"
    if machine in ("aarch64", "arm64"):
        return "arm64"
    raise ValueError(
        f"unsupported architecture {machine!r}; only amd64/arm64 are pinned "
        "(the archs the Listenarr Linux image ships for)"
    )


def _sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_and_extract(archive: pathlib.Path, pin: Pin, dest: pathlib.Path) -> pathlib.Path:
    """Verify ``archive`` against ``pin.sha256`` and extract its ffprobe to ``dest``.

    Split out from the download so the safety contract — a hash mismatch RAISES, never extracts —
    is unit-testable without the network.
    """
    actual = _sha256(archive)
    if actual != pin.sha256:
        raise ChecksumError(
            f"sha256 mismatch for {pin.url}\n  expected {pin.sha256}\n  actual   {actual}\n"
            "The pinned build changed or the download is corrupt; re-verify and re-pin."
        )
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:xz") as tar:
        member = tar.getmember(pin.member)
        with tar.extractfile(member) as src:  # type: ignore[union-attr]
            dest.write_bytes(src.read())
    dest.chmod(0o755)
    return dest


def ensure_ffprobe(arch: str | None = None, cache_dir: pathlib.Path | None = None) -> pathlib.Path:
    """Return a cached, verified ffprobe for ``arch`` (default: the host), downloading once."""
    arch = arch or host_arch()
    if arch not in PINS:
        raise ValueError(f"no pin for architecture {arch!r}")
    pin = PINS[arch]
    cache = cache_dir or DEFAULT_CACHE
    ffprobe = cache / arch / "ffprobe"
    if ffprobe.exists() and _sha256_of_binary_ok(ffprobe):
        return ffprobe

    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / f"{arch}.tar.xz"
    urllib.request.urlretrieve(pin.url, archive)
    try:
        return verify_and_extract(archive, pin, ffprobe)
    finally:
        archive.unlink(missing_ok=True)


def _sha256_of_binary_ok(ffprobe: pathlib.Path) -> bool:
    # The cached binary is the extracted ffprobe, not the archive, so we cannot re-check the archive
    # hash here; presence + executability is the cache-hit signal. A corrupt cache is cheap to bust
    # (delete build/ffprobe-cache). The archive hash is enforced at extract time.
    return ffprobe.stat().st_size > 0


def verify_pin(pin: Pin, work_dir: pathlib.Path) -> tuple[bool, str]:
    """Re-download a pin and report whether it still matches its recorded sha256.

    The drift check: johnvansickle rolls its "release" build, so a changed hash means the pin is
    stale and must be re-recorded deliberately (rather than silently trusting a new binary).
    Returns ``(matches, actual_sha256)``.
    """
    archive = work_dir / "verify.tar.xz"
    urllib.request.urlretrieve(pin.url, archive)
    try:
        actual = _sha256(archive)
        return actual == pin.sha256, actual
    finally:
        archive.unlink(missing_ok=True)


def provision_config(
    config_dir: pathlib.Path, arch: str | None = None, cache_dir: pathlib.Path | None = None
) -> pathlib.Path:
    """Drop a verified ffprobe at ``<config_dir>/ffmpeg/ffprobe`` so Listenarr skips downloading."""
    ffprobe = ensure_ffprobe(arch, cache_dir)
    dest = pathlib.Path(config_dir) / "ffmpeg" / "ffprobe"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ffprobe, dest)
    dest.chmod(0o755)
    return dest


if __name__ == "__main__":
    import argparse
    import tempfile

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config-dir", type=pathlib.Path, help="provision ffprobe into this dir")
    ap.add_argument("--arch", choices=sorted(PINS), help="override arch (default: host)")
    ap.add_argument("--verify-pins", action="store_true",
                    help="re-download every pin and check its sha256; non-zero exit on drift")
    args = ap.parse_args()

    if args.verify_pins:
        drifted = []
        with tempfile.TemporaryDirectory() as td:
            for arch, pin in sorted(PINS.items()):
                ok, actual = verify_pin(pin, pathlib.Path(td))
                print(f"{arch}: {'OK' if ok else 'DRIFTED'}  {pin.url}")
                print(f"    expected {pin.sha256}\n    actual   {actual}")
                if not ok:
                    drifted.append(arch)
        if drifted:
            print(f"\nPIN DRIFT: {', '.join(drifted)} rolled upstream; re-verify and re-pin.")
        raise SystemExit(1 if drifted else 0)

    if args.config_dir:
        placed = provision_config(args.config_dir, args.arch)
        print(f"provisioned ffprobe -> {placed}")
    else:
        print(f"cached ffprobe -> {ensure_ffprobe(args.arch)}")
