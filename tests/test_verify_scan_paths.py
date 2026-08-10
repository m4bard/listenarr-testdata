"""A relative manifest and an absolute --root-map must describe the same directory.

Mixing the two forms used to match nothing at all, and the failure was invisible in the worst way:
every file came back "never linked", which reads as a scanner that discovered nothing rather than as
a mistake in how the tool was called. A harness that answers "catastrophe" when it means "you passed
me inconsistent paths" is worse than one that crashes, so both shapes are pinned here.

Found by hand while comparing two images: one reported 0 of 133 files correctly attributed, which
was checked against the database directly and turned out to be false.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from corpus import cases  # noqa: E402
from tools.generate_library import generate  # noqa: E402

TOOL = pathlib.Path(__file__).resolve().parents[1] / "tools" / "verify_scan.py"


@pytest.fixture
def library(tmp_path: pathlib.Path) -> tuple[pathlib.Path, dict]:
    out = tmp_path / "lib"
    manifest = generate(cases.SCENARIOS_BY_KEY["happy-path"], out, seed=1, limit=3)
    return out, manifest


def _observed(tmp_path: pathlib.Path, manifest: dict) -> pathlib.Path:
    """Every generated file, linked to its true owner, as the container would report it."""
    path = tmp_path / "observed.json"
    path.write_text(json.dumps([
        {"path": f"/audiobooks/{entry['path']}", "asin": entry["belongs_to_asin"]}
        for entry in manifest["entries"] if entry.get("belongs_to_asin")
    ]))
    return path


def _total(output: str) -> str:
    line = next((ln for ln in output.splitlines() if ln.startswith("TOTAL")), "")
    return " ".join(line.split())


def _run(manifest_path: pathlib.Path, observed: pathlib.Path, local: str,
         cwd: pathlib.Path) -> str:
    result = subprocess.run(
        [sys.executable, str(TOOL), "--manifest", str(manifest_path),
         "--observed", str(observed), "--root-map", f"/audiobooks={local}"],
        capture_output=True, text=True, cwd=cwd,
    )
    return result.stdout


def test_absolute_and_relative_root_map_agree(
    library: tuple[pathlib.Path, dict], tmp_path: pathlib.Path
) -> None:
    out, manifest = library
    observed = _observed(tmp_path, manifest)
    manifest_path = out / "manifest.json"

    absolute = _run(manifest_path, observed, str(out.resolve()), tmp_path)
    relative = _run(manifest_path, observed, "lib", tmp_path)

    assert _total(absolute), f"no TOTAL row in the absolute run:\n{absolute}"
    assert _total(absolute) == _total(relative), (
        "absolute and relative --root-map disagree about the same directory:\n"
        f"  absolute: {_total(absolute)}\n  relative: {_total(relative)}"
    )


def test_a_correctly_linked_file_is_never_reported_missing(
    library: tuple[pathlib.Path, dict], tmp_path: pathlib.Path
) -> None:
    """The exact symptom: correctly linked files reported as never linked."""
    out, manifest = library
    observed = _observed(tmp_path, manifest)
    output = _run(out / "manifest.json", observed, str(out.resolve()), tmp_path)
    assert "[missing]" not in output, (
        f"a correctly linked file was reported as missing:\n{output}"
    )
