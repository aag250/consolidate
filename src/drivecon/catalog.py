"""The SQLite catalog: schema, batched commits, dated backups and WSL2 working copy.

Native mode opens ``_catalog/catalog.sqlite`` on the destination directly, in
rollback-journal mode (never WAL: unreliable on NTFS and drvfs) with a single writer.

WSL2 mode keeps a working copy on WSL's own disk, because SQLite locking on drvfs is
unreliable. After every committed batch the working copy is written to the destination
(temp file, fsync, rename). On open, the copy with the higher ``generation`` wins, so a
destination drive moved between machines is always current enough to resume from.
"""

from __future__ import annotations

import datetime as dt
import os
import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from drivecon import fault

SCHEMA_VERSION = 1

# files.status moves seen -> hashed -> copied | duplicate; pending_decision holds files
# with an unknown extension; error marks read failures.
FILE_STATUSES = ("seen", "hashed", "copied", "duplicate", "pending_decision", "error")

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY,
    label TEXT NOT NULL UNIQUE,
    fs_uuid TEXT UNIQUE,
    fs_type TEXT, volume_label TEXT, device_model TEXT, serial TEXT, interface TEXT,
    size_bytes INTEGER, used_bytes INTEGER,
    smart_status TEXT, smart_json TEXT, strip_prefixes TEXT,
    intake_at TEXT, last_scan_at TEXT
);

CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sources(id),
    rel_path_raw BLOB NOT NULL,
    rel_path TEXT NOT NULL,
    logical_path TEXT,
    size INTEGER, mtime REAL, ctime REAL,
    ext TEXT, mime TEXT,
    decision TEXT, rule_matched TEXT,
    hash TEXT,
    status TEXT NOT NULL DEFAULT 'seen'
        CHECK (status IN ({", ".join(repr(s) for s in FILE_STATUSES)})),
    error TEXT,
    scanned_at TEXT,
    UNIQUE (source_id, rel_path_raw)
);
CREATE INDEX IF NOT EXISTS files_status ON files (source_id, status);
CREATE INDEX IF NOT EXISTS files_hash ON files (hash);

CREATE TABLE IF NOT EXISTS blobs (
    hash TEXT PRIMARY KEY,
    size INTEGER NOT NULL,
    ext TEXT, dest_path TEXT, original_name TEXT,
    renamed INTEGER NOT NULL DEFAULT 0,
    oldest_mtime REAL, exif_date TEXT, image_label TEXT,
    copied_at TEXT, verified_at TEXT
);

CREATE INDEX IF NOT EXISTS blobs_dest_ci ON blobs (lower(dest_path));

CREATE TABLE IF NOT EXISTS pending_extensions (
    ext TEXT PRIMARY KEY,
    first_seen TEXT, file_count INTEGER NOT NULL DEFAULT 0,
    total_bytes INTEGER NOT NULL DEFAULT 0,
    example_paths TEXT, decision TEXT, decided_at TEXT
);

CREATE TABLE IF NOT EXISTS conflicts (
    dest_path TEXT NOT NULL, hash TEXT NOT NULL, chosen_name TEXT NOT NULL,
    source_id INTEGER REFERENCES sources(id)
);

CREATE TABLE IF NOT EXISTS excluded_dirs (
    source_id INTEGER NOT NULL REFERENCES sources(id),
    rel_path TEXT NOT NULL, rule_matched TEXT,
    file_count INTEGER NOT NULL DEFAULT 0, total_bytes INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    command TEXT NOT NULL, source_id INTEGER REFERENCES sources(id),
    started_at TEXT NOT NULL, finished_at TEXT, counts TEXT
);
"""

Mode = Literal["native", "wsl"]


def _generation(path: Path) -> int:
    """Generation counter of a catalog file; -1 if the file is absent or unreadable."""
    if not path.exists():
        return -1
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            row = conn.execute("SELECT value FROM meta WHERE key='generation'").fetchone()
        finally:
            conn.close()
    except sqlite3.DatabaseError:
        return -1
    return int(row[0]) if row else 0


def _fsync_file(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_copy_db(src: sqlite3.Connection, dest: Path, hook: str | None = None) -> None:
    """Copy ``src`` to ``dest`` via a temp name, fsync, then rename into place."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    tmp.unlink(missing_ok=True)
    out = sqlite3.connect(tmp)
    try:
        src.backup(out)
    finally:
        out.close()
    _fsync_file(tmp)
    if hook:
        fault.checkpoint(hook)
    os.replace(tmp, dest)


class Catalog:
    def __init__(
        self,
        dest_dir: Path,
        mode: Mode = "native",
        work_dir: Path | None = None,
        clock: Callable[[], dt.datetime] = dt.datetime.now,
    ) -> None:
        if mode == "wsl" and work_dir is None:
            raise ValueError("wsl mode needs work_dir on a Linux filesystem")
        self.mode = mode
        self.clock = clock
        # Threads share one connection; hold this around every group of statements.
        self.lock = threading.RLock()
        self.dest_file = dest_dir / "_catalog" / "catalog.sqlite"
        self.backup_dir = dest_dir / "_catalog" / "backups"
        self.dest_file.parent.mkdir(parents=True, exist_ok=True)
        if mode == "wsl":
            assert work_dir is not None
            work_dir.mkdir(parents=True, exist_ok=True)
            self.db_file = work_dir / "catalog.sqlite"
            self._adopt_newer_copy()
        else:
            self.db_file = self.dest_file
        # One thread at a time uses the connection (the scan worker, then the main thread).
        self.conn = sqlite3.connect(self.db_file, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=DELETE")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()

    def _adopt_newer_copy(self) -> None:
        """On start, use the newer of the working copy and the copy on the destination."""
        if _generation(self.dest_file) > _generation(self.db_file):
            src = sqlite3.connect(f"file:{self.dest_file}?mode=ro", uri=True)
            tmp = self.db_file.with_name(self.db_file.name + ".tmp")
            tmp.unlink(missing_ok=True)
            out = sqlite3.connect(tmp)
            try:
                src.backup(out)
            finally:
                out.close()
                src.close()
            os.replace(tmp, self.db_file)

    def _init_schema(self) -> None:
        self.conn.executescript(SCHEMA)
        self.conn.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('generation', '0')")
        self.conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        self.conn.commit()

    @property
    def generation(self) -> int:
        row = self.conn.execute("SELECT value FROM meta WHERE key='generation'").fetchone()
        return int(row[0])

    def commit(self) -> None:
        """Commit the open batch, bump the generation and, in WSL mode, sync to the drive."""
        with self.lock:
            self.conn.execute(
                "UPDATE meta SET value = CAST(value AS INTEGER) + 1 WHERE key='generation'"
            )
            self.conn.commit()
            fault.checkpoint("catalog.after_commit")
            if self.mode == "wsl":
                self.sync_to_dest()

    def sync_to_dest(self) -> None:
        _atomic_copy_db(self.conn, self.dest_file, hook="catalog.sync_copy")

    @contextmanager
    def batch(self) -> Iterator[sqlite3.Connection]:
        """Run statements as one batch; commit on success, roll back on error."""
        try:
            yield self.conn
        except BaseException:
            self.conn.rollback()
            raise
        self.commit()

    def backup_dated(self) -> Path:
        """Write a dated copy to _catalog/backups/ (call after each drive)."""
        stamp = self.clock().strftime("%Y%m%d-%H%M%S")
        target = self.backup_dir / f"catalog-{stamp}.sqlite"
        _atomic_copy_db(self.conn, target)
        return target

    def close(self) -> None:
        if self.mode == "wsl":
            self.sync_to_dest()
        self.conn.close()

    # --- sources and runs -------------------------------------------------------------

    def register_source(self, label: str, fs_uuid: str | None = None, **fields: object) -> int:
        """Register a drive, or return the id of one already known by UUID."""
        if fs_uuid:
            row = self.conn.execute("SELECT id FROM sources WHERE fs_uuid=?", (fs_uuid,)).fetchone()
            if row:
                return int(row[0])
        cols = ["label", "fs_uuid", "intake_at", *fields]
        vals = [label, fs_uuid, self.clock().isoformat(), *fields.values()]
        marks = ", ".join("?" for _ in cols)
        cur = self.conn.execute(f"INSERT INTO sources ({', '.join(cols)}) VALUES ({marks})", vals)
        self.commit()
        assert cur.lastrowid is not None
        return cur.lastrowid

    def start_run(self, command: str, source_id: int | None = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO runs (command, source_id, started_at) VALUES (?, ?, ?)",
            (command, source_id, self.clock().isoformat()),
        )
        self.commit()
        assert cur.lastrowid is not None
        return cur.lastrowid

    def finish_run(self, run_id: int, counts: str) -> None:
        self.conn.execute(
            "UPDATE runs SET finished_at=?, counts=? WHERE id=?",
            (self.clock().isoformat(), counts, run_id),
        )
        self.commit()
