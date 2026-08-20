"""asin_tag_probe.py decides whether a file carries the ASIN tag Listenarr writes.

The whole of validate_asin_tag_embed.sh rests on this one judgment, and the judgment is
asymmetric in a dangerous way: `untagged` is the finding. Anything that makes the reader say
`untagged` when it should not have spoken at all — a truncated file, a format it does not
understand, a tag written under a spelling it does not check — reads as a reproduction of the
bug. So the contract tests below are mostly about the reader refusing to answer.
"""
from __future__ import annotations

import pathlib
import shutil
import subprocess
import sys

import pytest
from mutagen.mp4 import MP4, MP4FreeForm

ROOT = pathlib.Path(__file__).resolve().parent.parent
PROBE = ROOT / "tools" / "asin_tag_probe.py"
ASIN = "B002UUFXKU"

sys.path.insert(0, str(ROOT / "tools"))

from asin_tag_probe import MP4_ATOM, read_asin, stamp_asin


def run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(PROBE), *args], capture_output=True, text=True)


@pytest.fixture
def untagged_m4b(tmp_path: pathlib.Path) -> pathlib.Path:
    """A real, tiny m4b carrying ordinary tags and no ASIN of any kind.

    Synthesized rather than committed, the same way the generator makes its audio, and
    skipped rather than faked when ffmpeg is missing: a stub file would exercise the error
    path and quietly stop testing the judgment.
    """
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg is required to synthesize the fixture audio")
    target = tmp_path / "book.m4b"
    subprocess.run(
        ["ffmpeg", "-v", "quiet", "-y", "-f", "lavfi", "-i", "anullsrc=r=22050:cl=mono",
         "-t", "1", "-c:a", "aac", "-metadata", "title=A Book", str(target)],
        check=True,
    )
    audio = MP4(target)
    if audio.tags is not None:
        audio.tags.pop(MP4_ATOM, None)
        audio.save()
    return target


@pytest.mark.contract
def test_a_file_with_no_asin_reads_untagged_and_exits_one(untagged_m4b: pathlib.Path) -> None:
    result = run("read", str(untagged_m4b))
    assert result.returncode == 1
    assert "untagged" in result.stdout


@pytest.mark.contract
def test_a_stamped_file_reads_tagged_and_exits_zero(untagged_m4b: pathlib.Path) -> None:
    # The positive control the shell script depends on. If this ever stopped holding, every
    # `untagged` verdict the tool has printed would be unfalsifiable.
    stamp_asin(untagged_m4b, ASIN)
    result = run("read", str(untagged_m4b), "--expect-asin", ASIN)
    assert result.returncode == 0
    assert "tagged" in result.stdout
    assert ASIN in result.stdout


@pytest.mark.contract
def test_the_wrong_asin_is_not_reported_as_a_clean_pass(untagged_m4b: pathlib.Path) -> None:
    # Some other book's identifier in the tag is a different failure from no identifier, and
    # neither is a pass.
    stamp_asin(untagged_m4b, "B0069AC0KA")
    result = run("read", str(untagged_m4b), "--expect-asin", ASIN)
    assert result.returncode == 1
    assert "wrong-asin" in result.stdout


@pytest.mark.contract
def test_an_unreadable_file_is_an_error_not_a_finding(tmp_path: pathlib.Path) -> None:
    # A file the reader cannot parse must exit 2. Exiting 1 would let a corrupt or truncated
    # destination masquerade as "Listenarr wrote no tag", which is the exact claim the tool is
    # supposed to be able to make honestly.
    broken = tmp_path / "book.m4b"
    broken.write_text("this is not an MPEG-4 container")
    result = run("read", str(broken))
    assert result.returncode == 2


@pytest.mark.contract
def test_an_unknown_extension_is_an_error_not_a_finding(tmp_path: pathlib.Path) -> None:
    unknown = tmp_path / "book.wav"
    unknown.write_bytes(b"RIFF\x00\x00\x00\x00WAVE")
    result = run("read", str(unknown))
    assert result.returncode == 2


@pytest.mark.contract
def test_a_missing_file_is_an_error_not_a_finding(tmp_path: pathlib.Path) -> None:
    result = run("read", str(tmp_path / "absent.m4b"))
    assert result.returncode == 2


def test_the_stamped_atom_is_the_one_listenarr_writes(untagged_m4b: pathlib.Path) -> None:
    # `TagLibAudioTagWriter.ApplyAsinTag` calls SetDashBox("com.apple.iTunes", "ASIN", ...),
    # which lands in this atom. A control written to any other spelling would prove nothing
    # about whether Listenarr's own write arrived.
    stamp_asin(untagged_m4b, ASIN)
    assert MP4_ATOM == "----:com.apple.iTunes:ASIN"
    tags = MP4(untagged_m4b).tags
    assert tags is not None
    assert bytes(tags[MP4_ATOM][0]).decode() == ASIN


def test_reading_back_a_hand_written_atom_agrees_with_the_reader(
    untagged_m4b: pathlib.Path,
) -> None:
    audio = MP4(untagged_m4b)
    assert audio.tags is not None
    audio.tags[MP4_ATOM] = [MP4FreeForm(ASIN.encode("utf-8"))]
    audio.save()
    assert read_asin(untagged_m4b) == (ASIN, MP4_ATOM)
