"""Contract tests for the reported-size verdict (see TESTING.md, issue #15).

The interesting failures are not arithmetic. They are the ways this check could quietly agree that a
wrong size is fine:

* false PASS            — a stored size that does not match the files is reported as correct
* false PASS via absence — no size stored at all reads as "nothing wrong here"
* false FAIL            — a correct size is called wrong
* false PASS via silence — the answer key cannot be read, so nothing is found to complain about

The second is the one this tool exists for. Listenarr#542 is partly about books that show no
total, so treating a missing value as acceptable would miss half the bug this was written to find.
"""
from __future__ import annotations

import json
import pathlib
import sqlite3
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

from size_report import (
    EXIT_FAIL,
    EXIT_INCONCLUSIVE,
    EXIT_PASS,
    SizeReport,
    main,
    on_disk_totals,
)

ASIN = "B000TESTED"


def report(reported: int | None, linked_sum: int = 900, linked_files: int = 3) -> SizeReport:
    return SizeReport(
        label="test", asin=ASIN, title="A Book",
        on_disk_bytes=900, on_disk_files=3,
        linked_sum_bytes=linked_sum, linked_files=linked_files,
        reported_bytes=reported,
    )


@pytest.mark.contract
class TestVerdictContract:
    def test_a_size_that_does_not_match_the_files_cannot_pass(self) -> None:
        # false PASS: one file's size standing in for the whole book is the reported bug.
        r = report(reported=300)
        assert r.verdict == "wrong"
        assert r.exit_code == EXIT_FAIL

    def test_no_stored_size_is_a_failure_not_an_absence(self) -> None:
        # false PASS via absence: "not set" is half of Listenarr#542, so it must not read as fine.
        r = report(reported=None)
        assert r.verdict == "missing"
        assert r.exit_code == EXIT_FAIL

    def test_a_matching_size_is_not_called_wrong(self) -> None:
        # false FAIL: the tool has to be able to say a correct implementation is correct.
        r = report(reported=900)
        assert r.verdict == "correct"
        assert r.exit_code == EXIT_PASS

    def test_nothing_linked_is_inconclusive_rather_than_pass(self) -> None:
        # With no linked files there is no claim to judge, and silence must not read as success.
        r = report(reported=None, linked_sum=0, linked_files=0)
        assert r.verdict == "inconclusive"
        assert r.exit_code == EXIT_INCONCLUSIVE
        assert r.exit_code != EXIT_PASS

    def test_an_answer_key_describing_no_files_is_refused(self) -> None:
        with pytest.raises(ValueError):
            on_disk_totals({"entries": []}, ROOT, ASIN)

    def test_a_short_linked_sum_is_reported_as_a_discovery_problem(self) -> None:
        # If the scan missed files, saying only "the size is wrong" would point at the wrong code.
        r = report(reported=600, linked_sum=600, linked_files=2)
        assert r.scan_found_everything is False
        assert r.verdict == "correct"  # correct *for what was linked*, which the render explains


class TestGroundTruth:
    def test_on_disk_totals_sums_only_the_requested_book(self, tmp_path: pathlib.Path) -> None:
        (tmp_path / "a").mkdir()
        (tmp_path / "a" / "one.mp3").write_bytes(b"x" * 100)
        (tmp_path / "a" / "two.mp3").write_bytes(b"x" * 50)
        (tmp_path / "a" / "other.mp3").write_bytes(b"x" * 999)
        manifest = {"entries": [
            {"path": "a/one.mp3", "belongs_to_asin": ASIN},
            {"path": "a/two.mp3", "belongs_to_asin": ASIN},
            {"path": "a/other.mp3", "belongs_to_asin": "B000OTHER0"},
        ]}
        assert on_disk_totals(manifest, tmp_path, ASIN) == (150, 2)


class TestCli:
    def _fixture(
        self, tmp_path: pathlib.Path, stored: int | None
    ) -> tuple[pathlib.Path, pathlib.Path]:
        (tmp_path / "bk").mkdir()
        for name in ("1.mp3", "2.mp3"):
            (tmp_path / "bk" / name).write_bytes(b"x" * 400)
        manifest = tmp_path / "manifest.json"
        manifest.write_text(json.dumps({"entries": [
            {"path": "bk/1.mp3", "belongs_to_asin": ASIN},
            {"path": "bk/2.mp3", "belongs_to_asin": ASIN},
        ]}))
        db = tmp_path / "listenarr.db"
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE Audiobooks (Id TEXT, Asin TEXT, Title TEXT, FileSize INTEGER)")
        con.execute("CREATE TABLE AudiobookFiles (AudiobookId TEXT, Size INTEGER)")
        con.execute("INSERT INTO Audiobooks VALUES ('1', ?, 'A Book', ?)", (ASIN, stored))
        con.executemany("INSERT INTO AudiobookFiles VALUES ('1', ?)", [(400,), (400,)])
        con.commit()
        con.close()
        return manifest, db

    def _run(self, tmp_path: pathlib.Path, stored: int | None) -> int:
        manifest, db = self._fixture(tmp_path, stored)
        return main(["--manifest", str(manifest), "--library", str(tmp_path),
                     "--db", str(db), "--book-id", "1", "--asin", ASIN])

    def test_missing_stored_size_exits_failure(self, tmp_path: pathlib.Path) -> None:
        assert self._run(tmp_path, None) == EXIT_FAIL

    def test_single_file_size_standing_in_for_the_total_exits_failure(
        self, tmp_path: pathlib.Path
    ) -> None:
        assert self._run(tmp_path, 400) == EXIT_FAIL

    def test_correct_total_exits_pass(self, tmp_path: pathlib.Path) -> None:
        assert self._run(tmp_path, 800) == EXIT_PASS

    def test_unreadable_manifest_is_inconclusive(self, tmp_path: pathlib.Path) -> None:
        _, db = self._fixture(tmp_path, 800)
        assert main(["--manifest", str(tmp_path / "nope.json"), "--library", str(tmp_path),
                     "--db", str(db), "--book-id", "1", "--asin", ASIN]) == EXIT_INCONCLUSIVE


class TestScript:
    SCRIPT = ROOT / "tools" / "validate_reported_size.sh"

    def test_script_is_syntactically_valid(self) -> None:
        import subprocess
        result = subprocess.run(["bash", "-n", str(self.SCRIPT)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr

    def test_unknown_flag_is_refused(self) -> None:
        import subprocess
        result = subprocess.run(
            ["bash", str(self.SCRIPT), "--bogus"], capture_output=True, text=True
        )
        assert result.returncode != 0
        assert "unknown argument" in (result.stderr + result.stdout)
