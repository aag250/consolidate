"""Kill or interrupt the real CLI at every named crash point, resume, and compare the
result with an uninterrupted run."""

import hashlib
import os
import random
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from drivecon.mockdevices import build_tree

ROOT = Path(__file__).parent.parent.parent

M1: list[dict[str, Any]] = [
    {"path": f"Users/u1/Documents/proj/doc{i:02}.docx", "key": f"d{i}", "size": 200 + i}
    for i in range(14)
] + [
    {"path": "Users/u1/Documents/finance/w2.pdf", "key": "w2", "size": 500},
    {"path": "Users/u1/Desktop/w2.pdf", "key": "w2", "size": 500},
    {"path": "Users/u1/Documents/budget.xlsx", "key": "b1", "size": 90},
    {"path": "Users/u1/Documents/a:b?.txt", "content": "hostile name"},
    {"path": "Users/u1/Documents/CON.txt", "content": "reserved"},
    {"path": "Users/u1/Documents/data.weird", "size": 30},
    {"path": "Windows/x.dll", "size": 9},
    {"path": "Users/u1/AppData/Roaming/skip.docx", "size": 9},
]
M2: list[dict[str, Any]] = [
    {"path": "Backup 2012/finance/w2.pdf", "key": "w2", "size": 500},
    {"path": "Backup 2012/budget.xlsx", "key": "b2", "size": 91},
    {"path": "Backup 2012/proj/doc00.docx", "key": "d0", "size": 200},
    {"path": "Backup 2012/proj/new1.docx", "key": "n1", "size": 120},
    {"path": "Backup 2012/proj/new2.docx", "key": "n2", "size": 121},
]


def tree_digest(root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        if p.is_file():
            h.update(p.relative_to(root).as_posix().encode() + p.read_bytes())
    return h.hexdigest()


class Case:
    def __init__(self, base: Path, shared: Path, wsl: bool = False) -> None:
        self.dest, self.work = base / "dest", base / "work"
        self.config, self.sources = shared / "config", shared / "sources"
        self.env = {
            **os.environ,
            "DRIVECON_DEST": str(self.dest),
            "DRIVECON_CONFIG": str(self.config),
            "DRIVECON_SOURCES": str(self.sources),
            "CONSOLIDATE_BATCH": "3",
        }
        if wsl:
            self.env |= {"DRIVECON_CATALOG_MODE": "wsl", "DRIVECON_WORK_DIR": str(self.work)}
        for label in ("M1", "M2"):
            self.cli("intake", "--label", label, "--source-dir", str(self.sources / label))

    def cli(
        self, *args: str, fault: str | None = None, **extra: str
    ) -> subprocess.CompletedProcess[str]:
        env = {**self.env, **extra}
        if fault:
            env["CONSOLIDATE_FAULT"] = fault
        return subprocess.run(
            [sys.executable, "-m", "drivecon", *args],
            env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120,
        )  # fmt: skip

    def finish(self, label: str, fault: str | None = None) -> int:
        """Run ``label`` (with a fault on the first try), re-running until it completes."""
        first = self.cli("run", label, fault=fault)
        rc = first.returncode
        for _ in range(6):
            if rc == 0:
                break
            rc = self.cli("run", label).returncode
        assert rc == 0, "run never completed"
        return first.returncode

    def snapshot(self) -> dict[str, object]:
        files = {
            p.relative_to(self.dest).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (self.dest / "Files").rglob("*")
            if p.is_file()
        }
        conn = sqlite3.connect(self.dest / "_catalog" / "catalog.sqlite")
        try:
            q = conn.execute
            return {
                "files": files,
                "rows": q(
                    "SELECT s.label, f.rel_path, f.status, f.hash FROM files f"
                    " JOIN sources s ON s.id=f.source_id ORDER BY 1, 2"
                ).fetchall(),
                "blobs": q(
                    "SELECT hash, dest_path, copied_at IS NOT NULL FROM blobs ORDER BY hash"
                ).fetchall(),
                "conflicts": q(
                    "SELECT dest_path, hash, chosen_name FROM conflicts ORDER BY 3"
                ).fetchall(),
                "pending": q(
                    "SELECT ext, file_count FROM pending_extensions ORDER BY ext"
                ).fetchall(),
            }
        finally:
            conn.close()

    def check_invariants(self) -> None:
        assert not list((self.dest / "Files").rglob(".dc-*")), "temporary files remain"
        res = self.cli("verify")  # every copied row has its file and re-hashes; no orphans
        assert res.returncode == 0, res.stdout + res.stderr


@pytest.fixture(scope="module")
def shared(tmp_path_factory: pytest.TempPathFactory) -> Path:
    d = tmp_path_factory.mktemp("shared")
    shutil.copytree(ROOT / "config", d / "config")
    build_tree({"seed": 11, "files": M1}, d / "sources" / "M1")
    build_tree({"seed": 11, "files": M2}, d / "sources" / "M2")
    return d


@pytest.fixture(scope="module")
def golden(shared: Path, tmp_path_factory: pytest.TempPathFactory) -> dict[str, object]:
    c = Case(tmp_path_factory.mktemp("golden"), shared)
    c.finish("M1")
    c.finish("M2")
    c.check_invariants()
    snap = c.snapshot()
    assert len(snap["files"]) > 15 and snap["conflicts"]  # type: ignore[arg-type]
    return snap


CHECKPOINTS = [
    ("scan.batch_commit", 2),
    ("hash.after_hash", 2),
    ("copy.after_temp_write", 3),
    ("copy.after_fsync", 3),
    ("copy.after_rename", 3),
    ("catalog.after_commit", 4),
]


@pytest.mark.system
@pytest.mark.parametrize("action", ["kill", "sigint"])
@pytest.mark.parametrize(("checkpoint", "n"), CHECKPOINTS)
def test_crash_in_first_source_then_resume_matches_uninterrupted_run(
    shared: Path, golden: dict[str, object], tmp_path: Path, checkpoint: str, n: int, action: str
) -> None:
    before = tree_digest(shared / "sources")
    c = Case(tmp_path, shared)
    assert c.finish("M1", fault=f"{checkpoint}:{n}:{action}") != 0, "fault never fired"
    c.finish("M2")
    c.check_invariants()
    assert c.snapshot() == golden
    assert tree_digest(shared / "sources") == before  # sources are byte-identical


@pytest.mark.system
@pytest.mark.parametrize(
    "fault",
    ["hash.after_hash:1:kill", "copy.after_temp_write:2:kill", "copy.after_rename:2:sigint"],
)
def test_crash_in_second_source_then_resume_matches_uninterrupted_run(
    shared: Path, golden: dict[str, object], tmp_path: Path, fault: str
) -> None:
    c = Case(tmp_path, shared)
    c.finish("M1")
    assert c.finish("M2", fault=fault) != 0, "fault never fired"
    c.check_invariants()
    assert c.snapshot() == golden


@pytest.mark.system
@pytest.mark.parametrize("fault", ["catalog.sync_copy:3:kill", "catalog.after_commit:5:kill"])
def test_wsl_catalog_mode_survives_crashes(
    shared: Path, golden: dict[str, object], tmp_path: Path, fault: str
) -> None:
    c = Case(tmp_path, shared, wsl=True)
    assert c.finish("M1", fault=fault) != 0, "fault never fired"
    c.finish("M2")
    c.check_invariants()
    assert c.snapshot() == golden


@pytest.mark.system
@pytest.mark.parametrize("seed", [1, 2, 3])
def test_random_kills_then_resume_match_uninterrupted_run(
    shared: Path, golden: dict[str, object], tmp_path: Path, seed: int
) -> None:
    rng = random.Random(seed)
    c = Case(tmp_path, shared)
    env = {**c.env, "CONSOLIDATE_THROTTLE": "0.01"}
    for label in ("M1", "M2"):
        for _ in range(3):
            proc = subprocess.Popen(
                [sys.executable, "-m", "drivecon", "run", label],
                env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )  # fmt: skip
            time.sleep(rng.uniform(0.6, 1.8))
            proc.kill()
            proc.wait()
        c.finish(label)
    c.check_invariants()
    assert c.snapshot() == golden
