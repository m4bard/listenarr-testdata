"""The ffprobe differential-equivalence check — the gate for swapping/updating the ffmpeg source.

Two safety properties matter, and both are contracts: it must FLAG a change in any field Listenarr
actually reads (or an update could silently change behaviour), and it must IGNORE differences in the
rest of ffprobe's output (build strings, encoder tags, demuxer lists) so it doesn't cry wolf. These
run offline by feeding canned ffprobe JSON through the extraction + compare logic.

The stakes are higher than a normal tool test, because this tool's green result has already been
published upstream as the evidence that a given ffmpeg source is a behaviour-safe replacement on
every platform Listenarr ships. A false "no differences" does not merely let a bad build through;
it retroactively makes that published claim untrue, and nothing else in the repository would
notice. So the weight here is on the failing direction: every field, every mode, and every way a
run can end up comparing less than it appears to.

Most of it runs offline against canned payloads. A handful of tests drive a real ffprobe over the
committed fixture corpus, and skip when no ffprobe is on PATH rather than passing silently.
"""
from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import sys
from typing import Any

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import ffprobe_equivalence as eq

FIXTURE_CORPUS = ROOT / "tests" / "fixtures" / "eqcorpus"

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="ffmpeg is required to build a comparison corpus"
)
needs_ffprobe = pytest.mark.skipif(
    shutil.which("ffprobe") is None, reason="ffprobe is required to probe the fixture corpus"
)

# main() runs `<candidate> -version` purely for the banner and ignores the result, so once `probe`
# is faked any executable stands in for an ffprobe build.
STAND_IN_BINARY = sys.executable


def test_functional_view_takes_exactly_the_fields_listenarr_reads() -> None:
    probe_json = {
        # size / nb_streams are outside the read set and must be ignored.
        "format": {"duration": "60.0", "format_name": "mov,mp4,m4a", "bit_rate": "128000",
                   "tags": {"title": "She"}, "size": "999", "nb_streams": 1},
        "streams": [
            {"codec_type": "video", "codec_name": "mjpeg"},  # cover art stream — must be ignored
            {"codec_type": "audio", "sample_rate": "22050", "channels": 1, "bit_rate": "128000",
             "codec_name": "aac", "tags": {"language": "eng"}, "profile": "LC"},  # profile ignored
        ],
    }
    view = eq.functional_view(probe_json)
    assert view == {
        "format.duration": "60.0", "format.format_name": "mov,mp4,m4a",
        "format.bit_rate": "128000", "format.tags": {"title": "She"},
        "stream.sample_rate": "22050", "stream.channels": 1, "stream.bit_rate": "128000",
        "stream.codec_name": "aac", "stream.tags": {"language": "eng"},
    }


@pytest.mark.contract
class TestEquivalenceContract:
    def _fake_probe(self, monkeypatch: pytest.MonkeyPatch,
                    by_binary: dict[str, dict]) -> None:
        def fake(ffprobe: str, file: pathlib.Path) -> dict:
            return by_binary[ffprobe]
        monkeypatch.setattr(eq, "probe", fake)

    def test_identical_functional_fields_are_equivalent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        j = {"format": {"duration": "1.0", "tags": {"title": "x"}},
             "streams": [{"codec_type": "audio", "codec_name": "aac"}]}
        self._fake_probe(monkeypatch, {"A": j, "B": json_copy(j)})
        assert eq.compare("A", "B", [pathlib.Path("s.m4b")]) == []

    def test_a_change_in_a_read_field_is_flagged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        base = {"format": {"duration": "1.0"},
                "streams": [{"codec_type": "audio", "codec_name": "aac"}]}
        cand = {"format": {"duration": "1.0"},
                "streams": [{"codec_type": "audio", "codec_name": "mp3"}]}
        self._fake_probe(monkeypatch, {"A": base, "B": cand})
        diffs = eq.compare("A", "B", [pathlib.Path("s.m4b")])
        assert len(diffs) == 1
        assert diffs[0].field == "stream.codec_name"
        assert (diffs[0].baseline, diffs[0].candidate) == ("aac", "mp3")

    def test_noise_outside_the_read_fields_is_ignored(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Two builds' outputs differ in fields Listenarr never reads (size, nb_streams, start_pts,
        # start_time). Flagging these would block every equivalent update, so they must be ignored.
        base = {"format": {"duration": "1.0", "size": "1000", "nb_streams": 1},
                "streams": [{"codec_type": "audio", "codec_name": "aac", "start_pts": 0,
                             "start_time": "0.000000"}]}
        cand = {"format": {"duration": "1.0", "size": "1050", "nb_streams": 1},
                "streams": [{"codec_type": "audio", "codec_name": "aac", "start_pts": 1024,
                             "start_time": "0.046440"}]}
        self._fake_probe(monkeypatch, {"A": base, "B": cand})
        assert eq.compare("A", "B", [pathlib.Path("s.m4b")]) == []


@pytest.mark.contract
class TestGoldenMode:
    """The mode macOS/Windows runners use: compare a build against a committed golden, no local
    baseline binary. Must flag a change in a read field and stay quiet when it reproduces golden."""

    def test_reproducing_golden_is_clean(self, monkeypatch: pytest.MonkeyPatch) -> None:
        view = {"format.duration": "1.0", "stream.codec_name": "aac"}
        monkeypatch.setattr(eq, "probe", lambda ff, f: {})
        monkeypatch.setattr(eq, "functional_view", lambda j: view)
        golden = {"s.m4b": view}
        assert eq.compare_to_golden("X", [pathlib.Path("s.m4b")], golden) == []

    def test_a_deviation_from_golden_is_flagged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(eq, "probe", lambda ff, f: {})
        monkeypatch.setattr(eq, "functional_view", lambda j: {"stream.codec_name": "mp3"})
        golden = {"s.m4b": {"stream.codec_name": "aac"}}
        diffs = eq.compare_to_golden("X", [pathlib.Path("s.m4b")], golden)
        assert len(diffs) == 1 and diffs[0].field == "stream.codec_name"

    def test_emit_produces_a_view_per_file(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(eq, "probe", lambda ff, f: {})
        monkeypatch.setattr(eq, "functional_view", lambda j: {"format.duration": "1.0"})
        views = eq.emit_views("X", [pathlib.Path("a.m4b"), pathlib.Path("b.mp3")])
        assert set(views) == {"a.m4b", "b.mp3"}


def json_copy(obj: dict) -> dict:
    copied: dict = json.loads(json.dumps(obj))
    return copied


# --------------------------------------------------------------------------------------
# Shared payloads and helpers for the rest of the file.
# --------------------------------------------------------------------------------------

# A tagged m4b as ffprobe describes it, carrying fields on BOTH sides of the read set so a
# comparison that quietly widened or narrowed its view shows up here.
BASE_FORMAT: dict[str, Any] = {
    "duration": "3600.000000",
    "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
    "bit_rate": "64000",
    "tags": {"title": "Ayesha The Return of She", "artist": "H. Rider Haggard", "album": "She"},
    "size": "28800000",
    "nb_streams": 1,
}
BASE_STREAM: dict[str, Any] = {
    "codec_type": "audio",
    "sample_rate": "22050",
    "channels": 1,
    "bit_rate": "63000",
    "codec_name": "aac",
    "tags": {"language": "eng", "handler_name": "SoundHandler"},
    "start_pts": 0,
    "profile": "LC",
}

# One row per field the tool compares: where to change it, what to change it to, and the name the
# difference must be reported under. The test below asserts this table covers the whole view, so a
# field added to functional_view without a case here fails rather than going unchecked.
READ_FIELDS: list[tuple[str, str, Any, str]] = [
    ("format", "duration", "3599.000000", "format.duration"),
    ("format", "format_name", "matroska,webm", "format.format_name"),
    ("format", "bit_rate", "64001", "format.bit_rate"),
    ("format", "tags", {"title": "Something Else"}, "format.tags"),
    ("stream", "sample_rate", "44100", "stream.sample_rate"),
    ("stream", "channels", 2, "stream.channels"),
    ("stream", "bit_rate", "63001", "stream.bit_rate"),
    ("stream", "codec_name", "mp3", "stream.codec_name"),
    ("stream", "tags", {"language": "deu"}, "stream.tags"),
]


def probe_payload(section: str | None = None, key: str = "", value: Any = None) -> dict[str, Any]:
    """A whole ffprobe payload, optionally with a single field changed."""
    fmt: dict[str, Any] = {**BASE_FORMAT}
    stream: dict[str, Any] = {**BASE_STREAM}
    if section == "format":
        fmt[key] = value
    elif section == "stream":
        stream[key] = value
    return {"format": fmt, "streams": [stream]}


def probe_by_binary(monkeypatch: pytest.MonkeyPatch, answers: dict[str, dict[str, Any]]) -> None:
    """Each ffprobe build answers with its own payload, whatever the file."""
    monkeypatch.setattr(eq, "probe", lambda ffprobe, file: answers[ffprobe])


def probe_by_file(monkeypatch: pytest.MonkeyPatch, answers: dict[str, dict[str, Any]]) -> None:
    """Every build agrees, and each file gets its own payload."""
    monkeypatch.setattr(eq, "probe", lambda ffprobe, file: answers[file.name])


def make_corpus(root: pathlib.Path, *names: str) -> pathlib.Path:
    """A corpus directory. Contents are irrelevant when `probe` is faked; the names are not."""
    root.mkdir(parents=True, exist_ok=True)
    for name in names:
        (root / name).write_bytes(b"")
    return root


def write_golden(path: pathlib.Path, views: dict[str, dict[str, Any]]) -> pathlib.Path:
    path.write_text(json.dumps(views, indent=2, sort_keys=True))
    return path


def run_main(monkeypatch: pytest.MonkeyPatch, *args: str) -> int:
    monkeypatch.setattr(sys, "argv", ["ffprobe_equivalence.py", *args])
    return eq.main()


@pytest.mark.contract
class TestEveryFieldListenarrReadsIsDiffed:
    """Contract: a change in ANY compared field is reported, in both comparison modes.

    The existing coverage proved one field (codec_name) is diffed. That leaves the possibility
    that some other field is extracted but never actually compared — a wrong duration or a lost
    tag block would sail through, and the run would still print EQUIVALENT. These walk every
    field in the read set, one at a time, through both the two-build path used on Linux and the
    golden path used on macOS and Windows.
    """

    @pytest.mark.parametrize(
        ("section", "key", "changed", "field"), READ_FIELDS, ids=[row[3] for row in READ_FIELDS]
    )
    def test_a_change_in_one_read_field_is_reported_by_both_comparison_paths(
        self, monkeypatch: pytest.MonkeyPatch, section: str, key: str, changed: Any, field: str
    ) -> None:
        baseline_json = probe_payload()
        candidate_json = probe_payload(section, key, changed)
        probe_by_binary(monkeypatch, {"base": baseline_json, "cand": candidate_json})

        two_build = eq.compare("base", "cand", [pathlib.Path("sample.m4b")])
        assert [d.field for d in two_build] == [field]
        assert two_build[0].candidate == changed

        golden = {"sample.m4b": eq.functional_view(baseline_json)}
        against_golden = eq.compare_to_golden("cand", [pathlib.Path("sample.m4b")], golden)
        assert [d.field for d in against_golden] == [field]
        assert against_golden[0].candidate == changed

    def test_the_table_above_covers_every_field_the_tool_compares(self) -> None:
        """A field added to the view but not to READ_FIELDS would go untested and unnoticed."""
        assert {row[3] for row in READ_FIELDS} == set(eq.functional_view(probe_payload()))

    def test_a_field_dropped_entirely_by_the_candidate_is_reported(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Losing a field is a change too. A build that stops reporting bit_rate at all reads as
        None, which must not be waved through as 'nothing to compare'."""
        without = probe_payload()
        del without["format"]["bit_rate"]
        probe_by_binary(monkeypatch, {"base": probe_payload(), "cand": without})
        diffs = eq.compare("base", "cand", [pathlib.Path("sample.m4b")])
        assert [d.field for d in diffs] == ["format.bit_rate"]
        assert diffs[0].candidate is None

    def test_a_field_the_golden_never_recorded_is_still_compared(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A golden written before a field joined the read set has no entry for it. The candidate's
        value must still be checked against that absence rather than skipped as unknown."""
        golden = {"sample.m4b": {"format.duration": "3600.000000"}}
        monkeypatch.setattr(eq, "probe", lambda ffprobe, file: probe_payload())
        diffs = eq.compare_to_golden("cand", [pathlib.Path("sample.m4b")], golden)
        assert {d.field for d in diffs} == set(eq.functional_view(probe_payload())) - {
            "format.duration"
        }

    def test_a_number_reported_as_a_string_is_a_difference(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ffprobe's JSON types are part of the answer: Listenarr parses `channels` as a number and
        `sample_rate` as text. A build that changed one into the other would break the mapper, so
        the comparison must not coerce the two into agreement."""
        probe_by_binary(monkeypatch, {
            "base": probe_payload(),
            "cand": probe_payload("stream", "channels", "1"),
        })
        diffs = eq.compare("base", "cand", [pathlib.Path("sample.m4b")])
        assert [(d.field, d.baseline, d.candidate) for d in diffs] == [("stream.channels", 1, "1")]


@pytest.mark.contract
class TestAFileTheGoldenDoesNotMentionIsReported:
    """Contract: a corpus file with no golden entry is a difference, never a quiet skip.

    Skipping is how a check stops checking. If the corpus gains a format the golden predates —
    or a golden is regenerated from a partial run — the honest outcome is a loud unknown, because
    a file nobody has a reference value for has not been validated by anything.
    """

    def test_an_unknown_file_is_reported_rather_than_passed_over(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(eq, "probe", lambda ffprobe, file: probe_payload())
        diffs = eq.compare_to_golden("cand", [pathlib.Path("sample.opus")], {})
        assert [d.field for d in diffs] == ["<not-in-golden>"]
        assert diffs[0].file == "sample.opus"

    def test_the_other_files_are_still_checked_after_an_unknown_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unknown file must not swallow the rest of the run — the real difference is in the
        file the golden DOES know about, and it still has to be found."""
        probe_by_file(monkeypatch, {
            "sample.m4b": probe_payload("stream", "codec_name", "mp3"),
            "zz-unknown.opus": probe_payload(),
        })
        golden = {"sample.m4b": eq.functional_view(probe_payload())}
        files = [pathlib.Path("zz-unknown.opus"), pathlib.Path("sample.m4b")]
        diffs = eq.compare_to_golden("cand", files, golden)
        assert {d.field for d in diffs} == {"<not-in-golden>", "stream.codec_name"}

    def test_an_unknown_file_makes_the_whole_run_exit_nonzero(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        corpus = make_corpus(tmp_path / "corpus", "sample.m4b", "sample.opus")
        monkeypatch.setattr(eq, "probe", lambda ffprobe, file: probe_payload())
        golden = write_golden(tmp_path / "golden.json",
                              {"sample.m4b": eq.functional_view(probe_payload())})
        assert run_main(monkeypatch, "--candidate", STAND_IN_BINARY,
                        "--corpus", str(corpus), "--golden", str(golden)) == 1


@pytest.mark.contract
class TestEmitAndCompareAgree:
    """Contract: a golden this tool emits is one this tool then accepts.

    The two halves run on different machines and are joined only by a JSON file, so any drift
    between them — a key built from a different part of the path, a value that does not survive
    serialisation — shows up as a permanent, unexplainable difference on the platforms that use
    the golden. Worse, someone would then 'fix' it by regenerating the golden, which is how a
    real divergence gets baked in as the new reference.
    """

    def test_a_golden_it_emitted_is_one_it_then_accepts(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        corpus = make_corpus(tmp_path / "corpus", "sample.m4b", "sample.mp3")
        probe_by_file(monkeypatch, {
            "sample.m4b": probe_payload(),
            "sample.mp3": probe_payload("stream", "codec_name", "mp3"),
        })
        golden = tmp_path / "golden.json"
        assert run_main(monkeypatch, "--candidate", STAND_IN_BINARY,
                        "--corpus", str(corpus), "--emit", str(golden)) == 0
        assert run_main(monkeypatch, "--candidate", STAND_IN_BINARY,
                        "--corpus", str(corpus), "--golden", str(golden)) == 0

    def test_the_emitted_golden_is_keyed_by_the_names_the_comparison_looks_up(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        """Keys are bare file names on both sides. A key holding any part of the directory would
        make every file 'not in golden' the moment the corpus moved."""
        corpus = make_corpus(tmp_path / "corpus", "sample.m4b", "sample.mp3")
        probe_by_file(monkeypatch, {"sample.m4b": probe_payload(), "sample.mp3": probe_payload()})
        golden = tmp_path / "golden.json"
        run_main(monkeypatch, "--candidate", STAND_IN_BINARY,
                 "--corpus", str(corpus), "--emit", str(golden))
        written = json.loads(golden.read_text())
        assert set(written) == {"sample.m4b", "sample.mp3"}
        assert set(written["sample.m4b"]) == set(eq.functional_view(probe_payload()))

    def test_one_altered_value_in_that_golden_is_then_caught(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        """The round trip has to be tight, not merely symmetrical: emitting and re-reading must
        not launder a real difference away."""
        corpus = make_corpus(tmp_path / "corpus", "sample.m4b")
        probe_by_file(monkeypatch, {"sample.m4b": probe_payload()})
        golden = tmp_path / "golden.json"
        run_main(monkeypatch, "--candidate", STAND_IN_BINARY,
                 "--corpus", str(corpus), "--emit", str(golden))
        views = json.loads(golden.read_text())
        views["sample.m4b"]["format.duration"] = "3599.000000"
        write_golden(golden, views)
        assert run_main(monkeypatch, "--candidate", STAND_IN_BINARY,
                        "--corpus", str(corpus), "--golden", str(golden)) == 1


@pytest.mark.contract
class TestExitCodesCarryTheVerdict:
    """Contract: the exit code is the whole signal CI reads.

    Every consumer of this tool is a workflow step that looks at nothing but the status. A run
    that prints DIFFERENCES and exits 0 is a green gate on a divergent build, which is precisely
    the outcome the published cross-platform claim rests on not happening.
    """

    def test_an_identical_candidate_exits_zero_in_golden_mode(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        corpus = make_corpus(tmp_path / "corpus", "sample.m4b")
        probe_by_file(monkeypatch, {"sample.m4b": probe_payload()})
        golden = write_golden(tmp_path / "golden.json",
                              {"sample.m4b": eq.functional_view(probe_payload())})
        assert run_main(monkeypatch, "--candidate", STAND_IN_BINARY,
                        "--corpus", str(corpus), "--golden", str(golden)) == 0

    def test_a_single_differing_field_exits_nonzero_in_golden_mode(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        corpus = make_corpus(tmp_path / "corpus", "sample.m4b")
        probe_by_file(monkeypatch, {"sample.m4b": probe_payload("stream", "codec_name", "mp3")})
        golden = write_golden(tmp_path / "golden.json",
                              {"sample.m4b": eq.functional_view(probe_payload())})
        assert run_main(monkeypatch, "--candidate", STAND_IN_BINARY,
                        "--corpus", str(corpus), "--golden", str(golden)) == 1

    def test_two_identical_builds_exit_zero_in_baseline_mode(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        corpus = make_corpus(tmp_path / "corpus", "sample.m4b")
        monkeypatch.setattr(eq, "probe", lambda ffprobe, file: probe_payload())
        assert run_main(monkeypatch, "--candidate", STAND_IN_BINARY,
                        "--baseline", STAND_IN_BINARY, "--corpus", str(corpus)) == 0

    def test_a_differing_candidate_exits_nonzero_in_baseline_mode(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        corpus = make_corpus(tmp_path / "corpus", "sample.m4b")
        probe_by_binary(monkeypatch, {
            "/opt/baseline/ffprobe": probe_payload(),
            "/opt/candidate/ffprobe": probe_payload("format", "duration", "3599.000000"),
        })
        # Neither path exists, and main() only runs them to print a version banner it ignores.
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
            list(a[0]), 0, "ffprobe version 7.1", ""))
        assert run_main(monkeypatch, "--candidate", "/opt/candidate/ffprobe",
                        "--baseline", "/opt/baseline/ffprobe", "--corpus", str(corpus)) == 1

    def test_the_report_names_the_file_the_field_and_both_values(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A difference an operator cannot locate is barely a difference report, and the first
        thing anyone does with a red gate is decide whether it is real."""
        corpus = make_corpus(tmp_path / "corpus", "sample.m4b")
        probe_by_file(monkeypatch, {"sample.m4b": probe_payload("stream", "codec_name", "mp3")})
        golden = write_golden(tmp_path / "golden.json",
                              {"sample.m4b": eq.functional_view(probe_payload())})
        run_main(monkeypatch, "--candidate", STAND_IN_BINARY,
                 "--corpus", str(corpus), "--golden", str(golden))
        printed = capsys.readouterr().out
        assert "DIFFERENCES" in printed and "EQUIVALENT" not in printed
        assert "sample.m4b" in printed and "stream.codec_name" in printed
        assert "'aac'" in printed and "'mp3'" in printed


class TestArgumentValidation:
    def test_no_mode_at_all_is_refused(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        """A candidate and nothing to compare it to has no verdict to give. Refusing is the only
        answer that cannot be mistaken for equivalence."""
        corpus = make_corpus(tmp_path / "corpus", "sample.m4b")
        with pytest.raises(SystemExit) as exit_info:
            run_main(monkeypatch, "--candidate", STAND_IN_BINARY, "--corpus", str(corpus))
        assert exit_info.value.code != 0

    def test_no_candidate_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        with pytest.raises(SystemExit) as exit_info:
            run_main(monkeypatch, "--golden", "golden.json")
        assert exit_info.value.code != 0

    def test_golden_wins_when_a_baseline_is_also_given(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Characterisation: the modes are documented as mutually exclusive but not enforced as
        such. Pinned so that a run asking for two comparisons and silently getting one is a known,
        visible behaviour rather than a surprise in a workflow file."""
        corpus = make_corpus(tmp_path / "corpus", "sample.m4b")
        probe_by_file(monkeypatch, {"sample.m4b": probe_payload()})
        golden = write_golden(tmp_path / "golden.json",
                              {"sample.m4b": eq.functional_view(probe_payload())})
        assert run_main(monkeypatch, "--candidate", STAND_IN_BINARY, "--baseline", "/nonexistent",
                        "--corpus", str(corpus), "--golden", str(golden)) == 0
        assert "vs golden" in capsys.readouterr().out

    def test_emit_wins_over_a_comparison_and_writes_the_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        """--emit is documented as 'write the view and exit', so a golden passed alongside it is
        not compared. Pinned so that an emit run can never be read as a passing comparison."""
        corpus = make_corpus(tmp_path / "corpus", "sample.m4b")
        probe_by_file(monkeypatch, {"sample.m4b": probe_payload("stream", "codec_name", "mp3")})
        golden = write_golden(tmp_path / "golden.json",
                              {"sample.m4b": eq.functional_view(probe_payload())})
        emitted = tmp_path / "emitted.json"
        assert run_main(monkeypatch, "--candidate", STAND_IN_BINARY, "--corpus", str(corpus),
                        "--golden", str(golden), "--emit", str(emitted)) == 0
        assert json.loads(emitted.read_text())["sample.m4b"]["stream.codec_name"] == "mp3"


@pytest.mark.contract
class TestProbeFailsLoudly:
    """Contract: a build that cannot read a file raises, and asks exactly Listenarr's question.

    Both properties feed the same false-identical risk from different directions. A probe failure
    swallowed into an empty result would produce an all-None view, and two all-None views compare
    equal. And an invocation that has drifted from the one in FfmpegService.Probing.cs would be
    measuring the equivalence of a command Listenarr never runs.
    """

    def _record(self, monkeypatch: pytest.MonkeyPatch, returncode: int, stdout: str,
                stderr: str = "") -> list[tuple[list[str], dict[str, Any]]]:
        calls: list[tuple[list[str], dict[str, Any]]] = []

        def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            calls.append((cmd, kwargs))
            return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)

        monkeypatch.setattr(subprocess, "run", fake_run)
        return calls

    def test_a_nonzero_exit_raises_instead_of_returning_an_empty_view(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._record(monkeypatch, 1, "", "Invalid data found when processing input")
        with pytest.raises(eq.ffprobeError) as exc:
            eq.probe("/opt/candidate/ffprobe", pathlib.Path("/corpus/sample.opus"))
        assert "sample.opus" in str(exc.value)
        assert "Invalid data" in str(exc.value)

    def test_it_runs_exactly_the_command_listenarr_runs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = self._record(monkeypatch, 0, "{}")
        eq.probe("/opt/candidate/ffprobe", pathlib.Path("/corpus/sample.m4b"))
        cmd, _ = calls[0]
        assert cmd == ["/opt/candidate/ffprobe", *eq.LISTENARR_ARGS, "/corpus/sample.m4b"]
        assert eq.LISTENARR_ARGS == ["-v", "quiet", "-print_format", "json",
                                     "-show_format", "-show_streams"]

    def test_the_decode_is_pinned_to_utf8(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A Windows runner defaults to cp1252, which mangles non-ASCII tag values into
        differences that are an artefact of the runner rather than of the build."""
        calls = self._record(monkeypatch, 0, "{}")
        eq.probe("ffprobe", pathlib.Path("sample.m4b"))
        assert calls[0][1]["encoding"] == "utf-8"

    def test_a_silent_success_produces_an_all_none_view(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Characterisation of a hole worth knowing about: exit 0 with no output is read as an
        empty document rather than as a failure, and two builds that both do it compare equal.
        Golden mode catches it (all-None differs from a recorded view); two-build mode does not."""
        self._record(monkeypatch, 0, "")
        view = eq.functional_view(eq.probe("ffprobe", pathlib.Path("sample.m4b")))
        assert set(view.values()) == {None}


class TestWhichFilesGetCompared:
    def test_a_corpus_directory_contributes_its_files_in_a_stable_order(
        self, tmp_path: pathlib.Path
    ) -> None:
        corpus = make_corpus(tmp_path / "corpus", "sample.wav", "sample.m4b", "sample.mp3")
        assert [p.name for p in eq._corpus_files(corpus, [])] == [
            "sample.m4b", "sample.mp3", "sample.wav"
        ]

    def test_a_subdirectory_is_not_mistaken_for_a_file(self, tmp_path: pathlib.Path) -> None:
        """ffprobe would fail on a directory, which turns a stray folder into a crash rather than
        a verdict."""
        corpus = make_corpus(tmp_path / "corpus", "sample.m4b")
        (corpus / "nested").mkdir()
        assert [p.name for p in eq._corpus_files(corpus, [])] == ["sample.m4b"]

    def test_without_a_corpus_the_generated_files_are_used(self, tmp_path: pathlib.Path) -> None:
        generated = [tmp_path / "sample.wav", tmp_path / "sample.mp3"]
        assert eq._corpus_files(None, generated) == generated


class TestBuildingTheDefaultCorpus:
    def test_it_refuses_rather_than_comparing_nothing_when_ffmpeg_is_missing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        """Without ffmpeg there is no corpus, and a corpus of zero files compares clean. Raising
        is what keeps that from reading as equivalence."""
        monkeypatch.setattr(shutil, "which", lambda name: None)
        with pytest.raises(eq.ffprobeError, match="ffmpeg is required"):
            eq.build_corpus(tmp_path / "corpus")

    @needs_ffmpeg
    def test_it_produces_a_playable_file_per_format_the_host_can_encode(
        self, tmp_path: pathlib.Path
    ) -> None:
        files = eq.build_corpus(tmp_path / "corpus")
        assert files, "the host ffmpeg encoded nothing at all"
        assert {p.suffix.lstrip(".") for p in files} <= set(eq.FORMAT_RECIPES)
        assert all(p.exists() and p.stat().st_size > 0 for p in files)
        # pcm_s16le is in every ffmpeg build, so its absence means the recipe itself broke.
        assert any(p.suffix == ".wav" for p in files)

    @needs_ffmpeg
    def test_a_codec_the_host_lacks_is_skipped_not_failed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        """Both builds read the same files, so host codec availability cannot bias the diff — but
        only if a missing encoder drops the format instead of aborting the run."""
        monkeypatch.setattr(eq, "FORMAT_RECIPES", {
            "wav": ["-c:a", "pcm_s16le"],
            "xyz": ["-c:a", "no_such_encoder"],
        })
        files = eq.build_corpus(tmp_path / "corpus")
        assert [p.name for p in files] == ["sample.wav"]

    @needs_ffmpeg
    @needs_ffprobe
    def test_the_generated_files_carry_the_tags_the_comparison_is_meant_to_exercise(
        self, tmp_path: pathlib.Path
    ) -> None:
        """format.tags and stream.tags are two of the nine compared fields. An untagged corpus
        would compare them as None on both sides and prove nothing about the metadata path."""
        ffprobe = shutil.which("ffprobe")
        assert ffprobe is not None
        files = eq.build_corpus(tmp_path / "corpus")
        flac = next(p for p in files if p.suffix == ".flac")
        tags = eq.functional_view(eq.probe(ffprobe, flac))["format.tags"]
        assert tags["title"] == "Ayesha The Return of She"
        assert tags["artist"] == "H. Rider Haggard"


@needs_ffprobe
@pytest.mark.contract
class TestAgainstARealFfprobe:
    """The same contract, with the fakes taken away.

    Everything above replaces `probe`, so it proves the comparison logic and nothing about the
    subprocess that feeds it. These drive a real binary over the committed fixture corpus. They
    deliberately compare a build against its OWN emitted golden rather than against the checked-in
    one: the host ffprobe is whatever the machine happens to have, so demanding it reproduce the
    committed reference would be testing the machine, not the tool.
    """

    def test_a_build_reproduces_the_golden_it_just_emitted(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        ffprobe = shutil.which("ffprobe")
        assert ffprobe is not None
        golden = tmp_path / "golden.json"
        assert run_main(monkeypatch, "--candidate", ffprobe,
                        "--corpus", str(FIXTURE_CORPUS), "--emit", str(golden)) == 0
        views = json.loads(golden.read_text())
        assert set(views) == {p.name for p in FIXTURE_CORPUS.iterdir() if p.is_file()}
        assert run_main(monkeypatch, "--candidate", ffprobe,
                        "--corpus", str(FIXTURE_CORPUS), "--golden", str(golden)) == 0

    def test_one_edited_value_in_that_golden_makes_the_real_run_fail(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A golden the tool cannot fail against is not a check. Editing one duration stands in
        for the divergence a future ffmpeg release would introduce."""
        ffprobe = shutil.which("ffprobe")
        assert ffprobe is not None
        golden = tmp_path / "golden.json"
        run_main(monkeypatch, "--candidate", ffprobe,
                 "--corpus", str(FIXTURE_CORPUS), "--emit", str(golden))
        views = json.loads(golden.read_text())
        views["sample.wav"]["format.duration"] = "999.000000"
        write_golden(golden, views)
        capsys.readouterr()
        assert run_main(monkeypatch, "--candidate", ffprobe,
                        "--corpus", str(FIXTURE_CORPUS), "--golden", str(golden)) == 1
        assert "sample.wav" in capsys.readouterr().out

    @needs_ffmpeg
    def test_the_default_mode_builds_its_own_corpus_and_cleans_up_after_itself(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The invocation in the module docstring: two builds, no --corpus, so the tool generates
        one itself. Run here with a single real binary on both sides, which must come out equal —
        if that ever reported a difference, every genuine comparison would be noise. The temp
        directory it builds into is checked afterwards because a CI runner does hundreds of these.
        """
        ffprobe = shutil.which("ffprobe")
        assert ffprobe is not None
        monkeypatch.setattr(eq, "FORMAT_RECIPES", {"wav": ["-c:a", "pcm_s16le"],
                                                   "flac": ["-c:a", "flac"]})
        built_into: list[pathlib.Path] = []
        real_build = eq.build_corpus

        def spy(out_dir: pathlib.Path) -> list[pathlib.Path]:
            built_into.append(out_dir)
            return real_build(out_dir)

        monkeypatch.setattr(eq, "build_corpus", spy)
        assert run_main(monkeypatch, "--candidate", ffprobe, "--baseline", ffprobe) == 0
        assert built_into and not built_into[0].exists()

    def test_the_fixture_corpus_still_covers_every_format_the_recipes_build(self) -> None:
        """The committed corpus is what CI compares. If it lost a format, the workflow would keep
        passing while silently validating less than it claims to."""
        present = {p.suffix.lstrip(".") for p in FIXTURE_CORPUS.iterdir() if p.is_file()}
        assert present == set(eq.FORMAT_RECIPES)


@pytest.mark.contract
class TestTheCheckCannotStopCheckingSilently:
    """Two ways a run could once report EQUIVALENT having compared less than it appeared to.

    Neither was hypothetical for the workflow that consumes this: it passes --corpus and --golden
    as two independent paths, and nothing ties the contents of one to the other. Since this tool's
    green result is the evidence behind a public claim about cross-platform behaviour, a run that
    compared nothing had to stop being indistinguishable from a run that compared everything.
    Both now refuse.
    """

    def test_an_empty_corpus_is_refused_rather_than_called_equivalent(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A corpus directory that exists but holds nothing: a checkout that did not fetch it, a
        path typo, a cleanup step that ran early. Comparing zero files once counted as
        equivalence, and the golden's own entries were never consulted, so the size of what had
        been skipped was invisible."""
        empty = make_corpus(tmp_path / "corpus")
        golden = write_golden(tmp_path / "golden.json",
                              {"sample.m4b": eq.functional_view(probe_payload())})
        assert run_main(monkeypatch, "--candidate", STAND_IN_BINARY,
                        "--corpus", str(empty), "--golden", str(golden)) == 2
        assert "EQUIVALENT" not in capsys.readouterr().out

    def test_a_golden_entry_with_no_matching_file_is_reported(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        """The mirror image of the unknown-file case, and the one that used to go uncaught. The
        run walks the corpus, so a golden entry with nothing to compare against was skipped in
        silence: a corpus that lost seven of its eight formats still exited zero on the one that
        was left, even where the build genuinely diverged on the other seven."""
        corpus = make_corpus(tmp_path / "corpus", "sample.m4b")
        probe_by_file(monkeypatch, {"sample.m4b": probe_payload()})
        golden = write_golden(tmp_path / "golden.json", {
            "sample.m4b": eq.functional_view(probe_payload()),
            # A format the candidate would fail on, if it were ever asked about it.
            "sample.opus": eq.functional_view(probe_payload("stream", "codec_name", "opus")),
        })
        assert run_main(monkeypatch, "--candidate", STAND_IN_BINARY,
                        "--corpus", str(corpus), "--golden", str(golden)) == 1
