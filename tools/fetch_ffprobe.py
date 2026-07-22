#!/usr/bin/env python3
"""Download an ffprobe build and extract just the ffprobe binary — cross-platform (Linux/macOS/Win).

Used by the cross-platform equivalence CI so every runner fetches its platform's ffprobe with the
same command, regardless of shell. Handles `.tar.xz` (Linux/macOS builds) and `.zip` (Windows), and
finds the binary by basename (`ffprobe` or `ffprobe.exe`) wherever it sits in the archive. If a
sha256 is given it is verified before extraction — a mismatch raises rather than proceeding.
"""
from __future__ import annotations

import argparse
import hashlib
import pathlib
import tarfile
import tempfile
import urllib.request
import zipfile

FFPROBE_NAMES = {"ffprobe", "ffprobe.exe"}


class ChecksumError(RuntimeError):
    pass


def _sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def extract_ffprobe(archive: pathlib.Path, dest: pathlib.Path) -> pathlib.Path:
    """Pull the ffprobe binary out of a .tar.xz or .zip, wherever it lives, to ``dest``."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    name = str(archive).lower()
    if name.endswith(".zip") or zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as zf:
            entry = next(m for m in zf.namelist()
                         if pathlib.PurePosixPath(m).name in FFPROBE_NAMES)
            dest.write_bytes(zf.read(entry))
    else:
        with tarfile.open(archive) as tf:
            info = next(m for m in tf.getmembers()
                        if m.isfile() and pathlib.PurePosixPath(m.name).name in FFPROBE_NAMES)
            extracted = tf.extractfile(info)
            if extracted is None:
                raise RuntimeError(f"could not read {info.name} from {archive}")
            with extracted:
                dest.write_bytes(extracted.read())
    dest.chmod(0o755)  # harmless on Windows
    return dest


def fetch(url: str, dest: pathlib.Path, sha256: str | None = None) -> pathlib.Path:
    with tempfile.TemporaryDirectory() as tmp:
        archive = pathlib.Path(tmp) / "download"
        urllib.request.urlretrieve(url, archive)  # https release asset, hash-checked below
        if sha256:
            actual = _sha256(archive)
            if actual != sha256:
                raise ChecksumError(
                    f"sha256 mismatch for {url}\n  expected {sha256}\n  actual   {actual}")
        return extract_ffprobe(archive, dest)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", required=True, help="the ffprobe build archive to download")
    ap.add_argument("--out", type=pathlib.Path, required=True, help="write the ffprobe binary here")
    ap.add_argument("--sha256", help="verify the archive against this sha256 before extracting")
    args = ap.parse_args()
    placed = fetch(args.url, args.out, args.sha256)
    print(f"fetched ffprobe -> {placed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
