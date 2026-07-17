"""The layout picker's logic lives in docs/layouts.js and is tested with node's built-in runner.

Run those tests as part of pytest so `python -m pytest` covers the whole repo — Python and JS —
with one command, and no npm toolchain (node:test is built in).
"""
from __future__ import annotations

import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required for the picker JS tests")
def test_picker_logic_passes_node_tests() -> None:
    result = subprocess.run(
        ["node", "--test", str(DOCS)], capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_index_html_imports_the_same_logic_module() -> None:
    # The UI and the tests must share one source of truth for the layout logic.
    html = (DOCS / "index.html").read_text()
    assert "./layouts.js" in html
    for symbol in ("renderPath", "matchPreset", "commandFor", "PRESETS", "TOKENS"):
        assert symbol in html, f"index.html does not use {symbol} from layouts.js"
