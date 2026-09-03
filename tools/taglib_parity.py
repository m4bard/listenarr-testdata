#!/usr/bin/env python3
"""Measure whether TagLib# reports the same media properties as ffprobe, field by field.

Listenarr reads media metadata by shelling out to ffprobe, which it downloads from GitHub at
first boot. That download subsystem has already produced three upstream issues (a broken macOS
install, the fix for it, and unpinned/unverified URLs). Readarr and Lidarr — the audio-focused
projects in the same lineage — have no ffmpeg-family dependency at all; they read media through
TagLib#. Listenarr already references TagLibSharp and already ships it, but only uses it to
WRITE tags: nothing reads ``Properties.Duration``, ``AudioBitrate`` or ``AudioSampleRate``.

So there is a question with a number for an answer: for the fields Listenarr actually consumes,
does TagLib# agree with ffprobe? If it does, an entire download-at-boot subsystem is deletable.
If it agrees on everything except bitrate, that is a different and much more interesting answer.

**This tool measures. It does not fix anything and it changes no Listenarr code.** Its output is
evidence for a decision someone else makes.

How the comparison is made honest
---------------------------------

Comparing raw ffprobe JSON against raw TagLib# properties would answer the wrong question: it
would measure the two libraries, not what Listenarr would end up holding. Listenarr's
``FfprobeMetadataMapper``/``FfprobeTagMetadataMapper`` sit in between, and they do real work —
they split ``format_name`` on a comma, they special-case ``.m4b`` by file extension, they read
``format.bit_rate`` in bits per second, they prefer format tags over stream tags. So both sides
are mapped down to the same ``AudioMetadata`` shape before anything is compared:

* the ffprobe side is a faithful mirror of the two C# mappers, quirks included (see
  ``listenarr_ffprobe_view``);
* the TagLib# side is a PROPOSED mapping, because Listenarr has no such code today. It is
  written out explicitly in ``taglib_view`` so a reviewer can disagree with it in one place
  rather than having to trust a summary.

The ffprobe invocation itself is imported from ``ffprobe_equivalence`` rather than re-spelled,
so this harness cannot drift away from Listenarr's real command line.

The silence problem, stated up front
------------------------------------

This repository generates one second of silence for its library fixtures, and silence is not a
fair test of a bitrate reading. A VBR encoder given silence emits almost nothing, so the average
bitrate it reports describes the encoder's floor rather than any recording. A parity result
measured on silence would look clean for reasons that have nothing to do with real audio.

So the default matrix here is generated from pink noise, not silence, and it also builds a
silence arm alongside it so the distortion is quantified rather than merely disclaimed. The
report prints both arms and the gap between them. Nothing generated is ever committed.

Usage
-----

    # generate the matrix and measure
    python3 tools/taglib_parity.py --generate build/parity-matrix

    # measure an existing tree of real files, generating nothing
    python3 tools/taglib_parity.py --corpus /some/library --json parity.json

Real-file paths are only ever reported relative to the corpus root, so a run over somebody's
library produces a report that can be pasted somewhere without carrying their filesystem.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import pathlib
import re
import shutil
import statistics
import subprocess
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from ffprobe_equivalence import probe as ffprobe_probe

ROOT = pathlib.Path(__file__).resolve().parent.parent
PROBE_PROJECT = ROOT / "tools" / "taglib_probe"
DEFAULT_PROBE_BUILD = ROOT / "build" / "taglib-probe"

AUDIO_EXTENSIONS = frozenset({"mp3", "m4b", "m4a", "aac", "flac", "ogg", "opus", "wav"})


class ParityError(RuntimeError):
    """The measurement cannot be taken as asked. Always raised, never swallowed into a verdict."""


# ---------------------------------------------------------------------------
# The fields under measurement
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Tolerance:
    """How far apart two readings may be and still count as practically the same value.

    Both bounds must be satisfied for a pair to count as tolerant-equal, so a large relative
    allowance cannot wave through an absurd absolute gap on a long file, and vice versa.
    """

    absolute: float
    relative: float

    def holds(self, left: float, right: float) -> bool:
        gap = abs(left - right)
        scale = max(abs(left), abs(right))
        return gap <= self.absolute and (scale == 0.0 or gap / scale <= self.relative)

    def describe(self) -> str:
        return f"within {self.absolute:g} and {self.relative:.1%}"


@dataclass(frozen=True)
class FieldSpec:
    """One comparable field, plus what a disagreement on it would actually cost."""

    key: str
    label: str
    numeric: bool
    tolerance: Tolerance | None
    stakes: str


FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec(
        "duration_seconds", "duration", True, Tolerance(1.0, 0.01),
        "Shown to the user and used to reason about a book's length. A second either way is "
        "invisible; a VBR file read as half its length is not.",
    ),
    FieldSpec(
        "bitrate_bps", "bitrate", True, Tolerance(3000.0, 0.05),
        "Feeds quality decisions; upstream #549 turned on exactly this field. The absolute "
        "bound is 3 kbit/s because that is what the unavoidable overhead measured at on this "
        "matrix: one kbit/s of TagLib# rounding plus the container and tag bytes that ffprobe "
        "folds into format.bit_rate, which came to between 1.3 and 2.6 kbit/s on MP4.",
    ),
    FieldSpec(
        "quality_rung", "quality rung", False, None,
        "The one that decides whether the bitrate gap is real. Both bitrates are pushed "
        "through Listenarr's own QualityMatcher arithmetic and the default profile's ladder; "
        "if they land on the same rung, the difference in the raw integers changed nothing.",
    ),
    FieldSpec("sample_rate_hz", "sample rate", True, None, "Quality reporting."),
    FieldSpec("channels", "channels", True, None, "Quality reporting."),
    FieldSpec(
        "container", "container", False, None,
        "Used for display and for format-preference matching.",
    ),
    FieldSpec("codec", "codec", False, None, "Display, and format preference."),
    FieldSpec("title", "title", False, None, "Falls back to the filename, so rarely empty."),
    FieldSpec("artist", "artist", False, None, "Author attribution."),
    FieldSpec("album", "album", False, None, "Series or book grouping."),
    FieldSpec("album_artist", "album artist", False, None, "Author attribution."),
    FieldSpec("track", "track", True, None, "Part ordering within a book."),
    FieldSpec("disc", "disc", True, None, "Part ordering within a book."),
    FieldSpec("year", "year", True, None, "Match confirmation."),
)

FIELD_BY_KEY = {spec.key: spec for spec in FIELDS}


# ---------------------------------------------------------------------------
# The derived field: does a bitrate difference actually change a decision?
# ---------------------------------------------------------------------------

# The rungs the default quality profile seeds, per codec group (QualityProfileService).
DEFAULT_RUNGS_KBPS: tuple[int, ...] = (64, 128, 192, 256, 320)

# QualityMatcher.IsLosslessFile short-circuits the bitrate comparison entirely for these, so a
# bitrate disagreement on a FLAC cannot change the rung it lands on.
LOSSLESS_CODECS = frozenset({"flac", "alac", "aiff", "ape", "dsd", "wavpack",
                             "pcm_s16le", "pcm_s24le", "pcm_s16be", "pcm_f32le"})

# The default profile seeds AAC and MP3 rungs only. An Opus or Vorbis file finds no codec-
# specific rung and no codec-less fallback, so QualityMatcher returns CodecMismatch whatever
# the bitrate says — which means a bitrate disagreement on those formats is moot under a
# default profile, and only bites once someone adds a rung for them.
DEFAULT_PROFILE_CODECS = frozenset({"aac", "mp3"})


def normalize_kbps(bits_per_second: float | int | None) -> int | None:
    """QualityMatcher.NormalizeKbps, including its guard against values already in kbit/s."""
    if not isinstance(bits_per_second, int | float) or bits_per_second <= 0:
        return None
    bps = int(bits_per_second)
    return round(bps / 1000) if bps >= 1000 else bps


def quality_rung(bitrate_bps: float | int | None, codec: str | None) -> str:
    """Which rung of the default profile a file with this bitrate and codec would land on.

    A mirror of the bitrate half of ``QualityMatcher.Match``: the best rung at or below the
    file's kbit/s, falling back to the worst rung when the file is under all of them, and to
    the worst rung again when the bitrate is unknown.

    That last fallback is only true of the DEFAULT profile, which seeds no VBR rung. Add one
    and an unknown bitrate stops landing on 64kbps and starts landing on the VBR rung instead,
    at which point a reader that returns no bitrate at all (TagLib# on Opus) diverges from one
    that returns a number. So this field understates the risk for anyone with a VBR rung
    configured, and the raw bitrate column is where that case stays visible.
    """
    name = (codec or "").strip().lower()
    if name in LOSSLESS_CODECS:
        return "lossless (bitrate ignored)"
    if name and name not in DEFAULT_PROFILE_CODECS:
        return f"no rung for {name} under the default profile"
    kbps = normalize_kbps(bitrate_bps)
    if kbps is None:
        return f"{min(DEFAULT_RUNGS_KBPS)}kbps"
    eligible = [rung for rung in DEFAULT_RUNGS_KBPS if rung <= kbps]
    return f"{max(eligible) if eligible else min(DEFAULT_RUNGS_KBPS)}kbps"


def add_derived(view: dict[str, Any]) -> dict[str, Any]:
    """Fill the fields that are computed from a view rather than read from a file."""
    view["quality_rung"] = quality_rung(view["bitrate_bps"], view["codec"])
    return view


# ---------------------------------------------------------------------------
# Side A: what Listenarr gets from ffprobe today
# ---------------------------------------------------------------------------


def _first_audio_stream(probe_json: dict[str, Any]) -> dict[str, Any]:
    streams = probe_json.get("streams")
    if not isinstance(streams, list):
        return {}
    for stream in streams:
        if isinstance(stream, dict) and stream.get("codec_type") == "audio":
            return stream
    return {}


def _tag_lookup(tags: dict[str, Any], names: Sequence[str]) -> str | None:
    """FfprobeTagMetadataMapper.GetTag: first non-blank match, case-insensitively, trimmed."""
    lowered = {str(key).lower(): value for key, value in tags.items()}
    for name in names:
        value = tags.get(name)
        if not isinstance(value, str):
            value = lowered.get(name.lower())
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _tag_number(tags: dict[str, Any], names: Sequence[str]) -> int | None:
    """FfprobeTagMetadataMapper.ParseNumericTag: take the part before any slash, first digits."""
    raw = _tag_lookup(tags, names)
    if raw is None:
        return None
    parts = [piece.strip() for piece in raw.split("/") if piece.strip()]
    token = parts[0] if parts else raw
    match = re.search(r"\d+", token)
    return int(match.group()) if match else None


TEXT_TAGS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("title", ("title", "TITLE")),
    ("artist", ("artist", "ARTIST")),
    ("album", ("album", "ALBUM")),
    ("album_artist", ("album_artist", "ALBUM_ARTIST", "album artist")),
)

NUMERIC_TAGS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("track", ("track", "TRACK", "tracknumber", "TRACKNUMBER")),
    ("disc", ("disc", "DISC", "discnumber", "DISCNUMBER")),
    ("year", ("date", "DATE", "year", "YEAR")),
)


def _apply_ffprobe_tags(view: dict[str, Any], tags: dict[str, Any]) -> None:
    """FfprobeTagMetadataMapper.Apply — first writer wins, which is why format tags beat stream."""
    for key, names in TEXT_TAGS:
        if not view[key]:
            view[key] = _tag_lookup(tags, names) or ""
    for key, names in NUMERIC_TAGS:
        if view[key] is None:
            view[key] = _tag_number(tags, names)


def listenarr_ffprobe_view(probe_json: dict[str, Any], path: pathlib.Path) -> dict[str, Any]:
    """What ``AudioMetadata`` Listenarr ends up holding for this file, today.

    A line-for-line mirror of ``FfprobeMetadataMapper`` and ``FfprobeTagMetadataMapper``,
    including two behaviours that are easy to mistake for bugs in this function:

    * ``format_name`` is split on a comma and only the first piece survives, so an ``.m4a``
      whose format is ``mov,mp4,m4a,3gp,3g2,mj2`` becomes the container ``MOV``. Only ``.m4b``
      is rescued, and it is rescued by file extension rather than by content.
    * the stream bitrate is read into a nullable that has just been left null when
      ``format.bit_rate`` was absent, and the C# guard is ``BitRate == 0`` — which is false for
      null. So the stream fallback is unreachable in practice. Mirrored rather than corrected:
      the point is what Listenarr gets, not what it meant to get.
    """
    fmt = probe_json.get("format", {})
    fmt = fmt if isinstance(fmt, dict) else {}
    stream = _first_audio_stream(probe_json)

    view: dict[str, Any] = {
        "duration_seconds": None,
        "bitrate_bps": None,
        "sample_rate_hz": None,
        "channels": None,
        "container": None,
        "codec": None,
        "title": "",
        "artist": "",
        "album": "",
        "album_artist": "",
        "track": None,
        "disc": None,
        "year": None,
    }

    duration = fmt.get("duration")
    if isinstance(duration, str):
        with contextlib.suppress(ValueError):
            view["duration_seconds"] = float(duration)

    format_name = fmt.get("format_name")
    ext = path.suffix.lstrip(".").lower()
    if isinstance(format_name, str):
        primary = format_name.split(",")[0]
        view["container"] = "M4B" if ext == "m4b" else primary.upper()

    bit_rate = fmt.get("bit_rate")
    if isinstance(bit_rate, str):
        with contextlib.suppress(ValueError):
            view["bitrate_bps"] = int(bit_rate)

    format_tags = fmt.get("tags")
    if isinstance(format_tags, dict):
        _apply_ffprobe_tags(view, format_tags)

    sample_rate = stream.get("sample_rate")
    if isinstance(sample_rate, str):
        with contextlib.suppress(ValueError):
            view["sample_rate_hz"] = int(sample_rate)
    if isinstance(stream.get("channels"), int):
        view["channels"] = stream["channels"]
    stream_bit_rate = stream.get("bit_rate")
    if isinstance(stream_bit_rate, str) and view["bitrate_bps"] == 0:
        with contextlib.suppress(ValueError):
            view["bitrate_bps"] = int(stream_bit_rate)
    if isinstance(stream.get("codec_name"), str):
        view["codec"] = stream["codec_name"]
    stream_tags = stream.get("tags")
    if isinstance(stream_tags, dict):
        _apply_ffprobe_tags(view, stream_tags)

    if not view["title"]:
        view["title"] = path.stem
    if not view["container"]:
        view["container"] = ext.upper()

    return add_derived(view)


# ---------------------------------------------------------------------------
# Side B: what a TagLib#-based implementation would get
# ---------------------------------------------------------------------------

# TagLib# names a codec in prose, so mapping it onto ffprobe's short name needs a table. An
# unmatched description is passed through verbatim rather than guessed at, so a disagreement
# row shows the actual string TagLib# produced and the table can be extended from evidence.
TAGLIB_CODEC_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"MPEG Version .*Layer 3", re.IGNORECASE), "mp3"),
    (re.compile(r"MPEG Version .*Layer 2", re.IGNORECASE), "mp2"),
    (re.compile(r"MPEG Version .*Layer 1", re.IGNORECASE), "mp1"),
    (re.compile(r"MPEG-4 Audio", re.IGNORECASE), "aac"),
    (re.compile(r"^ADTS", re.IGNORECASE), "aac"),
    (re.compile(r"Flac", re.IGNORECASE), "flac"),
    (re.compile(r"Vorbis", re.IGNORECASE), "vorbis"),
    (re.compile(r"Opus", re.IGNORECASE), "opus"),
    (re.compile(r"WAVE|PCM", re.IGNORECASE), "pcm_s16le"),
)


def _codec_from_description(description: str | None) -> str | None:
    if not description:
        return None
    for pattern, name in TAGLIB_CODEC_PATTERNS:
        if pattern.search(description):
            return name
    return description


def _first(values: Any) -> str:
    if isinstance(values, list):
        for value in values:
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _positive(value: Any) -> int | None:
    """TagLib# uses 0 for an absent number where ffprobe simply omits the tag.

    Folding 0 to None is a mapping choice and it is the charitable one: without it every
    untagged file would count as a disagreement on track, disc and year at once, which would
    say more about the two libraries' null conventions than about Listenarr.
    """
    return value if isinstance(value, int) and value > 0 else None


def taglib_view(record: dict[str, Any], path: pathlib.Path) -> dict[str, Any]:
    """The ``AudioMetadata`` a TagLib#-based reader would produce, under a proposed mapping.

    Listenarr has no such code, so every choice here is this harness's, and each one is a place
    a reviewer may reasonably disagree:

    * bitrate is scaled from TagLib#'s kbit/s to ffprobe's bit/s by a flat 1000. TagLib# has no
      finer resolution than a whole kbit/s, so this is the best any implementation could do and
      the residue is a property of the library rather than of the mapping.
    * the container comes from the MIME subtype (``taglib/m4b`` -> ``M4B``) with no extension
      special case, because TagLib# does not need one.
    * title falls back to the filename stem, matching what Listenarr already does.
    """
    view: dict[str, Any] = {key: None for key in FIELD_BY_KEY}
    view.update({"title": "", "artist": "", "album": "", "album_artist": ""})

    properties = record.get("properties")
    if isinstance(properties, dict):
        duration = properties.get("duration_seconds")
        if isinstance(duration, int | float):
            view["duration_seconds"] = float(duration)
        kbps = properties.get("audio_bitrate_kbps")
        if isinstance(kbps, int) and kbps > 0:
            view["bitrate_bps"] = kbps * 1000
        view["sample_rate_hz"] = _positive(properties.get("audio_sample_rate_hz"))
        view["channels"] = _positive(properties.get("audio_channels"))
        view["codec"] = _codec_from_description(properties.get("description"))

    mime = record.get("mime_type")
    if isinstance(mime, str) and "/" in mime:
        view["container"] = mime.split("/", 1)[1].upper()

    tag = record.get("tag")
    if isinstance(tag, dict):
        view["title"] = (tag.get("title") or "").strip()
        view["artist"] = _first(tag.get("performers"))
        view["album"] = (tag.get("album") or "").strip()
        view["album_artist"] = _first(tag.get("album_artists"))
        view["track"] = _positive(tag.get("track"))
        view["disc"] = _positive(tag.get("disc"))
        view["year"] = _positive(tag.get("year"))

    if not view["title"]:
        view["title"] = path.stem
    if not view["container"]:
        view["container"] = path.suffix.lstrip(".").upper()

    return add_derived(view)


# ---------------------------------------------------------------------------
# Driving the two readers
# ---------------------------------------------------------------------------


def resolve_dotnet(explicit: str | None) -> str:
    """Find a dotnet host. The SDK is often installed per-user and not on PATH."""
    for candidate in (explicit, os.environ.get("DOTNET")):
        if candidate:
            return candidate
    found = shutil.which("dotnet")
    if found:
        return found
    per_user = pathlib.Path.home() / ".dotnet" / "dotnet"
    if per_user.exists():
        return str(per_user)
    raise ParityError(
        "no dotnet host found — pass --dotnet, set DOTNET, or put dotnet on PATH. "
        "The TagLib# side cannot be measured without it, and guessing it would be a fabricated "
        "measurement."
    )


def build_taglib_probe(dotnet: str, out_dir: pathlib.Path) -> pathlib.Path:
    """Build the console probe against the same TagLibSharp version Listenarr pins."""
    if not PROBE_PROJECT.exists():
        raise ParityError(f"the probe project is missing from {PROBE_PROJECT.name}")
    result = subprocess.run(
        [dotnet, "build", str(PROBE_PROJECT), "-c", "Release", "-o", str(out_dir),
         "--nologo", "-v", "quiet"],
        capture_output=True, text=True, encoding="utf-8",
        env={**os.environ, "DOTNET_CLI_TELEMETRY_OPTOUT": "1", "DOTNET_NOLOGO": "1"},
    )
    dll = out_dir / "taglib-probe.dll"
    if result.returncode != 0 or not dll.exists():
        raise ParityError(
            "could not build the TagLib# probe:\n"
            f"{(result.stdout + result.stderr).strip()[:1500]}"
        )
    return dll


def run_taglib_probe(dotnet: str, dll: pathlib.Path,
                     files: Sequence[pathlib.Path]) -> tuple[str, dict[str, dict[str, Any]]]:
    """Feed every path to the probe and return its banner version plus one record per file."""
    payload = "\n".join(str(f) for f in files) + "\n"
    result = subprocess.run(
        [dotnet, str(dll)], input=payload, capture_output=True, text=True, encoding="utf-8",
        env={**os.environ, "DOTNET_CLI_TELEMETRY_OPTOUT": "1", "DOTNET_NOLOGO": "1"},
    )
    if result.returncode != 0:
        raise ParityError(f"the TagLib# probe failed: {result.stderr.strip()[:800]}")

    version = ""
    records: dict[str, dict[str, Any]] = {}
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parsed = json.loads(line)
        if parsed.get("kind") == "banner":
            version = str(parsed.get("taglib_version") or "")
            continue
        records[str(parsed.get("path"))] = parsed

    # A run whose provenance is unknown is not evidence. Refusing here is the same reflex as
    # ffprobe_equivalence refusing to report equivalence over an empty corpus.
    if not version:
        raise ParityError("the TagLib# probe printed no version banner — refusing to attribute "
                          "a measurement to an unidentified build")
    missing = [str(f) for f in files if str(f) not in records]
    if missing:
        raise ParityError(
            f"the TagLib# probe returned no row for {len(missing)} file(s), first: {missing[0]}"
        )
    return version, records


def ffprobe_version(ffprobe: str) -> str:
    result = subprocess.run([ffprobe, "-version"], capture_output=True, text=True,
                            encoding="utf-8")
    return result.stdout.splitlines()[0] if result.stdout else ffprobe


# ---------------------------------------------------------------------------
# The generated matrix
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Spec:
    """One recipe in the matrix: a container, a codec, an encoding mode, and a rate."""

    key: str
    ext: str
    mode: str
    args: tuple[str, ...]
    sample_rate: int = 44100
    channels: int = 1
    muxer: str | None = None
    note: str = ""


# Round tier values are the ones that flatter a bitrate comparison, so most of these are not
# round. Upstream #549 was reported fixed on the strength of a suite whose every bitrate was an
# exact tier value; on a real library the mismatch rate was ninety percent. A matrix of 128s and
# 256s would reproduce that failure exactly, so the awkward values carry the weight here and the
# round ones are only present as a control.
#
# mp3 CBR is a special case worth keeping: libmp3lame can only emit the MPEG tier rates, so
# asking it for 137k gets a tier back. That snap is itself worth recording.
MATRIX: tuple[Spec, ...] = (
    Spec("mp3-cbr-128", "mp3", "CBR", ("-c:a", "libmp3lame", "-b:a", "128k"),
         note="round tier value, the control case"),
    Spec("mp3-cbr-112", "mp3", "CBR", ("-c:a", "libmp3lame", "-b:a", "112k")),
    Spec("mp3-cbr-137-snaps", "mp3", "CBR", ("-c:a", "libmp3lame", "-b:a", "137k"),
         note="a non-tier request; libmp3lame snaps it, which is worth recording"),
    Spec("mp3-abr-117", "mp3", "ABR", ("-c:a", "libmp3lame", "-b:a", "117k", "-abr", "1")),
    Spec("mp3-vbr-q2", "mp3", "VBR", ("-c:a", "libmp3lame", "-q:a", "2")),
    Spec("mp3-vbr-q5", "mp3", "VBR", ("-c:a", "libmp3lame", "-q:a", "5")),
    Spec("mp3-vbr-q8", "mp3", "VBR", ("-c:a", "libmp3lame", "-q:a", "8")),
    Spec("mp3-vbr-q5-no-xing", "mp3", "VBR",
         ("-c:a", "libmp3lame", "-q:a", "5", "-write_xing", "0"),
         note="no Xing header, so both readers must estimate rather than read"),
    Spec("mp3-cbr-96-22k", "mp3", "CBR", ("-c:a", "libmp3lame", "-b:a", "96k"),
         sample_rate=22050),
    Spec("mp3-cbr-192-stereo", "mp3", "CBR", ("-c:a", "libmp3lame", "-b:a", "192k"), channels=2),
    Spec("m4b-cbr-93", "m4b", "CBR", ("-c:a", "aac", "-b:a", "93k"), muxer="mp4"),
    Spec("m4b-cbr-141", "m4b", "CBR", ("-c:a", "aac", "-b:a", "141k"), muxer="mp4"),
    Spec("m4b-cbr-47", "m4b", "CBR", ("-c:a", "aac", "-b:a", "47k"), muxer="mp4"),
    Spec("m4b-cbr-64-22k", "m4b", "CBR", ("-c:a", "aac", "-b:a", "64k"), sample_rate=22050,
         muxer="mp4"),
    Spec("m4b-vbr-q1.4", "m4b", "VBR", ("-c:a", "aac", "-q:a", "1.4"), muxer="mp4"),
    Spec("m4b-vbr-q0.6", "m4b", "VBR", ("-c:a", "aac", "-q:a", "0.6"), muxer="mp4"),
    Spec("m4b-cbr-128-stereo", "m4b", "CBR", ("-c:a", "aac", "-b:a", "128k"), channels=2,
         muxer="mp4"),
    Spec("m4a-cbr-128", "m4a", "CBR", ("-c:a", "aac", "-b:a", "128k"), muxer="mp4"),
    Spec("m4a-cbr-83", "m4a", "CBR", ("-c:a", "aac", "-b:a", "83k"), muxer="mp4"),
    Spec("flac-default", "flac", "lossless", ("-c:a", "flac")),
    Spec("flac-c0", "flac", "lossless", ("-c:a", "flac", "-compression_level", "0")),
    Spec("flac-c8-48k", "flac", "lossless", ("-c:a", "flac", "-compression_level", "8"),
         sample_rate=48000),
    Spec("opus-vbr-96", "opus", "VBR", ("-c:a", "libopus", "-b:a", "96k")),
    Spec("opus-vbr-53", "opus", "VBR", ("-c:a", "libopus", "-b:a", "53k")),
    Spec("opus-cbr-64", "opus", "CBR", ("-c:a", "libopus", "-b:a", "64k", "-vbr", "off")),
    Spec("opus-cvbr-79", "opus", "CVBR",
         ("-c:a", "libopus", "-b:a", "79k", "-vbr", "constrained")),
    Spec("opus-vbr-96-22k", "opus", "VBR", ("-c:a", "libopus", "-b:a", "96k"), sample_rate=22050,
         note="opus always decodes at 48 kHz; the input rate is what differs"),
    Spec("ogg-cbr-112", "ogg", "ABR", ("-c:a", "libvorbis", "-b:a", "112k")),
    Spec("ogg-vbr-q3", "ogg", "VBR", ("-c:a", "libvorbis", "-q:a", "3")),
    Spec("ogg-vbr-q7", "ogg", "VBR", ("-c:a", "libvorbis", "-q:a", "7")),
    Spec("ogg-abr-83", "ogg", "ABR", ("-c:a", "libvorbis", "-b:a", "83k")),
    Spec("wav-pcm", "wav", "uncompressed", ("-c:a", "pcm_s16le")),
)

SIGNALS: tuple[str, ...] = ("noise", "silence", "tone")
NOISE_SEED = 20260903

TAG_ARGS: tuple[str, ...] = (
    "-metadata", "title=Ayesha The Return of She",
    "-metadata", "artist=H. Rider Haggard",
    "-metadata", "album=She",
    "-metadata", "album_artist=H. Rider Haggard",
    "-metadata", "track=3/12",
    "-metadata", "disc=1/2",
    "-metadata", "date=1905",
)


def _lavfi_source(signal: str, sample_rate: int, seconds: float) -> list[str]:
    """The ffmpeg input for one signal arm.

    Pink noise is the default because it is broadband and incompressible enough that a VBR
    encoder has to spend real bits on it. Silence is generated too, as the contrast arm: it is
    what this repository's library fixtures contain, and the report exists partly to show what
    measuring on it would have cost.
    """
    if signal == "noise":
        return ["-f", "lavfi", "-i",
                f"anoisesrc=r={sample_rate}:c=pink:d={seconds}:seed={NOISE_SEED}:a=0.5"]
    if signal == "tone":
        return ["-f", "lavfi", "-i", f"sine=frequency=440:r={sample_rate}:duration={seconds}"]
    if signal == "silence":
        return ["-f", "lavfi", "-i", f"anullsrc=r={sample_rate}:cl=mono", "-t", str(seconds)]
    raise ParityError(f"unknown signal {signal!r} (have: {', '.join(SIGNALS)})")


def generate_matrix(out_dir: pathlib.Path, signals: Sequence[str], seconds: float,
                    ffmpeg: str) -> list[pathlib.Path]:
    """Write one file per (signal, spec). Never committed: these live under build/ by default."""
    if shutil.which(ffmpeg) is None and not pathlib.Path(ffmpeg).exists():
        raise ParityError(f"ffmpeg not found at {ffmpeg!r}; the matrix cannot be generated")
    if out_dir.exists():
        shutil.rmtree(out_dir)
    made: list[pathlib.Path] = []
    skipped: list[str] = []
    for signal in signals:
        for spec in MATRIX:
            dest = out_dir / signal / f"{spec.key}.{spec.ext}"
            dest.parent.mkdir(parents=True, exist_ok=True)
            cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                   *_lavfi_source(signal, spec.sample_rate, seconds),
                   "-ac", str(spec.channels), *spec.args, *TAG_ARGS]
            if spec.muxer:
                cmd += ["-f", spec.muxer]
            cmd.append(str(dest))
            result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
            if result.returncode == 0 and dest.exists() and dest.stat().st_size > 0:
                made.append(dest)
            else:
                skipped.append(f"{signal}/{spec.key}: {result.stderr.strip()[:90]}")
    for line in skipped:
        print(f"  (skipped {line})", file=sys.stderr)
    return made


def collect_corpus(root: pathlib.Path) -> list[pathlib.Path]:
    """Every audio file under ``root``, recursively, sorted. Nothing about the tree is assumed."""
    if not root.is_dir():
        raise ParityError(f"{root} is not a directory")
    return sorted(
        path for path in root.rglob("*")
        if path.is_file() and path.suffix.lstrip(".").lower() in AUDIO_EXTENSIONS
    )


# ---------------------------------------------------------------------------
# Comparison and reporting
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Comparison:
    """One field on one file, as both readers saw it."""

    relative_path: str
    arm: str
    field: str
    ffprobe: Any
    taglib: Any
    exact: bool
    tolerant: bool

    def delta(self) -> float | None:
        if isinstance(self.ffprobe, int | float) and isinstance(self.taglib, int | float):
            return float(self.taglib) - float(self.ffprobe)
        return None


@dataclass
class FieldTally:
    compared: int = 0
    exact: int = 0
    tolerant: int = 0
    disagreements: list[Comparison] = field(default_factory=list)

    def exact_rate(self) -> float:
        return self.exact / self.compared if self.compared else 0.0

    def tolerant_rate(self) -> float:
        return self.tolerant / self.compared if self.compared else 0.0


def compare_views(spec: FieldSpec, ffprobe_value: Any, taglib_value: Any) -> tuple[bool, bool]:
    """Return (exact, tolerant) for one field.

    Text is compared case-insensitively after trimming: a container reported as ``opus`` versus
    ``OPUS`` is not the disagreement anyone cares about, and letting it count as one would bury
    the disagreements that matter.
    """
    if spec.numeric:
        both_numeric = (isinstance(ffprobe_value, int | float)
                        and isinstance(taglib_value, int | float))
        if not both_numeric:
            same = ffprobe_value == taglib_value
            return same, same
        exact = float(ffprobe_value) == float(taglib_value)
        tolerant = exact or (spec.tolerance is not None
                             and spec.tolerance.holds(float(ffprobe_value), float(taglib_value)))
        return exact, tolerant

    left = ffprobe_value.strip().lower() if isinstance(ffprobe_value, str) else ffprobe_value
    right = taglib_value.strip().lower() if isinstance(taglib_value, str) else taglib_value
    same = left == right
    return same, same


@dataclass
class Measurement:
    """Everything one run learned. Serializable, so a run over a real library can be shipped."""

    ffprobe_build: str = ""
    taglib_build: str = ""
    files: int = 0
    unreadable: list[dict[str, str]] = field(default_factory=list)
    tallies: dict[str, FieldTally] = field(default_factory=dict)
    # The same tallies split by top-level directory. On a generated run that directory is the
    # signal arm, which is what turns the silence caveat from a disclaimer into a number.
    per_arm: dict[str, dict[str, FieldTally]] = field(default_factory=dict)
    per_arm_bitrate_gap: dict[str, list[float]] = field(default_factory=dict)
    arms: list[str] = field(default_factory=list)


def measure(files: Sequence[pathlib.Path], root: pathlib.Path, ffprobe: str,
            taglib_records: dict[str, dict[str, Any]]) -> Measurement:
    """Read every file with both tools, map both onto AudioMetadata, and tally the differences."""
    result = Measurement(tallies={spec.key: FieldTally() for spec in FIELDS})
    arms: set[str] = set()

    for path in files:
        relative = path.relative_to(root).as_posix()
        arm = path.relative_to(root).parts[0] if len(path.relative_to(root).parts) > 1 else "."
        arms.add(arm)

        record = taglib_records[str(path)]
        if not record.get("ok"):
            result.unreadable.append(
                {"file": relative, "reader": "taglib", "error": str(record.get("error"))})
            continue
        try:
            probe_json = ffprobe_probe(ffprobe, path)
        # Any ffprobe failure is a result to record, not a crash to propagate.
        except Exception as exc:
            result.unreadable.append({"file": relative, "reader": "ffprobe", "error": str(exc)})
            continue

        left = listenarr_ffprobe_view(probe_json, path)
        right = taglib_view(record, path)
        result.files += 1

        arm_tallies = result.per_arm.setdefault(
            arm, {spec.key: FieldTally() for spec in FIELDS})
        for spec in FIELDS:
            exact, tolerant = compare_views(spec, left[spec.key], right[spec.key])
            for tally in (result.tallies[spec.key], arm_tallies[spec.key]):
                tally.compared += 1
                tally.exact += int(exact)
                tally.tolerant += int(tolerant)
            comparison = Comparison(relative, arm, spec.key, left[spec.key], right[spec.key],
                                    exact, tolerant)
            if not exact:
                # Only the overall tally carries the list; the per-arm ones are counts, and
                # duplicating every row into both would double the JSON for nothing.
                result.tallies[spec.key].disagreements.append(comparison)
            if spec.key == "bitrate_bps":
                delta = comparison.delta()
                if delta is not None and isinstance(left[spec.key], int | float) \
                        and left[spec.key]:
                    result.per_arm_bitrate_gap.setdefault(arm, []).append(
                        abs(delta) / float(left[spec.key]))

    result.ffprobe_build = ffprobe_version(ffprobe)
    result.arms = sorted(arms)
    return result


def verdict_for(spec: FieldSpec, tally: FieldTally) -> str:
    """A plain-language read on whether TagLib# can carry this field."""
    if tally.compared == 0:
        return "not measured"
    exact = tally.exact_rate()
    tolerant = tally.tolerant_rate()
    if exact == 1.0:
        return "drop-in: every file agreed exactly"
    if spec.tolerance is not None and tolerant == 1.0:
        return f"usable: never exact, always {spec.tolerance.describe()}"
    if spec.tolerance is not None and tolerant >= 0.95:
        return f"mostly usable: {tolerant:.0%} {spec.tolerance.describe()}, with exceptions below"
    if exact >= 0.95:
        return f"mostly usable: {exact:.0%} exact, with exceptions below"
    return "NOT a drop-in: see the disagreements below"


def render(measurement: Measurement, show_all: bool) -> str:
    """The report. Deliberately readable as-is, because that is what gets pasted upstream."""
    out: list[str] = []
    add = out.append

    add("=" * 78)
    add("TagLib# versus ffprobe, over the fields Listenarr consumes")
    add("=" * 78)
    add(f"ffprobe    {measurement.ffprobe_build}")
    add(f"TagLib#    {measurement.taglib_build} (the version Listenarr pins)")
    add(f"files      {measurement.files} compared"
        + (f", {len(measurement.unreadable)} unreadable" if measurement.unreadable else ""))
    if measurement.arms and measurement.arms != ["."]:
        add(f"arms       {', '.join(measurement.arms)}")
    add("")
    add("Both sides are mapped onto Listenarr's AudioMetadata before comparison. The ffprobe")
    add("side mirrors FfprobeMetadataMapper; the TagLib# side is a proposed mapping that does")
    add("not exist in Listenarr today, spelled out in taglib_view() for anyone who wants to")
    add("argue with it.")
    add("")

    add("-" * 78)
    add(f"{'field':<14}{'n':>5}{'exact':>9}{'tolerant':>10}  verdict")
    add("-" * 78)
    for spec in FIELDS:
        tally = measurement.tallies[spec.key]
        tolerant = f"{tally.tolerant_rate():.0%}" if spec.tolerance else "-"
        add(f"{spec.label:<14}{tally.compared:>5}{tally.exact_rate():>9.0%}{tolerant:>10}  "
            f"{verdict_for(spec, tally)}")
    add("-" * 78)
    add("'tolerant' is only defined for fields where being close is genuinely good enough:")
    for spec in FIELDS:
        if spec.tolerance:
            add(f"  {spec.label}: {spec.tolerance.describe()}")
    add("")

    if measurement.per_arm_bitrate_gap:
        add("The silence caveat, measured rather than asserted")
        add("-" * 78)
        add("This repository's library fixtures are one second of silence, and silence gives a")
        add("VBR encoder almost nothing to encode, so a bitrate read from it describes the")
        add("encoder's floor. The matrix is generated from pink noise for that reason, with a")
        add("silence arm kept alongside to show the size of the difference.")
        add("")
        add(f"{'arm':<12}{'files':>7}{'median bitrate gap':>22}{'worst':>10}")
        for arm in sorted(measurement.per_arm_bitrate_gap):
            gaps = measurement.per_arm_bitrate_gap[arm]
            add(f"{arm:<12}{len(gaps):>7}{statistics.median(gaps):>21.1%}{max(gaps):>10.1%}")
        add("")
        add("A silence arm that looks better than the noise arm is the exact failure mode of")
        add("upstream #549: a suite that only asked the question in the form where the answer")
        add("was already right.")
        add("")

    if len(measurement.per_arm) > 1:
        arms = sorted(measurement.per_arm)
        add("Exact agreement per arm, so no arm can carry the headline on its own")
        add("-" * 78)
        add("field         " + "".join(f"{arm:>12}" for arm in arms))
        for spec in FIELDS:
            cells = "".join(
                f"{measurement.per_arm[arm][spec.key].exact_rate():>11.0%} " for arm in arms)
            add(f"{spec.label:<14}{cells}")
        add("")

    for spec in FIELDS:
        tally = measurement.tallies[spec.key]
        if not tally.disagreements:
            continue
        add("=" * 78)
        add(f"{spec.label}: {len(tally.disagreements)} of {tally.compared} files disagree")
        add(f"  why it matters: {spec.stakes}")
        add("-" * 78)
        rows = tally.disagreements if show_all else tally.disagreements[:40]
        for row in rows:
            marker = " " if row.tolerant else "!"
            delta = row.delta()
            suffix = f"   (delta {delta:+g})" if delta is not None else ""
            add(f"{marker} {row.relative_path}")
            add(f"    ffprobe {row.ffprobe!r}   taglib {row.taglib!r}{suffix}")
        if len(rows) < len(tally.disagreements):
            add(f"  ... and {len(tally.disagreements) - len(rows)} more (pass --all)")
        add("")
    if any(t.disagreements for t in measurement.tallies.values()):
        add("A leading '!' marks a disagreement outside the tolerance for that field; a blank")
        add("marks one that is inside it.")
        add("")

    if measurement.unreadable:
        add("=" * 78)
        add(f"unreadable: {len(measurement.unreadable)} file(s)")
        add("-" * 78)
        for entry in measurement.unreadable:
            add(f"  {entry['reader']:<8} {entry['file']}: {entry['error'][:120]}")
        add("")

    return "\n".join(out)


def to_json(measurement: Measurement) -> dict[str, Any]:
    return {
        "ffprobe_build": measurement.ffprobe_build,
        "taglib_build": measurement.taglib_build,
        "files_compared": measurement.files,
        "arms": measurement.arms,
        "unreadable": measurement.unreadable,
        "fields": {
            spec.key: {
                "label": spec.label,
                "compared": measurement.tallies[spec.key].compared,
                "exact": measurement.tallies[spec.key].exact,
                "tolerant": measurement.tallies[spec.key].tolerant,
                "exact_rate": measurement.tallies[spec.key].exact_rate(),
                "tolerant_rate": measurement.tallies[spec.key].tolerant_rate(),
                "tolerance": (spec.tolerance.describe() if spec.tolerance else None),
                "verdict": verdict_for(spec, measurement.tallies[spec.key]),
                "disagreements": [
                    {"file": d.relative_path, "arm": d.arm, "ffprobe": d.ffprobe,
                     "taglib": d.taglib, "within_tolerance": d.tolerant}
                    for d in measurement.tallies[spec.key].disagreements
                ],
            }
            for spec in FIELDS
        },
        "bitrate_gap_by_arm": {
            arm: {"files": len(gaps), "median": statistics.median(gaps), "worst": max(gaps)}
            for arm, gaps in measurement.per_arm_bitrate_gap.items()
        },
        "per_arm": {
            arm: {
                key: {"compared": tally.compared, "exact": tally.exact,
                      "tolerant": tally.tolerant, "exact_rate": tally.exact_rate()}
                for key, tally in tallies.items()
            }
            for arm, tallies in measurement.per_arm.items()
        },
    }


def _signals(raw: str) -> list[str]:
    chosen = [piece.strip() for piece in raw.split(",") if piece.strip()]
    unknown = [piece for piece in chosen if piece not in SIGNALS]
    if unknown:
        raise ParityError(f"unknown signal(s) {', '.join(unknown)}; have: {', '.join(SIGNALS)}")
    return chosen


def main(argv: Iterable[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    source = ap.add_mutually_exclusive_group(required=True)
    source.add_argument("--generate", type=pathlib.Path, metavar="DIR",
                        help="generate the matrix into DIR and measure it (DIR is overwritten)")
    source.add_argument("--corpus", type=pathlib.Path, metavar="DIR",
                        help="measure the audio files already under DIR, generating nothing")
    ap.add_argument("--signals", default="noise,silence",
                    help=f"comma-separated arms to generate ({', '.join(SIGNALS)})")
    ap.add_argument("--seconds", type=float, default=20.0,
                    help="length of each generated file; short files make VBR averages noisy")
    ap.add_argument("--ffmpeg", default="ffmpeg")
    ap.add_argument("--ffprobe", default="ffprobe")
    ap.add_argument("--dotnet", help="path to a dotnet host (default: PATH, $DOTNET, ~/.dotnet)")
    ap.add_argument("--probe-build", type=pathlib.Path, default=DEFAULT_PROBE_BUILD,
                    help="where to build the TagLib# probe")
    ap.add_argument("--json", type=pathlib.Path, help="also write the full result here")
    ap.add_argument("--all", action="store_true", help="list every disagreement, not the first 40")
    ap.add_argument("--strict", action="store_true",
                    help="exit 1 if any field disagreed outside its tolerance")
    args = ap.parse_args(list(argv) if argv is not None else None)

    if shutil.which(args.ffprobe) is None and not pathlib.Path(args.ffprobe).exists():
        raise ParityError(f"ffprobe not found at {args.ffprobe!r}")

    if args.generate:
        root = args.generate
        files = generate_matrix(root, _signals(args.signals), args.seconds, args.ffmpeg)
    else:
        root = args.corpus
        files = collect_corpus(root)

    # Nothing measured is not agreement. Same reflex as ffprobe_equivalence refusing to call an
    # empty corpus equivalent: a harness that reports 100% over zero files is worse than useless,
    # because the number gets quoted.
    if not files:
        print(f"no audio files under {root} — refusing to report agreement over an empty corpus",
              file=sys.stderr)
        return 2

    dotnet = resolve_dotnet(args.dotnet)
    dll = build_taglib_probe(dotnet, args.probe_build)
    taglib_build, records = run_taglib_probe(dotnet, dll, files)

    measurement = measure(files, root, args.ffprobe, records)
    measurement.taglib_build = taglib_build
    print(render(measurement, args.all))

    if args.json:
        args.json.write_text(json.dumps(to_json(measurement), indent=2, sort_keys=True) + "\n")
        print(f"full result -> {args.json}")

    if args.strict:
        offenders = [FIELD_BY_KEY[key].label for key, tally in measurement.tallies.items()
                     if tally.compared and tally.tolerant < tally.compared]
        if offenders:
            print(f"\nSTRICT: disagreement outside tolerance on {', '.join(offenders)}",
                  file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ParityError as exc:
        print(f"taglib_parity: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
