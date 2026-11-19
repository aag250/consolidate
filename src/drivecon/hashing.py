"""BLAKE3 hashing of every included file, run in a worker alongside the scan."""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import blake3

from drivecon import fault
from drivecon.catalog import Catalog

CHUNK = 1 << 20


def hash_file(path: str | Path) -> tuple[str, int]:
    """BLAKE3 hex digest and the number of bytes actually read."""
    h = blake3.blake3()
    n = 0
    with open(path, "rb") as f:
        while chunk := f.read(CHUNK):
            h.update(chunk)
            n += len(chunk)
    return h.hexdigest(), n


@dataclass
class HashStats:
    hashed: int = 0
    bytes: int = 0
    errors: int = 0


class HashWorker:
    """Hash rows in status 'seen' until the scan is done and none are left."""

    def __init__(
        self,
        cat: Catalog,
        source_id: int,
        root: Path,
        *,
        stop: threading.Event | None = None,
        progress: Callable[[HashStats], None] | None = None,
        progress_every: int = 1000,
        throttle: float = 0.0,
        batch: int = 100,
    ) -> None:
        self.cat, self.sid, self.root = cat, source_id, root
        self.stop = stop or threading.Event()
        self.progress, self.progress_every, self.throttle = progress, progress_every, throttle
        self.batch = batch
        self.scan_done = threading.Event()
        self.stats = HashStats()

    def run(self) -> HashStats:
        while not self.stop.is_set():
            with self.cat.lock:
                rows = self.cat.conn.execute(
                    "SELECT id, rel_path_raw, size FROM files WHERE source_id=? AND status='seen'"
                    " ORDER BY id LIMIT ?",
                    (self.sid, self.batch),
                ).fetchall()
            if not rows:
                if self.scan_done.is_set():
                    break
                time.sleep(0.05)
                continue
            results = [self._hash_one(*r) for r in rows]
            if self.stop.is_set() and not any(results):
                break
            with self.cat.lock:
                for res in results:
                    if res:
                        self.cat.conn.execute(*res)
                fault.checkpoint("hash.after_hash")
                self.cat.commit()
        return self.stats

    def _hash_one(
        self, fid: int, raw: bytes, size: int | None
    ) -> tuple[str, tuple[object, ...]] | None:
        if self.stop.is_set():
            return None
        if self.throttle:
            time.sleep(self.throttle)
        path = self.root / os.fsdecode(raw)
        try:
            digest, n = hash_file(path)
            st = os.stat(path)
        except OSError as e:
            self.stats.errors += 1
            return (
                "UPDATE files SET status='error', error=? WHERE id=? AND status='seen'",
                (str(e), fid),
            )
        self.stats.hashed += 1
        self.stats.bytes += n
        if self.progress and self.stats.hashed % self.progress_every == 0:
            self.progress(self.stats)
        # The file may have changed since the scan: trust what was actually read.
        return (
            "UPDATE files SET hash=?, status='hashed', size=?, mtime=?, error=NULL"
            " WHERE id=? AND status='seen'",
            (digest, n, st.st_mtime, fid),
        )
