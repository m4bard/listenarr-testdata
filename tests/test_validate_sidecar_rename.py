"""validate_sidecar_rename.sh reproduces Listenarr#577 (rename strands companion files).

The full check drives a real container and asserts on the rename plan (self-gating on exit code).
What the suite guards without a container: the script is syntactically valid and refuses to run
without its one required argument, so a broken edit is caught by the PR gate.
"""
from __future__ import annotations

import pathlib
import subprocess

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "tools" / "validate_sidecar_rename.sh"


def test_script_is_syntactically_valid() -> None:
    result = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_image_argument_is_required() -> None:
    result = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True)
    assert result.returncode != 0
    assert "usage" in (result.stderr + result.stdout).lower()
