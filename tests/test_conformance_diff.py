"""The A/B conformance diff: what changed between two runs, and the gate on it.

The value of a diff tool is entirely in one property — it must be able to make a run that
regressed a case FAIL, even when the head build is still "mostly green". These pin that property
(the #15 verdict-contract category) alongside the classification itself.

The failure mode worth spending tests on is not a crash. A crash gets noticed. The damaging one
is a diff that reports nothing and exits zero over a run that did break something, because that
is indistinguishable from the ordinary happy case that this tool exists to produce hundreds of
times a week. So most of what follows constructs a state where the gate MUST refuse, and checks
the refusal — the printed line, the JSON verdict, and the exit code, which are three separate
things a reader might key on and three separate chances to lie.
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

from conformance_diff import Diff, DiffError, diff_reports, main, print_diff


def report(*results: tuple[str, str]) -> dict:
    """A minimal verify_scan-shaped JSON report from (path, verdict) pairs."""
    return {
        "summary": {"overall": "fail" if any(v != "pass" for _, v in results) else "pass"},
        "results": [{"path": p, "verdict": v, "case": "-", "why": ""} for p, v in results],
    }


def inconclusive(reason: str = "source error: schema moved") -> dict:
    """What verify_scan actually writes when it could not look.

    Note the absence of a `results` key — that is faithful to `emit_inconclusive`, and it is why
    the refusal has to key on `summary.overall` rather than on the results being empty.
    """
    return {
        "summary": {"passed": 0, "failed": 0, "total": 0, "overall": "inconclusive"},
        "error": reason,
    }


def run_main(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path,
    base: dict, head: dict, *extra: str,
) -> int:
    """Drive the CLI in-process, the way test_verify_scan drives verify_scan.

    A subprocess run measures nothing: coverage cannot see into it, so the argument parsing, the
    output routing and the exit-code arithmetic all read as untested however many times they are
    exercised. TestTheProcessBoundary keeps exactly one real process around to prove the module
    entry point still wires main()'s return value to the shell.
    """
    (tmp_path / "base.json").write_text(json.dumps(base))
    (tmp_path / "head.json").write_text(json.dumps(head))
    monkeypatch.setattr(sys, "argv", [
        "conformance_diff.py", str(tmp_path / "base.json"), str(tmp_path / "head.json"), *extra,
    ])
    return main()


class TestClassification:
    def test_a_regression_is_detected(self) -> None:
        base = report(("a.m4b", "pass"), ("b.m4b", "pass"))
        head = report(("a.m4b", "pass"), ("b.m4b", "fail"))
        diff = diff_reports(base, head)
        assert [r["path"] for r in diff.regressed] == ["b.m4b"]
        assert not diff.fixed

    def test_a_fix_is_detected(self) -> None:
        base = report(("a.m4b", "fail"))
        head = report(("a.m4b", "pass"))
        diff = diff_reports(base, head)
        assert [r["path"] for r in diff.fixed] == ["a.m4b"]
        assert not diff.regressed

    def test_missing_and_fail_both_count_as_not_passing(self) -> None:
        # A pass -> missing is as much a regression as pass -> fail; the diff cares about
        # pass-ness, not the flavour of the non-pass.
        base = report(("a.m4b", "pass"))
        head = report(("a.m4b", "missing"))
        assert [r["path"] for r in diff_reports(base, head).regressed] == ["a.m4b"]

    @pytest.mark.contract
    @pytest.mark.parametrize("verdict", ["fail", "missing", "unexpected", "inconclusive", ""])
    def test_every_flavour_of_not_passing_is_a_regression_from_pass(self, verdict: str) -> None:
        """The rule is "only `pass` is a pass", and it has to hold for verdicts nobody has
        invented yet. A new verify_scan verdict string that this tool did not anticipate must
        land on the safe side of the line — counted as broken — rather than slipping through
        some allow-list of known-bad names and reading as unchanged."""
        diff = diff_reports(report(("a.m4b", "pass")), report(("a.m4b", verdict)))
        assert [r["path"] for r in diff.regressed] == ["a.m4b"]

    def test_a_non_pass_becoming_a_different_non_pass_is_not_a_change(self) -> None:
        # fail -> missing is churn, not news. Reporting it would put noise in the one section a
        # reviewer is meant to read every line of.
        diff = diff_reports(report(("a.m4b", "fail")), report(("a.m4b", "missing")))
        assert not diff.regressed and not diff.fixed

    def test_unchanged_cases_are_silent(self) -> None:
        base = report(("a.m4b", "pass"), ("b.m4b", "fail"))
        head = report(("a.m4b", "pass"), ("b.m4b", "fail"))
        diff = diff_reports(base, head)
        assert not diff.regressed and not diff.fixed

    def test_the_unchanged_majority_is_suppressed_around_the_one_case_that_moved(self) -> None:
        """The whole premise: a hundred steady cases must not bury the one that broke."""
        steady = [(f"steady-{n:03d}.m4b", "pass") for n in range(100)]
        base = report(*steady, ("b.m4b", "pass"))
        head = report(*steady, ("b.m4b", "fail"))
        diff = diff_reports(base, head)
        assert [r["path"] for r in diff.regressed] == ["b.m4b"]
        assert not diff.fixed and not diff.added and not diff.dropped

    def test_a_fix_and_a_regression_in_one_run_are_reported_separately(self) -> None:
        """The scenario the tool was written for: a PR that fixes one bug and breaks another.

        Netting these off against each other — or letting the fix colour the verdict — is how a
        breaking change gets waved through as "still green overall".
        """
        base = report(("a.m4b", "pass"), ("b.m4b", "fail"))
        head = report(("a.m4b", "fail"), ("b.m4b", "pass"))
        diff = diff_reports(base, head)
        assert [r["path"] for r in diff.regressed] == ["a.m4b"]
        assert [r["path"] for r in diff.fixed] == ["b.m4b"]

    def test_a_regressed_row_carries_the_verdict_it_used_to_have(self) -> None:
        # "was: pass" is what makes the printed line a diff rather than a second failure list.
        base = report(("a.m4b", "pass"))
        head = report(("a.m4b", "missing"))
        row = diff_reports(base, head).regressed[0]
        assert (row["was"], row["verdict"]) == ("pass", "missing")

    def test_added_and_dropped_are_tracked_separately(self) -> None:
        base = report(("a.m4b", "pass"))
        head = report(("b.m4b", "pass"))
        diff = diff_reports(base, head)
        assert [r["path"] for r in diff.added] == ["b.m4b"]
        assert [r["path"] for r in diff.dropped] == ["a.m4b"]

    @pytest.mark.contract
    def test_a_case_only_in_head_is_added_not_a_fix(self) -> None:
        """A case with no base row has nothing to have improved from. Calling a brand new
        passing case a "fix" would let a PR that adds cases claim credit for repairs it did not
        make, and, worse, a new FAILING case would then look like a regression and cry wolf."""
        diff = diff_reports(report(), report(("new.m4b", "fail")))
        assert [r["path"] for r in diff.added] == ["new.m4b"]
        assert not diff.regressed and not diff.fixed

    def test_both_sides_empty_is_a_clean_empty_diff(self) -> None:
        diff = diff_reports(report(), report())
        assert diff.to_dict()["summary"] == {
            "regressed": 0, "fixed": 0, "added": 0, "dropped": 0, "verdict": "clean",
        }

    @pytest.mark.contract
    def test_an_inconclusive_base_refuses_to_diff(self) -> None:
        # You cannot compare against a run that could not observe the scan — that must be a loud
        # refusal, not a diff that treats every case as regressed.
        with pytest.raises(DiffError, match="base report is inconclusive"):
            diff_reports(inconclusive(), report(("a.m4b", "pass")))

    @pytest.mark.contract
    def test_an_inconclusive_head_refuses_to_diff(self) -> None:
        """The docstring promises "either side", and the head side is the one that matters more:
        a head run that could not look at all has zero results, so without this check every case
        in base would come back as merely "dropped" and the gate would pass."""
        with pytest.raises(DiffError, match="head report is inconclusive"):
            diff_reports(report(("a.m4b", "pass")), inconclusive())

    def test_the_refusal_repeats_the_reason_the_run_was_inconclusive(self) -> None:
        # Whoever reads this message is looking at CI output, not at the report file.
        with pytest.raises(DiffError, match="schema moved"):
            diff_reports(inconclusive("source error: schema moved"), report())

    @pytest.mark.contract
    def test_a_report_with_no_results_key_is_refused(self) -> None:
        # verify_scan always emits `results` on a conclusive run, so a report without it is an
        # empty file, a truncated one, or something that is not a verify_scan report at all.
        # Reading it as "no cases" is what let a failing head diff come out clean.
        with pytest.raises(DiffError, match="no `results`"):
            diff_reports({"summary": {"overall": "pass"}}, report())


class TestSummary:
    """`summary.verdict` is what a script greps for, so it has to agree with the exit code."""

    @pytest.mark.contract
    def test_any_regression_makes_the_verdict_regressed(self) -> None:
        diff = diff_reports(report(("a.m4b", "pass")), report(("a.m4b", "fail")))
        assert diff.to_dict()["summary"]["verdict"] == "regressed"

    @pytest.mark.contract
    def test_a_run_of_nothing_but_fixes_stays_clean(self) -> None:
        diff = diff_reports(report(("a.m4b", "fail")), report(("a.m4b", "pass")))
        summary = diff.to_dict()["summary"]
        assert summary["verdict"] == "clean" and summary["fixed"] == 1

    def test_the_counts_match_the_lists(self) -> None:
        base = report(("regress.m4b", "pass"), ("fix.m4b", "fail"), ("gone.m4b", "pass"))
        head = report(("regress.m4b", "fail"), ("fix.m4b", "pass"), ("new.m4b", "pass"))
        payload = diff_reports(base, head).to_dict()
        assert payload["summary"] == {
            "regressed": 1, "fixed": 1, "added": 1, "dropped": 1, "verdict": "regressed",
        }
        for bucket in ("regressed", "fixed", "added", "dropped"):
            assert len(payload[bucket]) == payload["summary"][bucket]


class TestPrintedReport:
    """The text output is what a human actually reads on a PR, and it is a separate surface from
    the JSON — it has its own verdict line, and its own chance to disagree with the exit code."""

    @pytest.mark.contract
    def test_a_regression_prints_a_regressed_verdict(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        diff = diff_reports(report(("a.m4b", "pass")), report(("a.m4b", "fail")))
        print_diff(diff, "base.json", "head.json")
        printed = capsys.readouterr().out
        assert "VERDICT: regressed" in printed
        assert "REGRESSED (1):" in printed and "a.m4b" in printed

    @pytest.mark.contract
    def test_a_clean_run_says_so_in_the_verdict_line(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        diff = diff_reports(report(("a.m4b", "fail")), report(("a.m4b", "pass")))
        print_diff(diff, "base.json", "head.json")
        assert "VERDICT: clean" in capsys.readouterr().out

    def test_a_case_that_did_not_move_is_never_printed(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Suppressing the unchanged majority is the feature. A steady case appearing anywhere
        in this output, even in a section headed something harmless, is the regression."""
        base = report(("steady.m4b", "pass"), ("b.m4b", "pass"))
        head = report(("steady.m4b", "pass"), ("b.m4b", "fail"))
        print_diff(diff_reports(base, head), "base.json", "head.json")
        assert "steady.m4b" not in capsys.readouterr().out

    def test_empty_sections_are_omitted_entirely(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        print_diff(Diff(), "base.json", "head.json")
        printed = capsys.readouterr().out
        assert "REGRESSED (" not in printed and "fixed (" not in printed
        assert "summary: 0 regressed, 0 fixed, 0 added, 0 dropped" in printed

    def test_each_section_is_printed_with_its_rows(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        base = report(("regress.m4b", "pass"), ("fix.m4b", "fail"), ("gone.m4b", "pass"))
        head = report(("regress.m4b", "fail"), ("fix.m4b", "pass"), ("new.m4b", "pass"))
        print_diff(diff_reports(base, head), "base.json", "head.json")
        printed = capsys.readouterr().out
        for section in ("REGRESSED (1):", "fixed (1):", "added (head only) (1):",
                        "dropped (base only) (1):"):
            assert section in printed
        for path in ("regress.m4b", "fix.m4b", "new.m4b", "gone.m4b"):
            assert path in printed

    def test_the_reason_a_case_broke_is_printed_under_it(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A regression a reviewer has to go and re-run the harness to understand is a regression
        that gets dismissed as flaky."""
        base = {"summary": {"overall": "pass"},
                "results": [{"path": "a.m4b", "verdict": "pass", "case": "canonical", "why": ""}]}
        head = {"summary": {"overall": "fail"},
                "results": [{"path": "a.m4b", "verdict": "missing", "case": "canonical",
                             "why": "linked to no book at all"}]}
        print_diff(diff_reports(base, head), "base.json", "head.json")
        printed = capsys.readouterr().out
        assert "canonical" in printed and "linked to no book at all" in printed

    def test_a_row_without_a_case_or_reason_still_prints(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # `added` and `dropped` rows come straight from a report and carry no `was`; the
        # formatter must not throw on the fields it fills in with a dash.
        print_diff(diff_reports(report(), report(("new.m4b", "pass"))), "b", "h")
        assert "new.m4b" in capsys.readouterr().out

    def test_the_labels_name_which_file_was_which_side(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        print_diff(Diff(), "before.json", "after.json")
        assert "before.json -> after.json" in capsys.readouterr().out


@pytest.mark.contract
class TestTheGate:
    """The contract: a regression can fail the gate; a clean diff cannot.

    Everything here goes through main() so that the exit-code arithmetic, the argument parsing
    and the output routing are the code actually under test, rather than something a subprocess
    ran out of sight.
    """

    def test_strict_exits_nonzero_on_a_regression(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        assert run_main(monkeypatch, tmp_path, report(("a.m4b", "pass")),
                        report(("a.m4b", "fail")), "--strict") == 1

    def test_strict_exits_nonzero_even_when_the_head_run_is_mostly_green(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        """The false negative this tool exists to prevent. One broken case among a hundred good
        ones is still a broken case, and an overall-fail-rate check would wave it through."""
        steady = [(f"steady-{n:03d}.m4b", "pass") for n in range(99)]
        assert run_main(monkeypatch, tmp_path,
                        report(*steady, ("b.m4b", "pass")),
                        report(*steady, ("b.m4b", "fail")), "--strict") == 1

    def test_strict_exits_nonzero_when_a_regression_is_offset_by_a_fix(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        """Fixing one case does not buy you the right to break another."""
        assert run_main(monkeypatch, tmp_path,
                        report(("a.m4b", "pass"), ("b.m4b", "fail")),
                        report(("a.m4b", "fail"), ("b.m4b", "pass")), "--strict") == 1

    def test_strict_exits_zero_when_only_fixes(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        # A PR that only fixes things must not be blocked by --strict.
        assert run_main(monkeypatch, tmp_path, report(("a.m4b", "fail")),
                        report(("a.m4b", "pass")), "--strict") == 0

    def test_strict_exits_zero_when_nothing_moved(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        steady = report(("a.m4b", "pass"), ("b.m4b", "fail"))
        assert run_main(monkeypatch, tmp_path, steady, steady, "--strict") == 0

    def test_strict_exits_zero_for_a_new_case_that_fails_from_the_start(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        """A case added by the PR itself, failing on its first run, is a known-broken scenario
        being documented — not something this PR regressed. It is reported under `added`."""
        assert run_main(monkeypatch, tmp_path, report(), report(("new.m4b", "fail")),
                        "--strict") == 0

    def test_a_regression_without_strict_still_exits_zero(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        """Reporting and gating are deliberately separate: without --strict this is a viewer,
        and a caller that wants the gate has to ask for it."""
        assert run_main(monkeypatch, tmp_path, report(("a.m4b", "pass")),
                        report(("a.m4b", "fail"))) == 0

    def test_an_inconclusive_base_exits_two_and_says_why_on_stderr(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """2, not 1 and not 0: a caller has to be able to tell "could not compare" from both
        "compared, and it regressed" and "compared, and it was fine"."""
        assert run_main(monkeypatch, tmp_path, inconclusive(),
                        report(("a.m4b", "pass")), "--strict") == 2
        assert "CANNOT DIFF" in capsys.readouterr().err

    def test_an_inconclusive_head_exits_two(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        assert run_main(monkeypatch, tmp_path, report(("a.m4b", "pass")),
                        inconclusive(), "--strict") == 2

    def test_the_refusal_prints_no_verdict_line_that_could_be_read_as_clean(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A "VERDICT: clean" on stdout next to a 2 on the exit code is how a human skimming CI
        output concludes the run was fine."""
        run_main(monkeypatch, tmp_path, inconclusive(), report(("a.m4b", "pass")))
        assert "VERDICT" not in capsys.readouterr().out


@pytest.mark.contract
class TestMachineOutput:
    """`--json` is what a bot reads. It must never disagree with the exit code."""

    def test_the_json_verdict_reflects_a_regression(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        assert run_main(monkeypatch, tmp_path, report(("a.m4b", "pass")),
                        report(("a.m4b", "fail")), "--json", "-", "--strict") == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["summary"]["verdict"] == "regressed"
        assert payload["summary"]["regressed"] == 1
        assert payload["regressed"][0]["path"] == "a.m4b"

    def test_nothing_but_json_reaches_stdout(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """`--json -` is meant to be piped. One stray line of the human table makes it
        unparseable, and a caller that shrugs off the parse error is back to no gate at all."""
        run_main(monkeypatch, tmp_path, report(("a.m4b", "pass")), report(("a.m4b", "fail")),
                 "--json", "-")
        assert json.loads(capsys.readouterr().out)["summary"]["verdict"] == "regressed"

    def test_writing_json_to_a_file_still_prints_the_table(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        out = tmp_path / "diff.json"
        assert run_main(monkeypatch, tmp_path, report(("a.m4b", "pass")),
                        report(("a.m4b", "fail")), "--json", str(out), "--strict") == 1
        assert json.loads(out.read_text())["summary"]["verdict"] == "regressed"
        assert "VERDICT: regressed" in capsys.readouterr().out

    def test_a_clean_run_serializes_as_clean(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        assert run_main(monkeypatch, tmp_path, report(("a.m4b", "fail")),
                        report(("a.m4b", "pass")), "--json", "-", "--strict") == 0
        assert json.loads(capsys.readouterr().out)["summary"]["verdict"] == "clean"

    def test_a_refused_diff_writes_no_json_file_to_be_mistaken_for_a_result(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        """The refusal happens before any artifact is written, so a pipeline that reads the file
        gets an error rather than a stale or empty "clean". Note this differs from verify_scan,
        which writes an explicit `inconclusive` artifact; here the exit code carries it alone.
        """
        out = tmp_path / "diff.json"
        assert run_main(monkeypatch, tmp_path, inconclusive(), report(("a.m4b", "pass")),
                        "--json", str(out)) == 2
        assert not out.exists()


@pytest.mark.contract
class TestUnusableInput:
    """Input this tool cannot read must raise, never resolve to a calm empty diff.

    TESTING.md sub-contract 3: an empty result that reads as a clean run is the worst outcome a
    data source can produce, because "no regressions" is exactly the answer everybody wants.
    """

    def test_a_missing_input_file_raises(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        monkeypatch.setattr(sys, "argv", [
            "conformance_diff.py", str(tmp_path / "nope.json"), str(tmp_path / "also-nope.json"),
            "--strict",
        ])
        with pytest.raises(FileNotFoundError):
            main()

    def test_a_truncated_report_raises_rather_than_diffing_what_survived(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        """A half-written report is the realistic corruption: the harness was killed mid-run."""
        (tmp_path / "base.json").write_text(json.dumps(report(("a.m4b", "pass")))[:40])
        (tmp_path / "head.json").write_text(json.dumps(report(("a.m4b", "fail"))))
        monkeypatch.setattr(sys, "argv", [
            "conformance_diff.py", str(tmp_path / "base.json"), str(tmp_path / "head.json"),
            "--strict",
        ])
        with pytest.raises(json.JSONDecodeError):
            main()

    def test_a_result_row_without_a_verdict_raises(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        # A renamed field in verify_scan's schema must break loudly here, not silently make
        # every case look unchanged.
        base = {"summary": {"overall": "pass"}, "results": [{"path": "a.m4b", "result": "pass"}]}
        with pytest.raises(KeyError):
            run_main(monkeypatch, tmp_path, base, report(("a.m4b", "fail")), "--strict")

    def test_a_result_row_without_a_path_raises(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        base = {"summary": {"overall": "pass"}, "results": [{"file": "a.m4b", "verdict": "pass"}]}
        with pytest.raises(KeyError):
            run_main(monkeypatch, tmp_path, base, report(("a.m4b", "fail")), "--strict")

    @pytest.mark.contract
    def test_a_base_report_with_no_results_is_refused_rather_than_read_as_empty(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        """Point the wrong JSON file at this tool and it agrees that nothing regressed.

        `{}`, an older tool's output, or a report from a run that produced no rows would all
        index to an empty base. Every head case then classified as `added`, the verdict came out
        `clean`, and `--strict` exited 0 over a head run that was failing outright. The tool now
        refuses, on the same reasoning the docstring already gave for an inconclusive report: you
        cannot diff against a run that could not look.
        """
        assert run_main(monkeypatch, tmp_path, {}, report(("a.m4b", "fail")), "--strict") != 0


class TestTheProcessBoundary:
    """One real subprocess, to prove the module entry point still exists and wires up.

    Everything else calls main() directly, which cannot catch a broken `if __name__` block or a
    return value that never reaches the shell. That is worth exactly one slow test.
    """

    @pytest.mark.contract
    def test_a_regression_reaches_the_shell_as_a_nonzero_exit(
        self, tmp_path: pathlib.Path
    ) -> None:
        (tmp_path / "base.json").write_text(json.dumps(report(("a.m4b", "pass"))))
        (tmp_path / "head.json").write_text(json.dumps(report(("a.m4b", "fail"))))
        result = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "conformance_diff.py"),
             str(tmp_path / "base.json"), str(tmp_path / "head.json"), "--strict"],
            capture_output=True, text=True,
        )
        assert result.returncode == 1, result.stdout + result.stderr
