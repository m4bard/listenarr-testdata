"""vet-against.sh builds a Listenarr branch and runs the harness against it.

Exercised through --dry-run so the suite needs no clone, no container build, and no network:
the plan must be complete and correct, and dry-run must execute nothing.
"""
from __future__ import annotations

import pathlib
import subprocess

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "tools" / "vet-against.sh"


def run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True)


def test_branch_is_required() -> None:
    result = run("--dry-run")
    assert result.returncode != 0
    assert "branch is required" in (result.stderr + result.stdout).lower()


def test_dry_run_prints_a_complete_plan_and_executes_nothing() -> None:
    result = run("--branch", "some-branch", "--dry-run")
    assert result.returncode == 0
    out = result.stdout + result.stderr
    assert "nothing executed" in out.lower()
    assert "git clone" in out and "some-branch" in out          # clone step
    assert "build -t" in out                                     # build step
    assert "benchmark_scan.sh" in out                            # run step


def test_passthrough_flags_are_forwarded_in_the_plan() -> None:
    result = run("--branch", "b", "--layout", "listenarr", "--no-basepath", "--dry-run")
    assert result.returncode == 0
    out = result.stdout + result.stderr
    assert "--layout" in out and "listenarr" in out
    assert "--no-basepath" in out


def test_custom_repo_is_used_in_the_plan() -> None:
    result = run("--repo", "https://example.com/fork.git", "--branch", "b", "--dry-run")
    assert result.returncode == 0
    assert "https://example.com/fork.git" in (result.stdout + result.stderr)


def test_default_repo_is_upstream() -> None:
    result = run("--branch", "b", "--dry-run")
    assert "github.com/Listenarrs/Listenarr" in (result.stdout + result.stderr)


def test_help_lists_branch_and_forwarded_flags() -> None:
    result = run("--help")
    assert result.returncode == 0
    assert "--branch" in result.stdout
    assert "--layout" in result.stdout


def test_default_tool_is_the_benchmark() -> None:
    result = run("--branch", "b", "--dry-run")
    assert "benchmark_scan.sh" in (result.stdout + result.stderr)


def test_attribution_tool_dispatches_to_the_attribution_validator() -> None:
    # The point of --tool is that someone reviewing a scan-matching PR can build the branch and
    # see who the linked files really belong to in one command, without knowing the harness.
    result = run("--branch", "b", "--tool", "attribution", "--dry-run")
    assert result.returncode == 0
    out = result.stdout + result.stderr
    assert "validate_scan_attribution.sh" in out
    assert "benchmark_scan.sh" not in out


def test_unknown_tool_is_refused() -> None:
    result = run("--branch", "b", "--tool", "nonsense", "--dry-run")
    assert result.returncode != 0
    assert "unknown --tool" in (result.stderr + result.stdout)


def test_attribution_flags_are_forwarded_to_the_validator() -> None:
    result = run("--branch", "b", "--tool", "attribution", "--asin", "B004FOLXEO", "--dry-run")
    assert result.returncode == 0
    out = result.stdout + result.stderr
    assert "--asin" in out and "B004FOLXEO" in out


def test_help_lists_the_tool_choices() -> None:
    result = run("--help")
    assert "--tool" in result.stdout
    assert "attribution" in result.stdout
