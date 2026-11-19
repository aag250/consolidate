"""Unknown-extension bookkeeping: live tracker, DB refresh and release of held files."""

from __future__ import annotations

import datetime as dt
import json
import threading
from dataclasses import dataclass, field

from drivecon.catalog import Catalog
from drivecon.rules import Rules

MAX_EXAMPLES = 3


@dataclass
class PendingInfo:
    ext: str
    count: int = 0
    total_bytes: int = 0
    examples: list[str] = field(default_factory=list)


class PendingTracker:
    """Thread-safe, in-memory view of extensions held during a scan, for live prompts."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: dict[str, PendingInfo] = {}
        self._order: list[str] = []
        self._asked: set[str] = set()

    def add(self, ext: str, path: str, size: int) -> None:
        with self._lock:
            info = self._data.get(ext)
            if info is None:
                info = self._data[ext] = PendingInfo(ext)
                self._order.append(ext)
            info.count += 1
            info.total_bytes += size
            if len(info.examples) < MAX_EXAMPLES:
                info.examples.append(path)

    def next_unasked(self) -> PendingInfo | None:
        with self._lock:
            for ext in self._order:
                if ext not in self._asked:
                    self._asked.add(ext)
                    i = self._data[ext]
                    return PendingInfo(i.ext, i.count, i.total_bytes, list(i.examples))
        return None

    def mark_asked(self, ext: str) -> None:
        with self._lock:
            self._asked.add(ext)

    def snapshot(self, ext: str) -> PendingInfo:
        with self._lock:
            i = self._data[ext]
            return PendingInfo(i.ext, i.count, i.total_bytes, list(i.examples))


def refresh_pending(cat: Catalog, now: dt.datetime) -> None:
    """Recompute pending_extensions from the files still held (the source of truth)."""
    conn = cat.conn
    rows = conn.execute(
        "SELECT ext, COUNT(*), SUM(size) FROM files WHERE status='pending_decision' GROUP BY ext"
    ).fetchall()
    held = set()
    for ext, count, total in rows:
        held.add(ext)
        examples = [
            r[0]
            for r in conn.execute(
                "SELECT rel_path FROM files WHERE status='pending_decision' AND ext=? "
                "ORDER BY id LIMIT ?",
                (ext, MAX_EXAMPLES),
            )
        ]
        conn.execute(
            "INSERT INTO pending_extensions (ext, first_seen, file_count, total_bytes,"
            " example_paths) VALUES (?,?,?,?,?) ON CONFLICT(ext) DO UPDATE SET"
            " file_count=excluded.file_count, total_bytes=excluded.total_bytes,"
            " example_paths=excluded.example_paths, decision=NULL, decided_at=NULL",
            (ext, now.isoformat(), count, total or 0, json.dumps(examples)),
        )
    for (ext,) in conn.execute("SELECT ext FROM pending_extensions WHERE decision IS NULL"):
        if ext not in held:
            conn.execute("DELETE FROM pending_extensions WHERE ext=?", (ext,))


def release_decided(cat: Catalog, rules: Rules, now: dt.datetime) -> int:
    """Process held files whose extension now has an answer. Returns files released.

    Include: the files go back to status ``seen``. Exclude: they are counted in
    excluded_dirs and removed from files.
    """
    conn = cat.conn
    released = 0
    exts = [
        r[0] for r in conn.execute("SELECT DISTINCT ext FROM files WHERE status='pending_decision'")
    ]
    for ext in exts:
        ftype = rules.file_type(ext)
        if ftype is None:
            continue
        decision, rule = ftype
        if decision == "include":
            cur = conn.execute(
                "UPDATE files SET status='seen', decision='include', rule_matched=? "
                "WHERE status='pending_decision' AND ext=?",
                (rule, ext),
            )
        else:
            agg: dict[tuple[int, str], list[int]] = {}
            for sid, rel, size in conn.execute(
                "SELECT source_id, rel_path, size FROM files "
                "WHERE status='pending_decision' AND ext=?",
                (ext,),
            ).fetchall():
                slot = agg.setdefault((sid, rel.rpartition("/")[0]), [0, 0])
                slot[0] += 1
                slot[1] += size or 0
            for (sid, d), (n, total) in agg.items():
                cur = conn.execute(
                    "UPDATE excluded_dirs SET file_count=file_count+?, total_bytes=total_bytes+?"
                    " WHERE source_id=? AND rel_path=? AND rule_matched=?",
                    (n, total, sid, d, rule),
                )
                if cur.rowcount == 0:
                    conn.execute(
                        "INSERT INTO excluded_dirs (source_id, rel_path, rule_matched,"
                        " file_count, total_bytes) VALUES (?,?,?,?,?)",
                        (sid, d, rule, n, total),
                    )
            cur = conn.execute(
                "DELETE FROM files WHERE status='pending_decision' AND ext=?", (ext,)
            )
        released += cur.rowcount
        conn.execute(
            "UPDATE pending_extensions SET decision=?, decided_at=? WHERE ext=?",
            (decision, now.isoformat(), ext),
        )
    return released
