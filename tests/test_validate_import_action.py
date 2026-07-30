"""validate_import_action.sh proves what a completed-file import action really did, on Linux.

The full check drives real containers and self-gates on its exit code, so the suite guards the
parts that do not need one: the script parses, it refuses to run without an image, and it refuses
an action it cannot classify. An action string that reached the API unvalidated would import
something for real and then be judged against the wrong expectation.
"""
from __future__ import annotations

import pathlib
import subprocess

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "tools" / "validate_import_action.sh"


def run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True)


def test_script_is_syntactically_valid() -> None:
    result = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_image_argument_is_required() -> None:
    # No image -> the ${1:?...} guard must abort non-zero, not run a container against nothing.
    result = run()
    assert result.returncode != 0
    assert "usage" in (result.stderr + result.stdout).lower()


def test_an_unclassifiable_action_is_refused() -> None:
    # The verdict only knows how to recognise a hardlink, a copy and a symlink. Anything else would
    # import for real and then be compared against an expectation the script cannot express.
    result = run("some-image", "--action", "nonsense")
    assert result.returncode != 0
    assert "unknown --action" in (result.stderr + result.stdout)


def test_known_actions_are_accepted() -> None:
    # Accepted actions get past parsing and fail later, on the missing container runtime or venv,
    # never on the action itself.
    for action in ("hardlink/copy", "symlink"):
        result = run("some-image", "--action", action)
        assert "unknown --action" not in (result.stderr + result.stdout)


def test_unknown_flags_are_refused() -> None:
    result = run("some-image", "--bogus")
    assert result.returncode != 0
    assert "unknown argument" in (result.stderr + result.stdout)


def test_usage_names_both_actions() -> None:
    result = run()
    out = result.stderr + result.stdout
    assert "hardlink/copy" in out and "symlink" in out
