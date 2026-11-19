import datetime as dt
import sqlite3
from pathlib import Path

import pytest

from drivecon import fault
from drivecon.catalog import Catalog

NOW = dt.datetime(2026, 11, 18, 12, 0, 0)
TABLES = {
    "sources",
    "files",
    "blobs",
    "pending_extensions",
    "conflicts",
    "excluded_dirs",
    "runs",
    "meta",
}


def make(tmp_path: Path, mode: str = "native", name: str = "dest") -> Catalog:
    return Catalog(
        tmp_path / name,
        mode=mode,  # type: ignore[arg-type]
        work_dir=tmp_path / "work" if mode == "wsl" else None,
        clock=lambda: NOW,
    )


@pytest.mark.component
def test_schema_and_rollback_journal(tmp_path: Path) -> None:
    cat = make(tmp_path)
    names = {r[0] for r in cat.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert names >= TABLES
    assert cat.conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    cat.close()


@pytest.mark.component
def test_reopen_is_idempotent_and_keeps_data(tmp_path: Path) -> None:
    cat = make(tmp_path)
    sid = cat.register_source("Dell-2009-C", fs_uuid="AAAA-1111", fs_type="ntfs")
    cat.close()
    cat = make(tmp_path)
    assert cat.register_source("renamed-label", fs_uuid="AAAA-1111") == sid
    assert cat.conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 1
    cat.close()


@pytest.mark.component
def test_batch_rolls_back_on_error_and_commits_on_success(tmp_path: Path) -> None:
    cat = make(tmp_path)
    sid = cat.register_source("a", fs_uuid="u1")
    with pytest.raises(RuntimeError), cat.batch() as c:
        c.execute(
            "INSERT INTO files (source_id, rel_path_raw, rel_path) VALUES (?, ?, ?)",
            (sid, b"x", "x"),
        )
        raise RuntimeError("boom")
    assert cat.conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 0
    gen = cat.generation
    with cat.batch() as c:
        c.execute(
            "INSERT INTO files (source_id, rel_path_raw, rel_path) VALUES (?, ?, ?)",
            (sid, b"x", "x"),
        )
    assert cat.generation == gen + 1
    cat.close()


@pytest.mark.component
def test_non_utf8_path_survives_as_raw_bytes(tmp_path: Path) -> None:
    cat = make(tmp_path)
    sid = cat.register_source("a", fs_uuid="u1")
    raw = "caf\xe9.txt".encode("latin-1")  # not valid UTF-8
    with cat.batch() as c:
        c.execute(
            "INSERT INTO files (source_id, rel_path_raw, rel_path) VALUES (?, ?, ?)",
            (sid, raw, raw.decode("utf-8", "replace")),
        )
    got = cat.conn.execute("SELECT rel_path_raw FROM files").fetchone()[0]
    assert got == raw
    cat.close()


@pytest.mark.component
def test_file_status_is_constrained(tmp_path: Path) -> None:
    cat = make(tmp_path)
    sid = cat.register_source("a", fs_uuid="u1")
    with pytest.raises(sqlite3.IntegrityError):
        cat.conn.execute(
            "INSERT INTO files (source_id, rel_path_raw, rel_path, status) VALUES (?,?,?,?)",
            (sid, b"x", "x", "bogus"),
        )
    cat.close()


@pytest.mark.component
def test_dated_backup(tmp_path: Path) -> None:
    cat = make(tmp_path)
    cat.register_source("a", fs_uuid="u1")
    target = cat.backup_dated()
    assert target.name == "catalog-20261118-120000.sqlite"
    assert sqlite3.connect(target).execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 1
    cat.close()


@pytest.mark.component
def test_wsl_mode_copies_to_destination_after_each_commit(tmp_path: Path) -> None:
    cat = make(tmp_path, "wsl")
    cat.register_source("a", fs_uuid="u1")
    dest = tmp_path / "dest" / "_catalog" / "catalog.sqlite"
    assert sqlite3.connect(dest).execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 1
    assert not list(dest.parent.glob("*.tmp"))
    cat.close()


@pytest.mark.component
def test_wsl_interrupted_between_commit_and_sync_resolves_to_newer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cat = make(tmp_path, "wsl")
    cat.register_source("a", fs_uuid="u1")
    fault.reset()
    monkeypatch.setenv("CONSOLIDATE_FAULT", "catalog.sync_copy:1:raise")
    with pytest.raises(fault.FaultInjected):
        cat.register_source("b", fs_uuid="u2")  # committed locally, copy to drive died
    cat.conn.close()
    monkeypatch.delenv("CONSOLIDATE_FAULT")
    dest = tmp_path / "dest" / "_catalog" / "catalog.sqlite"
    assert sqlite3.connect(dest).execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 1
    cat = make(tmp_path, "wsl")  # restart: the working copy is newer and wins
    assert cat.conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 2
    cat.close()  # and the drive copy is brought up to date at exit
    assert sqlite3.connect(dest).execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 2


@pytest.mark.component
def test_wsl_drive_moved_to_another_machine_uses_destination_copy(tmp_path: Path) -> None:
    cat = make(tmp_path, "wsl")
    cat.register_source("a", fs_uuid="u1")
    cat.close()
    other = Catalog(tmp_path / "dest", "wsl", tmp_path / "other-machine-work", lambda: NOW)
    assert other.conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 1
    other.register_source("b", fs_uuid="u2")
    other.close()
    # Back on the first machine its working copy is stale; the drive copy must win.
    again = make(tmp_path, "wsl")
    assert again.conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 2
    again.close()


@pytest.mark.component
def test_wsl_mode_requires_work_dir(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        Catalog(tmp_path, mode="wsl")


@pytest.mark.component
def test_runs_are_recorded(tmp_path: Path) -> None:
    cat = make(tmp_path)
    rid = cat.start_run("scan")
    cat.finish_run(rid, '{"files": 3}')
    row = cat.conn.execute("SELECT command, finished_at, counts FROM runs").fetchone()
    assert row == ("scan", NOW.isoformat(), '{"files": 3}')
    cat.close()
