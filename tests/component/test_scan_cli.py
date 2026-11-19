import json
import shutil
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from drivecon.cli import app
from drivecon.mockdevices import build_tree, load_spec

ROOT = Path(__file__).parent.parent.parent
runner = CliRunner()


class Setup:
    def __init__(self, tmp: Path) -> None:
        self.dest, self.config, self.sources = tmp / "dest", tmp / "config", tmp / "sources"
        shutil.copytree(ROOT / "config", self.config)
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
