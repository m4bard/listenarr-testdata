"""The A/B conformance diff: what changed between two runs, and the gate on it.

The value of a diff tool is entirely in one property — it must be able to make a run that
regressed a case FAIL, even when the head build is still "mostly green". These pin that property
(the #15 verdict-contract category) alongside the classification itself.
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

from conformance_diff import DiffError, diff_reports  # noqa: E402


def report(*results: tuple[str, str]) -> dict:
    """A minimal verify_scan-shaped JSON report from (path, verdict) pairs."""
    return {
        "summary": {"overall": "fail" if any(v != "pass" for _, v in results) else "pass"},
        "results": [{"path": p, "verdict": v, "case": "-", "why": ""} for p, v in results],
    }


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

    def test_unchanged_cases_are_silent(self) -> None:
        base = report(("a.m4b", "pass"), ("b.m4b", "fail"))
        head = report(("a.m4b", "pass"), ("b.m4b", "fail"))
        diff = diff_reports(base, head)
        assert not diff.regressed and not diff.fixed

    def test_added_and_dropped_are_tracked_separately(self) -> None:
        base = report(("a.m4b", "pass"))
        head = report(("b.m4b", "pass"))
        diff = diff_reports(base, head)
        assert [r["path"] for r in diff.added] == ["b.m4b"]
        assert [r["path"] for r in diff.dropped] == ["a.m4b"]

    def test_an_inconclusive_side_refuses_to_diff(self) -> None:
        # You cannot compare against a run that could not observe the scan — that must be a loud
        # refusal, not a diff that treats every case as regressed.
        base = {"summary": {"overall": "inconclusive"}, "error": "source rotted", "results": []}
        head = report(("a.m4b", "pass"))
        with pytest.raises(DiffError):
            diff_reports(base, head)


class TestGate:
    """The contract: a regression can fail the gate; a clean diff cannot."""

    def _run(self, tmp_path: pathlib.Path, base: dict, head: dict, *extra: str):
        (tmp_path / "base.json").write_text(json.dumps(base))
        (tmp_path / "head.json").write_text(json.dumps(head))
        return subprocess.run(
            [sys.executable, str(ROOT / "tools" / "conformance_diff.py"),
             str(tmp_path / "base.json"), str(tmp_path / "head.json"), *extra],
            capture_output=True, text=True,
        )

    def test_strict_exits_nonzero_on_a_regression(self, tmp_path: pathlib.Path) -> None:
        result = self._run(tmp_path, report(("a.m4b", "pass")), report(("a.m4b", "fail")),
                           "--strict")
        assert result.returncode == 1

    def test_strict_exits_zero_when_only_fixes(self, tmp_path: pathlib.Path) -> None:
        # A PR that only fixes things must not be blocked by --strict.
        result = self._run(tmp_path, report(("a.m4b", "fail")), report(("a.m4b", "pass")),
                           "--strict")
        assert result.returncode == 0, result.stdout + result.stderr

    def test_json_verdict_reflects_regression(self, tmp_path: pathlib.Path) -> None:
        result = self._run(tmp_path, report(("a.m4b", "pass")), report(("a.m4b", "fail")),
                           "--json", "-")
        payload = json.loads(result.stdout)
        assert payload["summary"]["verdict"] == "regressed"
        assert payload["summary"]["regressed"] == 1

    def test_inconclusive_input_exits_two(self, tmp_path: pathlib.Path) -> None:
        base = {"summary": {"overall": "inconclusive"}, "error": "x", "results": []}
        result = self._run(tmp_path, base, report(("a.m4b", "pass")), "--strict")
        assert result.returncode == 2
