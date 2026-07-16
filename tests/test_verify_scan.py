"""The conformance checker, and the destructive check.

verify_scan.py is what makes this repo evidence rather than fixtures, so it has to be
trustworthy in one specific way: it must not report a pass that did not happen. These tests
drive it against a synthetic SQLite database shaped like Listenarr's, and against a rename
that really does lose a file.
"""
from __future__ import annotations

import json
import pathlib
import shutil
import sqlite3
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "corpus"))

import cases
from generate_library import generate
from verify_scan import (
    Observation,
    SourceError,
    audit,
    check_base_paths,
    compare,
    from_sqlite,
    normalize,
    snapshot,
)

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="ffmpeg is required to synthesize audio"
)


@pytest.fixture
def library(tmp_path: pathlib.Path) -> tuple[pathlib.Path, dict]:
    out = tmp_path / "lib"
    manifest = generate(cases.SCENARIOS_BY_KEY["happy-path"], out, seed=1, limit=6)
    return out, manifest


def make_db(path: pathlib.Path, rows: list[tuple[str, str | None, str | None, str | None]]) -> None:
    """A database shaped like Listenarr's: Audiobooks + AudiobookFiles."""
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE Audiobooks (
            Id INTEGER PRIMARY KEY, Title TEXT, Asin TEXT, BasePath TEXT
        );
        CREATE TABLE AudiobookFiles (
            Id INTEGER PRIMARY KEY, AudiobookId INTEGER, Path TEXT
        );
        """
    )
    for index, (file_path, asin, title, base_path) in enumerate(rows, start=1):
        connection.execute(
            "INSERT INTO Audiobooks (Id, Title, Asin, BasePath) VALUES (?, ?, ?, ?)",
            (index, title, asin, base_path),
        )
        connection.execute(
            "INSERT INTO AudiobookFiles (Id, AudiobookId, Path) VALUES (?, ?, ?)",
            (index, index, file_path),
        )
    connection.commit()
    connection.close()


class TestSqliteSource:
    def test_reads_linked_files(self, tmp_path: pathlib.Path) -> None:
        db = tmp_path / "listenarr.db"
        make_db(db, [("/audiobooks/a.m4b", "B008DFUGCQ", "A Princess of Mars", "/audiobooks")])
        observations = from_sqlite(db)
        assert len(observations) == 1
        assert observations[0].asin == "B008DFUGCQ"
        assert observations[0].path == "/audiobooks/a.m4b"

    def test_an_unlinked_file_simply_is_not_there(self, tmp_path: pathlib.Path) -> None:
        # A file the scan dropped has no row at all. Absence IS the observation, and for the
        # existing-library-adoption scenario it is the entire finding.
        db = tmp_path / "listenarr.db"
        make_db(db, [])
        assert from_sqlite(db) == []


class TestSourceRotIsLoud:
    """Contract: a rotted source RAISES; it must never read as an honest empty result.

    A false '0% linked' from a moved schema is indistinguishable from a real scan regression —
    the worst failure mode for a conformance tool. These pin that a source which can no longer be
    trusted fails loudly (SourceError) rather than silently returning [].
    """

    def test_a_renamed_table_raises_not_empty(self, tmp_path: pathlib.Path) -> None:
        db = tmp_path / "moved.db"
        connection = sqlite3.connect(db)
        # The schema moved: Audiobooks -> Books. The old, silent behaviour returned [].
        connection.executescript(
            "CREATE TABLE Books (Id INTEGER PRIMARY KEY, Title TEXT, Asin TEXT, BasePath TEXT);"
            "CREATE TABLE AudiobookFiles (Id INTEGER PRIMARY KEY, AudiobookId INTEGER, Path TEXT);"
        )
        connection.close()
        with pytest.raises(SourceError) as exc:
            from_sqlite(db)
        assert "Audiobooks" in str(exc.value)  # names what it expected and could not find

    def test_a_dropped_column_raises_not_empty(self, tmp_path: pathlib.Path) -> None:
        db = tmp_path / "narrowed.db"
        connection = sqlite3.connect(db)
        # Asin column dropped: the query would still parse to zero rows on an empty table, so a
        # column-level probe is what catches this one.
        connection.executescript(
            "CREATE TABLE Audiobooks (Id INTEGER PRIMARY KEY, Title TEXT, BasePath TEXT);"
            "CREATE TABLE AudiobookFiles (Id INTEGER PRIMARY KEY, AudiobookId INTEGER, Path TEXT);"
        )
        connection.close()
        with pytest.raises(SourceError) as exc:
            from_sqlite(db)
        assert "Asin" in str(exc.value)

    def test_a_correct_schema_with_zero_rows_is_still_a_clean_empty(
        self, tmp_path: pathlib.Path
    ) -> None:
        # The line the probe must NOT cross: a valid, empty database is a legitimate observation
        # (nothing linked), not a source error. Only a MOVED schema is an error.
        db = tmp_path / "empty.db"
        make_db(db, [])
        assert from_sqlite(db) == []


class TestRootMapping:
    def test_a_container_path_maps_onto_a_host_path(self) -> None:
        # Listenarr sees /audiobooks; we generated into ./build/lib. Without this every case
        # would report as missing and the whole run would be a false alarm.
        mapped = normalize("/audiobooks/Author/Book.m4b", ("/audiobooks", "build/lib"))
        assert mapped == "build/lib/Author/Book.m4b"

    def test_an_unmapped_path_is_unchanged(self) -> None:
        assert normalize("/audiobooks/x.m4b", None) == "/audiobooks/x.m4b"


class TestCompare:
    def test_a_correctly_linked_library_passes(
        self, library: tuple[pathlib.Path, dict]
    ) -> None:
        out, manifest = library
        observations = [
            Observation(path=str(out / e["path"]), asin=e["belongs_to_asin"],
                        title=e["true_title"])
            for e in manifest["entries"]
        ]
        report = compare(manifest, observations, out, None)
        assert report.failed == 0
        assert report.passed == len(manifest["entries"])

    def test_a_file_that_was_never_linked_is_reported_missing(
        self, library: tuple[pathlib.Path, dict]
    ) -> None:
        # THE headline bug: an existing library in a layout Listenarr does not parse is not
        # partially discovered, it is not discovered at all.
        out, manifest = library
        report = compare(manifest, [], out, None)
        assert report.passed == 0
        assert all(r.verdict == "missing" for r in report.results)

    def test_linking_a_file_to_the_wrong_book_is_a_failure_not_a_pass(
        self, library: tuple[pathlib.Path, dict]
    ) -> None:
        # A mis-link is worse than a miss: it silently attaches the wrong book and the user
        # has no reason to look. It must never score as a pass.
        out, manifest = library
        entry = manifest["entries"][0]
        observations = [
            Observation(path=str(out / entry["path"]), asin="B071YLS9YL",
                        title="Some Other Book")
        ]
        report = compare(manifest, observations, out, None)
        wrong = next(r for r in report.results if r.entry["path"] == entry["path"])
        assert wrong.verdict == "fail"
        assert "expected" in wrong.why

    def test_a_record_with_no_asin_is_matched_on_title(
        self, library: tuple[pathlib.Path, dict]
    ) -> None:
        # The common real-world case: an entire library with zero ASINs. The check has to
        # fall back to the title, or every no-ASIN library reports as a total failure.
        out, manifest = library
        observations = [
            Observation(path=str(out / e["path"]), asin=None, title=e["true_title"])
            for e in manifest["entries"]
        ]
        report = compare(manifest, observations, out, None)
        assert report.failed == 0

    def test_clutter_attached_to_a_book_is_a_failure(self, tmp_path: pathlib.Path) -> None:
        out = tmp_path / "lib"
        manifest = generate(cases.SCENARIOS_BY_KEY["clutter"], out, seed=1, limit=3)
        sample = next(e for e in manifest["entries"]
                      if e.get("clutter_kind") == "sample-track")
        observations = [
            Observation(path=str(out / sample["path"]), asin="B008DFUGCQ", title="Whatever")
        ]
        report = compare(manifest, observations, out, None)
        bad = next(r for r in report.results if r.entry["path"] == sample["path"])
        assert bad.verdict == "fail"
        assert "not the book" in bad.why

    def test_unlinked_clutter_passes(self, tmp_path: pathlib.Path) -> None:
        out = tmp_path / "lib"
        manifest = generate(cases.SCENARIOS_BY_KEY["clutter"], out, seed=1, limit=3)
        report = compare(manifest, [], out, None)
        clutter = [r for r in report.results if r.entry["kind"] == "clutter"]
        assert clutter
        assert all(r.verdict == "pass" for r in clutter)

    def test_sidecars_are_not_link_candidates_at_all(self, tmp_path: pathlib.Path) -> None:
        # desc.txt is read by the parser, which is a different question from being linked.
        # Scoring it as an unlinked audio file would be noise.
        out = tmp_path / "lib"
        manifest = generate(cases.SCENARIOS_BY_KEY["clutter"], out, seed=1, limit=3)
        report = compare(manifest, [], out, None)
        assert not any(r.entry.get("clutter_kind") == "sidecars" for r in report.results)


class TestWorkEquivalence:
    """Contract: a link to another valid ASIN of the same work is a PASS, not a FAIL.

    The repo's headline thesis is that a book ASIN is a manifestation id and (series_asin,
    position) is the stable work key. An answer key that demands one exact ASIN would score that
    very thesis as a scanner failure. These pin the equivalence — and its edges, so it does not
    turn into 'any ASIN passes'.
    """

    @pytest.fixture
    def twins(self, tmp_path: pathlib.Path) -> tuple[pathlib.Path, dict, dict, dict]:
        out = tmp_path / "lib"
        manifest = generate(cases.SCENARIOS_BY_KEY["series-work-key"], out, seed=1)
        # Two book entries that share a work key but carry different ASINs — the whole point of
        # the scenario. If the corpus ever loses its twin pairs this fixture fails loudly.
        by_work: dict[tuple, list[dict]] = {}
        for e in manifest["entries"]:
            if e["kind"] != "book" or not e.get("true_series_asin"):
                continue
            by_work.setdefault(
                (e["true_series_asin"], str(e["true_series_position"])), []
            ).append(e)
        pair = next(
            entries for entries in by_work.values()
            if len({e["belongs_to_asin"] for e in entries}) > 1
        )
        a = pair[0]
        b = next(e for e in pair if e["belongs_to_asin"] != a["belongs_to_asin"])
        return out, manifest, a, b

    def test_linking_to_the_twin_asin_passes(
        self, twins: tuple[pathlib.Path, dict, dict, dict]
    ) -> None:
        out, manifest, a, b = twins
        # The scanner linked file A to B's ASIN — a different manifestation of the same work.
        observations = [
            Observation(path=str(out / a["path"]), asin=b["belongs_to_asin"], title=b["true_title"])
        ]
        report = compare(manifest, observations, out, None)
        result = next(r for r in report.results if r.entry["path"] == a["path"])
        assert result.verdict == "pass"
        assert "work-equivalent" in result.why

    def test_a_different_position_in_the_same_series_still_fails(
        self, twins: tuple[pathlib.Path, dict, dict, dict]
    ) -> None:
        # The edge that keeps equivalence honest: same series ASIN, WRONG position is a real
        # mis-link (book 1 vs book 2 of a series), and must not be waved through.
        out, manifest, a, _ = twins
        intruder = next(
            e for e in manifest["entries"]
            if e["kind"] == "book"
            and e.get("true_series_asin") == a["true_series_asin"]
            and str(e.get("true_series_position")) != str(a["true_series_position"])
        )
        observations = [
            Observation(path=str(out / a["path"]), asin=intruder["belongs_to_asin"],
                        title=intruder["true_title"])
        ]
        report = compare(manifest, observations, out, None)
        result = next(r for r in report.results if r.entry["path"] == a["path"])
        assert result.verdict == "fail"

    def test_an_unrelated_asin_still_fails(
        self, twins: tuple[pathlib.Path, dict, dict, dict]
    ) -> None:
        out, manifest, a, _ = twins
        observations = [
            Observation(path=str(out / a["path"]), asin="B0UNRELATED", title="Something Else")
        ]
        report = compare(manifest, observations, out, None)
        result = next(r for r in report.results if r.entry["path"] == a["path"])
        assert result.verdict == "fail"


class TestStrictExitCode:
    """Contract: a broken scan must be able to make the pipeline exit non-zero.

    The verified defect (#6): the whole flow reported green for a totally broken branch because
    verify_scan returned 0 by default and nothing threaded --strict. These pin the exit code as a
    real signal — including the scoping that makes --strict usable on a run that adds only a
    subset of the library, which is why the blanket flag was unsafe to add before.
    """

    def _run(self, manifest_path: pathlib.Path, observed: list[dict], out: pathlib.Path,
             *extra: str) -> subprocess.CompletedProcess:
        observed_file = out / "observed.json"
        observed_file.write_text(json.dumps(observed))
        return subprocess.run(
            [sys.executable, str(ROOT / "tools" / "verify_scan.py"),
             "--manifest", str(manifest_path), "--observed", str(observed_file), *extra],
            capture_output=True, text=True,
        )

    def _linked(self, out: pathlib.Path, entries: list[dict]) -> list[dict]:
        return [{"path": str(out / e["path"]), "asin": e["belongs_to_asin"],
                 "title": e["true_title"]} for e in entries if e["kind"] == "book"]

    def test_zero_links_without_strict_still_exits_zero(
        self, library: tuple[pathlib.Path, dict]
    ) -> None:
        # Documents the default the defect rode in on: silence is exit 0. Kept explicit so a
        # future change to the default is a conscious one, caught here.
        out, manifest = library
        result = self._run(out / "manifest.json", [], out)
        assert result.returncode == 0

    def test_zero_links_with_strict_exits_nonzero(
        self, library: tuple[pathlib.Path, dict]
    ) -> None:
        # THE regression guard: a totally broken scan (nothing linked) under --strict must fail.
        out, manifest = library
        result = self._run(out / "manifest.json", [], out, "--strict")
        assert result.returncode == 1

    def test_a_fully_correct_scan_with_strict_exits_zero(
        self, library: tuple[pathlib.Path, dict]
    ) -> None:
        out, manifest = library
        result = self._run(out / "manifest.json", self._linked(out, manifest["entries"]),
                           out, "--strict")
        assert result.returncode == 0, result.stdout + result.stderr

    def test_strict_scoped_to_scanned_books_ignores_unadded_ones(
        self, library: tuple[pathlib.Path, dict]
    ) -> None:
        # The scale-perf case: only some books are added, the rest of the library is unlinked on
        # purpose. --strict alone would fail on the un-added books forever; scoped to the one book
        # actually scanned, a correct link passes.
        out, manifest = library
        books = [e for e in manifest["entries"] if e["kind"] == "book"]
        one = books[0]["belongs_to_asin"]
        scanned = [e for e in books if e["belongs_to_asin"] == one]
        result = self._run(out / "manifest.json", self._linked(out, scanned),
                           out, "--strict", "--only-asin", one)
        assert result.returncode == 0, result.stdout + result.stderr
        # And unscoped, the same observation fails, because the other books read as missing.
        unscoped = self._run(out / "manifest.json", self._linked(out, scanned), out, "--strict")
        assert unscoped.returncode == 1

    def test_only_asin_matching_nothing_is_loud_not_a_silent_pass(
        self, library: tuple[pathlib.Path, dict]
    ) -> None:
        # A scope that matches no generated book is a mistake, not an empty success. Exit 2, the
        # same inconclusive code a rotted source uses — never a green 0.
        out, manifest = library
        result = self._run(out / "manifest.json", [], out, "--strict", "--only-asin", "NOPE")
        assert result.returncode == 2


class TestBasePath:
    def test_a_basepath_shared_by_two_books_is_reported(
        self, library: tuple[pathlib.Path, dict]
    ) -> None:
        # CalculateBasePath climbing one level too far makes BasePath the AUTHOR folder,
        # which swallows every other book by that author.
        out, manifest = library
        first, second = manifest["entries"][0], manifest["entries"][1]
        assert first["belongs_to_asin"] != second["belongs_to_asin"]
        observations = [
            Observation(path=str(out / first["path"]), asin=first["belongs_to_asin"],
                        title=first["true_title"], base_path="/audiobooks/Author"),
            Observation(path=str(out / second["path"]), asin=second["belongs_to_asin"],
                        title=second["true_title"], base_path="/audiobooks/Author"),
        ]
        report = compare(manifest, observations, out, None)
        problems = check_base_paths(report, out)
        assert any("swallowed a sibling" in p for p in problems)

    def test_a_basepath_of_the_library_root_is_reported(
        self, library: tuple[pathlib.Path, dict]
    ) -> None:
        # With no BasePath the scan falls back to the library root and ffprobes every
        # unmatched file in the library, once per audiobook scanned.
        out, manifest = library
        entry = manifest["entries"][0]
        observations = [
            Observation(path=str(out / entry["path"]), asin=entry["belongs_to_asin"],
                        title=entry["true_title"], base_path=str(out))
        ]
        report = compare(manifest, observations, out, None)
        assert any("IS the library root" in p for p in check_base_paths(report, out))

    def test_correct_basepaths_report_nothing(
        self, library: tuple[pathlib.Path, dict]
    ) -> None:
        out, manifest = library
        observations = [
            Observation(path=str(out / e["path"]), asin=e["belongs_to_asin"],
                        title=e["true_title"],
                        base_path=str((out / e["path"]).parent))
            for e in manifest["entries"]
        ]
        report = compare(manifest, observations, out, None)
        assert check_base_paths(report, out) == []


class TestRenameAudit:
    def test_an_untouched_library_audits_clean(
        self, library: tuple[pathlib.Path, dict]
    ) -> None:
        out, _ = library
        assert audit(snapshot(out), out) == []

    def test_a_rename_that_preserves_content_audits_clean(
        self, library: tuple[pathlib.Path, dict]
    ) -> None:
        # Files are tracked by CONTENT, not by name — a rename is precisely a change of
        # name. The question is whether every byte still exists somewhere under the root.
        out, _ = library
        before = snapshot(out)
        for path in list(out.rglob("*.m4b")):
            path.rename(path.with_name("renamed-" + path.name))
        assert audit(before, out) == []

    def test_a_rename_that_LOSES_a_file_is_caught(
        self, library: tuple[pathlib.Path, dict]
    ) -> None:
        # The unforgivable bug. A matching bug leaves a file unlinked; a renaming bug
        # destroys data, and the two are one line apart in the same code path.
        out, _ = library
        before = snapshot(out)
        victim = next(out.rglob("*.m4b"))
        victim.unlink()
        problems = audit(before, out)
        assert len(problems) == 1
        assert "DATA LOSS" in problems[0]

    def test_a_rename_that_clobbers_one_file_with_another_is_caught(
        self, library: tuple[pathlib.Path, dict]
    ) -> None:
        # Two books whose paths collide under a case-insensitive filesystem: one overwrites
        # the other and the file count alone would not notice, because it stays the same.
        out, _ = library
        before = snapshot(out)
        files = sorted(out.rglob("*.m4b"))
        shutil.copyfile(files[0], files[1])  # files[1]'s content is gone
        problems = audit(before, out)
        assert any("DATA LOSS" in p for p in problems)

    def test_losing_one_of_two_IDENTICAL_files_is_caught(
        self, tmp_path: pathlib.Path
    ) -> None:
        # The hole a content hash alone leaves open. Two untagged files are byte-identical,
        # so if the audit only asked "is this hash still present" it would report clean while
        # a file had in fact been destroyed. It has to compare counts.
        out = tmp_path / "lib"
        out.mkdir()
        (out / "one.m4b").write_bytes(b"identical")
        (out / "two.m4b").write_bytes(b"identical")
        before = snapshot(out)
        assert len(before["files"]) == 1  # one hash, two paths

        (out / "two.m4b").unlink()
        problems = audit(before, out)
        assert len(problems) == 1
        assert "DATA LOSS" in problems[0]
        assert "1 of 2 copies" in problems[0]

    def test_a_path_escaping_the_root_is_caught(
        self, library: tuple[pathlib.Path, dict], tmp_path: pathlib.Path
    ) -> None:
        # SECURITY. A title of '../../etc' interpolated into a rename target escapes the
        # library root. A symlink out of the tree is the shape that leaves behind.
        out, _ = library
        before = snapshot(out)
        outside = tmp_path / "outside"
        outside.mkdir()
        (out / "escape.m4b").symlink_to(outside)
        problems = audit(before, out)
        assert any("ESCAPE" in p for p in problems)
