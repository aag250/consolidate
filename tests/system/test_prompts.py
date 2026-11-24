"""Drive the real CLI through a pseudo-terminal while a slowed scan is in progress."""

import os
import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pexpect
import pytest

from drivecon.mockdevices import build_tree
from tests.conftest import use_ask_mode

ROOT = Path(__file__).parent.parent.parent
N_DOCS = 150


class Run:
    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.dest, self.config, self.sources = tmp / "dest", tmp / "config", tmp / "sources"
        shutil.copytree(ROOT / "config", self.config)
        use_ask_mode(self.config)
        files = [{"path": f"a/x{i}.weird", "size": 8} for i in range(3)]
        files += [{"path": f"z/f{i:03}.docx", "size": 8, "key": str(i)} for i in range(N_DOCS)]
        build_tree({"seed": 3, "files": files}, self.sources / "M1")
        self.env = {
            **os.environ,
            "DRIVECON_DEST": str(self.dest),
            "DRIVECON_CONFIG": str(self.config),
            "DRIVECON_SOURCES": str(self.sources),
        }
        subprocess.run(
            [sys.executable, "-m", "drivecon", "intake", "--label", "M1",
             "--source-dir", str(self.sources / "M1")],
            env=self.env, check=True, capture_output=True,
        )  # fmt: skip

    def spawn(
        self, *args: str, slow: bool = True, env: dict[str, str] | None = None
    ) -> "pexpect.spawn[str]":
        env = {**self.env, **(env or {})}
        if slow:
            env |= {"CONSOLIDATE_THROTTLE": "0.02", "CONSOLIDATE_PROGRESS_EVERY": "10"}
        return pexpect.spawn(
            sys.executable, ["-m", "drivecon", *args], env=env, encoding="utf-8", timeout=30
        )

    def rows(self, sql: str) -> list[tuple[object, ...]]:
        conn = sqlite3.connect(self.dest / "_catalog" / "catalog.sqlite")
        try:
            return conn.execute(sql).fetchall()
        finally:
            conn.close()


@pytest.fixture
def run(tmp_path: Path) -> Run:
    return Run(tmp_path)


@pytest.mark.system
def test_progress_waits_while_prompt_is_open_and_the_question_is_repeated(run: Run) -> None:
    child = run.spawn("scan", "M1", env={"CONSOLIDATE_REMIND_EVERY": "1"})
    child.expect(r"Unknown extension '\.weird': \d+ files")
    child.expect(r"\[d\]ecide later")
    child.expect(r"Still waiting for your answer after \d+s: work continues in the background")
    assert "[scan]" not in str(child.before)  # progress never buries the open question
    child.expect(r"Unknown extension '\.weird'")  # the question is shown again
    child.expect(r"scanning finished; the run is waiting only for this answer", timeout=30)
    assert "[scan]" not in str(child.before)
    child.sendline("i")
    child.expect(r"\[scan\] (\d+) files")  # the latest progress, once answered
    m = child.match
    assert isinstance(m, re.Match) and int(m.group(1)) > 0
    child.expect(pexpect.EOF)
    child.close()
    assert child.exitstatus == 0
    assert 'ext = "weird"' in (run.config / "file-types.toml").read_text()
    assert run.rows("SELECT DISTINCT status FROM files WHERE ext='weird'") == [("seen",)]
    assert run.rows("SELECT COUNT(*) FROM files WHERE status='seen'") == [(N_DOCS + 3,)]


@pytest.mark.system
def test_exclude_answer(run: Run) -> None:
    child = run.spawn("scan", "M1")
    child.expect(r"\[d\]ecide later")
    child.sendline("e")
    child.expect(pexpect.EOF)
    child.close()
    assert run.rows("SELECT COUNT(*) FROM files WHERE ext='weird'") == [(0,)]


@pytest.mark.system
def test_sigint_while_prompt_open_stops_cleanly_and_later_runs_reach_golden(
    run: Run, tmp_path: Path
) -> None:
    child = run.spawn("scan", "M1")
    child.expect(r"\[d\]ecide later")
    child.sendintr()
    child.expect(pexpect.EOF, timeout=20)
    child.close()
    assert child.exitstatus == 130
    assert run.rows("SELECT ext FROM pending_extensions WHERE decision IS NULL") == [("weird",)]
    assert run.rows("SELECT COUNT(*) FROM files WHERE status='pending_decision'") != [(0,)]
    # Resume: scan again then decide; the result must equal an uninterrupted run.
    dec = run.spawn("decide")
    dec.expect(r"\[d\]ecide later")
    dec.sendline("i")
    dec.expect(pexpect.EOF)
    run.spawn("scan", "M1", slow=False).expect(pexpect.EOF)
    golden = Run(tmp_path / "golden")
    g = golden.spawn("scan", "M1")
    g.expect(r"\[d\]ecide later")
    g.sendline("i")
    g.expect(pexpect.EOF)
    q = "SELECT rel_path, status FROM files ORDER BY rel_path"
    assert run.rows(q) == golden.rows(q)


@pytest.mark.system
def test_no_terminal_queues_instead_of_blocking(run: Run) -> None:
    res = subprocess.run(
        [sys.executable, "-m", "drivecon", "scan", "M1"],
        env=run.env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60,
    )  # fmt: skip
    assert res.returncode == 0 and "run `decide`" in res.stdout
    assert re.search(r"held 3", res.stdout)
    assert run.rows("SELECT ext, file_count FROM pending_extensions") == [("weird", 3)]
