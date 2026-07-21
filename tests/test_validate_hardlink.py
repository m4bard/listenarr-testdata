"""validate_hardlink.sh proves what Listenarr's hardlink/copy import does, on Linux.

The full check drives real containers (it lives in the tool, self-gating on exit code). What the
suite guards without a container: the script is syntactically valid and refuses to run without the
one required argument, so a broken edit is caught by the PR gate rather than at container time.
"""
from __future__ import annotations

import pathlib
import subprocess

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "tools" / "validate_hardlink.sh"


def test_script_is_syntactically_valid() -> None:
    result = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_image_argument_is_required() -> None:
    # No image -> the ${1:?...} guard must abort non-zero, not run a container against nothing.
    result = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True)
    assert result.returncode != 0
    assert "usage" in (result.stderr + result.stdout).lower()
