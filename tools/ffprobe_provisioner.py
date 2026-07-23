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
(``File.Exists``) and skip the download entirely: no race, and the same binary every run — which is
what the benchmark needs to be reproducible.

This is now a thin wrapper over the shared provisioning harness (``ffmpeg_harness``): the pins,
the sha256-before-extraction safety contract and the drift check all live there, so ffprobe and the
fixture-building ffmpeg come from one source of truth. The source is selectable — ``jellyfin`` (the
one org-maintained source covering every platform Listenarr ships) by default, or ``johnvansickle``
(Listenarr's current Linux-only source) via
``--source``. The ``ffmpeg-drift`` workflow re-verifies the pins the same way.
"""
from __future__ import annotations

import pathlib
import shutil
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import ffmpeg_harness

# Re-exported so callers importing from this module get the same names as the harness.
ChecksumError = ffmpeg_harness.ChecksumError
UnsupportedTarget = ffmpeg_harness.UnsupportedTarget
DEFAULT_SOURCE = ffmpeg_harness.DEFAULT_SOURCE


def provision_config(
    config_dir: pathlib.Path,
    source: str = DEFAULT_SOURCE,
    rid: str | None = None,
    cache_dir: pathlib.Path | None = None,
) -> pathlib.Path:
    """Drop a verified ffprobe at ``<config_dir>/ffmpeg/ffprobe`` so Listenarr skips downloading.

    Delegates to ``ffmpeg_harness.provision`` (pinned archive, sha256-verified BEFORE extraction,
    cached) and copies the result into the config layout. This is the exact placement Listenarr's
    Docker entrypoint would perform before the app starts.
    """
    ffprobe = ffmpeg_harness.provision("ffprobe", source=source, rid=rid, cache_dir=cache_dir)
    dest = pathlib.Path(config_dir) / "ffmpeg" / "ffprobe"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ffprobe, dest)
    dest.chmod(0o755)
    return dest


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config-dir", type=pathlib.Path, help="provision ffprobe into this dir")
    ap.add_argument("--source", choices=sorted(ffmpeg_harness.SOURCES), default=DEFAULT_SOURCE,
                    help=f"provisioning source (default {DEFAULT_SOURCE}); johnvansickle is "
                         "Listenarr's current Linux-only source, jellyfin covers every platform")
    ap.add_argument("--rid", help="override the target RID (default: host)")
    ap.add_argument("--verify-pins", action="store_true",
                    help="re-download every pin for --source and check its sha256; "
                         "non-zero exit on drift")
    args = ap.parse_args()

    if args.verify_pins:
        drifted = []
        for rid, ok, actual in ffmpeg_harness.verify_source(args.source):
            print(f"{rid:<12} {'OK' if ok else 'DRIFT':<5}  {actual}")
            if not ok:
                drifted.append(rid)
        if drifted:
            print(f"\nPIN DRIFT in {args.source}: {', '.join(drifted)} — re-verify and re-pin.")
        return 1 if drifted else 0

    if args.config_dir:
        placed = provision_config(args.config_dir, args.source, args.rid)
        print(f"provisioned ffprobe -> {placed}")
    else:
        placed = ffmpeg_harness.provision("ffprobe", source=args.source, rid=args.rid)
        print(f"cached ffprobe -> {placed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
