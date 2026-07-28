"""Contract tests for the scan-attribution verdict (see TESTING.md, issue #15).

`validate_scan_attribution.sh` reports whether a scan attributed files to the wrong book. It is
verdict-bearing, so the three failure modes that matter are not "does it compute a number" but:

* false PASS            — a foreign file is claimed and the tool reports success anyway
* false FAIL            — only the book's own files are linked and the tool cries misattribution
* false PASS via silence — the answer key is missing or does not describe the linked paths, and
                           the tool reports success because it saw nothing to complain about

The third is the one that bites: a checker that cannot read its answer key must not be able to
return 0. Inconclusive is a distinct exit code precisely so it can never be mistaken for a pass.
"""
from __future__ import annotations

import json
import pathlib
import sqlite3
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

from attribution_report import (
    EXIT_FAIL,
    EXIT_INCONCLUSIVE,
    EXIT_PASS,
    LinkedFile,
    Report,
    classify,
    main,
    owner_index,
    to_manifest_path,
)

OWN = "B000OWN000"
OTHER = "B000OTHER0"


Index = tuple[dict[str, str], dict[str, str]]


def manifest_of(*entries: tuple[str, str, str]) -> dict[str, object]:
    return {
        "entries": [
            {"path": path, "belongs_to_asin": asin, "true_title": title}
            for path, asin, title in entries
        ]
    }


@pytest.fixture
def index() -> Index:
    return owner_index(
        manifest_of(
            ("M. R. James/Ghost Stories/Ghost Stories.m4b", OWN, "Ghost Stories"),
            (
                "Henry James/The Turn of the Screw/The Turn of the Screw.m4b",
                OTHER,
                "The Turn of the Screw",
            ),
        )
    )


def report_for(paths: list[str], index: Index) -> Report:
    owner, titles = index
    return classify(
        [LinkedFile(path=p) for p in paths],
        owner,
        titles,
        label="test",
        scanned_asin=OWN,
        scanned_title="Ghost Stories",
        base_path="/audiobooks/",
    )


@pytest.mark.contract
class TestVerdictContract:
    """The three ways a verdict can lie."""

    def test_foreign_file_cannot_report_success(self, index: Index) -> None:
        # false PASS: the scan claimed a file belonging to another book.
        report = report_for(
            [
                "/audiobooks/M. R. James/Ghost Stories/Ghost Stories.m4b",
                "/audiobooks/Henry James/The Turn of the Screw/The Turn of the Screw.m4b",
            ],
            index,
        )
        assert report.foreign_files == 1
        assert report.foreign_titles[next(iter(report.foreign))] == "The Turn of the Screw"
        assert report.exit_code == EXIT_FAIL

    def test_own_files_only_is_not_reported_as_failure(self, index: Index) -> None:
        # false FAIL: a correct scan must pass, or the tool is useless as a gate.
        report = report_for(["/audiobooks/M. R. James/Ghost Stories/Ghost Stories.m4b"], index)
        assert report.own_files == 1
        assert report.foreign_files == 0
        assert report.exit_code == EXIT_PASS

    def test_paths_absent_from_the_answer_key_are_inconclusive_not_pass(
        self, index: Index
    ) -> None:
        # false PASS via silence: the manifest does not describe this path, so the tool cannot
        # know who owns it. That must not read as "no misattribution found".
        report = report_for(["/audiobooks/Somebody Else/Mystery/Mystery.m4b"], index)
        assert report.unmapped == 1
        assert report.exit_code == EXIT_INCONCLUSIVE
        assert report.exit_code != EXIT_PASS

    def test_empty_manifest_is_refused_rather_than_silently_passing(self) -> None:
        # An answer key describing nothing would classify everything as unmapped. Refuse it.
        with pytest.raises(ValueError):
            owner_index({"entries": []})

    def test_a_scan_that_linked_nothing_is_a_pass_not_a_crash(self, index: Index) -> None:
        # Zero links is a real observation (an over-strict matcher) and must report cleanly.
        report = report_for([], index)
        assert report.files_linked == 0
        assert report.exit_code == EXIT_PASS


class TestPathMapping:
    def test_container_mount_prefix_is_stripped(self) -> None:
        assert to_manifest_path("/audiobooks/A/B/c.m4b") == "A/B/c.m4b"

    def test_a_path_without_the_mount_prefix_still_normalizes(self) -> None:
        assert to_manifest_path("A//B/./c.m4b") == "A/B/c.m4b"


class TestCli:
    """End to end over a synthetic database of the same shape Listenarr writes."""

    def _db(self, tmp_path: pathlib.Path, linked: list[str]) -> pathlib.Path:
        db = tmp_path / "listenarr.db"
        connection = sqlite3.connect(db)
        connection.execute(
            "CREATE TABLE Audiobooks (Id TEXT, Asin TEXT, Title TEXT, BasePath TEXT)"
        )
        connection.execute("CREATE TABLE AudiobookFiles (AudiobookId TEXT, Path TEXT)")
        connection.execute(
            "INSERT INTO Audiobooks VALUES ('1', ?, 'Ghost Stories', '/audiobooks/')", (OWN,)
        )
        for path in linked:
            connection.execute("INSERT INTO AudiobookFiles VALUES ('1', ?)", (path,))
        connection.commit()
        connection.close()
        return db

    def _manifest(self, tmp_path: pathlib.Path) -> pathlib.Path:
        path = tmp_path / "manifest.json"
        path.write_text(
            json.dumps(
                manifest_of(
                    ("M. R. James/Ghost Stories/Ghost Stories.m4b", OWN, "Ghost Stories"),
                    ("Henry James/Screw/Screw.m4b", OTHER, "The Turn of the Screw"),
                )
            )
        )
        return path

    def test_exit_code_is_failure_when_a_foreign_file_was_claimed(
        self, tmp_path: pathlib.Path
    ) -> None:
        db = self._db(
            tmp_path,
            [
                "/audiobooks/M. R. James/Ghost Stories/Ghost Stories.m4b",
                "/audiobooks/Henry James/Screw/Screw.m4b",
            ],
        )
        code = main(
            ["--manifest", str(self._manifest(tmp_path)), "--db", str(db), "--book-id", "1"]
        )
        assert code == EXIT_FAIL

    def test_exit_code_is_pass_for_a_correct_scan(self, tmp_path: pathlib.Path) -> None:
        db = self._db(tmp_path, ["/audiobooks/M. R. James/Ghost Stories/Ghost Stories.m4b"])
        code = main(
            ["--manifest", str(self._manifest(tmp_path)), "--db", str(db), "--book-id", "1"]
        )
        assert code == EXIT_PASS

    def test_unreadable_manifest_is_inconclusive(self, tmp_path: pathlib.Path) -> None:
        db = self._db(tmp_path, [])
        code = main(
            ["--manifest", str(tmp_path / "nope.json"), "--db", str(db), "--book-id", "1"]
        )
        assert code == EXIT_INCONCLUSIVE

    def test_json_output_records_the_verdict(self, tmp_path: pathlib.Path) -> None:
        db = self._db(
            tmp_path,
            [
                "/audiobooks/M. R. James/Ghost Stories/Ghost Stories.m4b",
                "/audiobooks/Henry James/Screw/Screw.m4b",
            ],
        )
        out = tmp_path / "report.json"
        main(
            [
                "--manifest", str(self._manifest(tmp_path)), "--db", str(db),
                "--book-id", "1", "--json", str(out),
            ]
        )
        payload = json.loads(out.read_text())
        assert payload["verdict"] == "fail"
        assert payload["foreign_files"] == 1
        assert payload["foreign_books"] == {"The Turn of the Screw": 1}


class TestScript:
    """Container-free guards on the driver itself."""

    SCRIPT = ROOT / "tools" / "validate_scan_attribution.sh"

    def test_script_is_syntactically_valid(self) -> None:
        result = subprocess.run(["bash", "-n", str(self.SCRIPT)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr

    def test_asin_is_required(self) -> None:
        result = subprocess.run(["bash", str(self.SCRIPT)], capture_output=True, text=True)
        assert result.returncode != 0
        assert "--asin is required" in (result.stderr + result.stdout)

    def test_unknown_argument_is_refused(self) -> None:
        result = subprocess.run(
            ["bash", str(self.SCRIPT), "--nonsense"], capture_output=True, text=True
        )
        assert result.returncode != 0
