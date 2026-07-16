"""`--layout` forces a single on-disk layout, so a generated library matches one tool's convention.

The scenario decides tag states and structure; --layout overrides *only* the layout mix, letting a
caller produce e.g. a `{Author}/{Series}/{Title}` (Listenarr default) or `{Author}/{Title}` tree.
"""
from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "corpus"))

import cases  # noqa: E402
from generate_library import generate  # noqa: E402

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe are required to synthesize audio",
)

LIMIT = 12


def book_layouts(manifest: dict) -> set[str]:
    return {e["layout"] for e in manifest["entries"] if e["kind"] == "book"}


def _cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(ROOT / "tools" / "generate_library.py"), *args],
        capture_output=True, text=True,
    )


@needs_ffmpeg
def test_override_forces_a_single_universal_layout(tmp_path: pathlib.Path) -> None:
    # `flat` needs only author+title, so every corpus book can express it — no eligibility skips.
    manifest = generate(
        cases.SCENARIOS_BY_KEY["mixed-reality"], tmp_path / "lib",
        seed=1, limit=LIMIT, layout_override="flat",
    )
    assert book_layouts(manifest) == {"flat"}


@needs_ffmpeg
def test_override_narrows_the_scenario_mix_to_one(tmp_path: pathlib.Path) -> None:
    # mixed-reality normally spreads books across many layouts...
    mixed = generate(cases.SCENARIOS_BY_KEY["mixed-reality"], tmp_path / "m", seed=1, limit=LIMIT)
    assert len(book_layouts(mixed)) > 1
    # ...and the override collapses it to exactly one.
    forced = generate(
        cases.SCENARIOS_BY_KEY["mixed-reality"], tmp_path / "f",
        seed=1, limit=LIMIT, layout_override="flat",
    )
    assert book_layouts(forced) == {"flat"}


@needs_ffmpeg
def test_override_with_listenarr_default_layout(tmp_path: pathlib.Path) -> None:
    # {Author}/{Series}/{Title} — the layout the maintainer's own tests use. Needs a series,
    # so assert non-empty explicitly rather than let a silent all-skip pass.
    manifest = generate(
        cases.SCENARIOS_BY_KEY["mixed-reality"], tmp_path / "lib",
        seed=1, limit=LIMIT, layout_override="author-series-title",
    )
    layouts = book_layouts(manifest)
    assert layouts, "no book expressed author-series-title — check corpus series coverage"
    assert layouts == {"author-series-title"}


def test_unknown_layout_is_rejected_with_a_helpful_error(tmp_path: pathlib.Path) -> None:
    # Fails during arg parsing, before any generation — no ffmpeg needed.
    result = _cli("--layout", "not-a-real-layout", "--out", str(tmp_path / "x"))
    assert result.returncode != 0
    assert "unknown layout" in (result.stderr + result.stdout).lower()


@needs_ffmpeg
def test_layout_alone_defaults_to_the_adoption_scenario(tmp_path: pathlib.Path) -> None:
    out = tmp_path / "lib"
    result = _cli("--layout", "flat", "--out", str(out), "--limit", str(LIMIT))
    assert result.returncode == 0, result.stderr
    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["scenario"] == "existing-library-adoption"
    assert book_layouts(manifest) == {"flat"}


def test_list_layouts_prints_the_menu() -> None:
    result = _cli("--list-layouts")
    assert result.returncode == 0
    # every layout key should appear
    for layout in cases.LAYOUTS:
        assert layout.key in result.stdout
