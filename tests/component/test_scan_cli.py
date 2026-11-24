import json
import shutil
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from drivecon.cli import app
from drivecon.mockdevices import build_tree, load_spec
from tests.conftest import use_ask_mode

ROOT = Path(__file__).parent.parent.parent
runner = CliRunner()


class Setup:
    def __init__(self, tmp: Path, ask: bool = True) -> None:
        self.dest, self.config, self.sources = tmp / "dest", tmp / "config", tmp / "sources"
        shutil.copytree(ROOT / "config", self.config)
        if ask:
            use_ask_mode(self.config)
        build_tree(load_spec(ROOT / "tests/fixtures/devices/win7.yaml"), self.sources / "M1")
        self.common = ["--dest", str(self.dest)]
        res = runner.invoke(
            app, ["intake", "--label", "M1", "--source-dir", str(self.sources / "M1"), *self.common]
        )
        assert res.exit_code == 0, res.output

    def run(self, *args: str, input: str | None = None) -> Any:
        return runner.invoke(app, list(args), input=input)

    def scan(self) -> Any:
        return self.run("scan", "M1", *self.common, "--config", str(self.config),
                        "--sources-root", str(self.sources))  # fmt: skip

    def db(self) -> sqlite3.Connection:
        return sqlite3.connect(self.dest / "_catalog" / "catalog.sqlite")


@pytest.fixture
def cli(tmp_path: Path) -> Setup:
    return Setup(tmp_path)


@pytest.mark.component
def test_scan_without_a_terminal_queues_and_report_types_lists_it(cli: Setup) -> None:
    res = cli.scan()
    assert res.exit_code == 0 and "held for unknown extensions" in res.output
    rep = cli.run("report", "types", *cli.common)
    assert ".weird" in rep.output and "2 files" in rep.output


@pytest.mark.component
def test_decide_answers_queue_and_releases(cli: Setup) -> None:
    cli.scan()
    res = cli.run("decide", *cli.common, "--config", str(cli.config), input="i\n")
    assert "Released 2 held files" in res.output
    assert "weird" in (cli.config / "file-types.toml").read_text()
    assert cli.run("report", "types", *cli.common).output.strip() == "No pending extensions."
    status = cli.db().execute("SELECT DISTINCT status FROM files WHERE ext='weird'").fetchall()
    assert status == [("seen",)]


@pytest.mark.component
def test_decide_later_leaves_the_queue(cli: Setup) -> None:
    cli.scan()
    cli.run("decide", *cli.common, "--config", str(cli.config), input="x\nd\n")
    assert ".weird" in cli.run("report", "types", *cli.common).output


@pytest.mark.component
def test_dry_run_writes_nothing_to_the_real_catalog_or_files(cli: Setup) -> None:
    before = cli.db().execute("SELECT COUNT(*) FROM files").fetchone()[0]
    res = cli.run("dry-run", "M1", "--config", str(cli.config), "--sources-root", str(cli.sources))
    assert "included 3" in res.output and "unknown .weird: 2 files" in res.output
    assert cli.db().execute("SELECT COUNT(*) FROM files").fetchone()[0] == before == 0
    assert not (cli.dest / "Files").exists()


@pytest.mark.component
def test_scan_of_unregistered_label_fails(cli: Setup) -> None:
    res = cli.run("scan", "NOPE", *cli.common, "--config", str(cli.config))
    assert res.exit_code != 0 and "run intake first" in res.output


@pytest.mark.component
def test_catalog_json_smart_is_none_for_mock(cli: Setup) -> None:
    row = cli.db().execute("SELECT smart_status, smart_json FROM sources").fetchone()
    assert row == ("unavailable", json.dumps(None))


@pytest.mark.component
def test_unknown_extensions_are_skipped_listed_and_can_be_included_later(tmp_path: Path) -> None:
    import csv

    cli = Setup(tmp_path, ask=False)
    paths = ["--config", str(cli.config), "--sources-root", str(cli.sources)]
    res = cli.run("run", "M1", *cli.common, *paths)
    assert res.exit_code == 0, res.output
    assert "Unknown extension" not in res.output  # never asks
    assert "Skipped 2 files" in res.output and ".weird" in res.output
    assert "M1: safe to retire" in res.output
    rep = cli.run("report", "unknown", *cli.common)
    assert "M1" in rep.output and ".weird" in rep.output and "2 files" in rep.output
    rows = list(csv.reader((cli.dest / "_reports" / "unknown-excluded.csv").open()))
    assert rows[0] == ["drive", "extension", "files", "bytes", "examples"] and rows[1][:3] == [
        "M1",
        "weird",
        "2",
    ]
    sheet = (cli.dest / "_intake" / "M1.txt").read_text()
    assert "Unknown types skipped: 2 files, 1 extensions" in sheet

    inc = cli.run("include-ext", ".WEIRD", "--config", str(cli.config), *cli.common)
    assert inc.exit_code == 0 and "Re-run these drives to copy the files: M1" in inc.output
    again = cli.run("run", "M1", *cli.common, *paths)
    assert again.exit_code == 0 and "Skipped" not in again.output
    copied = cli.db().execute("SELECT COUNT(*) FROM files WHERE ext='weird' AND status='copied'")
    assert copied.fetchone() == (2,)
    assert cli.run("report", "unknown", *cli.common).output.startswith("No unknown extensions")


@pytest.mark.component
def test_switching_to_exclude_mode_releases_files_held_by_an_earlier_scan(tmp_path: Path) -> None:
    cli = Setup(tmp_path)  # ask mode first: .weird files are held
    cli.scan()
    held = cli.db().execute("SELECT COUNT(*) FROM files WHERE status='pending_decision'")
    assert held.fetchone() == (2,)
    text = (cli.config / "file-types.toml").read_text()
    (cli.config / "file-types.toml").write_text(
        text.replace('unknown = "ask"', 'unknown = "exclude"', 1)
    )
    res = cli.scan()
    assert "dropped" in res.output
    held = cli.db().execute("SELECT COUNT(*) FROM files WHERE status='pending_decision'")
    assert held.fetchone() == (0,)
