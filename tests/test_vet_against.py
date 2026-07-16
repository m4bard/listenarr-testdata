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
