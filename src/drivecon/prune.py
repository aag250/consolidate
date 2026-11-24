"""Remove files from the backup that the rules now exclude everywhere they were found.

The tool never deletes on its own: a re-scan only marks rows whose file is now excluded
(see Scanner._reconcile). A copied file is a candidate here only when every place it was
found is marked excluded, so content that is also kept from somewhere else stays. Before
deleting, the file at the destination is re-hashed; anything that does not match the
catalog is left alone and reported.
"""

from __future__ import annotations

import csv
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from drivecon.catalog import Catalog
from drivecon.hashing import hash_file


@dataclass(frozen=True)
class Candidate:
    hash: str
    dest_path: str
    size: int
    found_at: str  # one original location, for the listing
    rule: str


@dataclass
class PruneResult:
    removed: int = 0
    bytes: int = 0
    missing: int = 0  # already gone from the destination: catalog cleaned up anyway
    mismatched: list[str] = field(default_factory=list)  # left alone: content differs
    dirs_removed: int = 0


def find_candidates(cat: Catalog) -> list[Candidate]:
    rows = cat.conn.execute(
        "SELECT b.hash, b.dest_path, b.size, MIN(s.label || ': ' || f.rel_path),"
        " MIN(f.rule_matched) FROM blobs b"
        " JOIN files f ON f.hash = b.hash JOIN sources s ON s.id = f.source_id"
        " WHERE b.copied_at IS NOT NULL AND b.dest_path IS NOT NULL"
        " GROUP BY b.hash HAVING SUM(f.decision IS NOT 'exclude') = 0"
        " ORDER BY b.dest_path"
    ).fetchall()
    return [Candidate(h, p, s or 0, at, rule or "") for h, p, s, at, rule in rows]


def write_listing(cands: list[Candidate], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["dest_path", "size", "found_at", "excluded_by"])
        for c in cands:
            w.writerow([c.dest_path, c.size, c.found_at, c.rule])


def _remove_empty_parents(path: Path, stop: Path) -> int:
    n = 0
    d = path.parent
    while d != stop and stop in d.parents:
        try:
            d.rmdir()  # only succeeds when empty
        except OSError:
            break
        n += 1
        d = d.parent
    return n


def prune(
    cat: Catalog,
    dest_root: Path,
    cands: list[Candidate],
    *,
    progress: Callable[[PruneResult], None] | None = None,
    batch: int = 200,
) -> PruneResult:
    res = PruneResult()
    files_root = dest_root / "Files"
    conn = cat.conn
    for i, c in enumerate(cands, 1):
        path = dest_root / c.dest_path
        if path.is_file():
            if hash_file(path)[0] != c.hash:
                res.mismatched.append(c.dest_path)
                continue
            path.unlink()
            res.dirs_removed += _remove_empty_parents(path, files_root)
            res.removed += 1
            res.bytes += c.size
        else:
            res.missing += 1
        with cat.lock:
            for table in ("files", "conflicts", "mirror_blobs", "blobs"):
                conn.execute(f"DELETE FROM {table} WHERE hash=?", (c.hash,))  # noqa: S608
        if i % batch == 0:
            cat.commit()
            if progress:
                progress(res)
    cat.commit()
    return res


def top_folders(cands: list[Candidate], depth: int = 3) -> list[tuple[str, int, int]]:
    agg: dict[str, list[int]] = {}
    for c in cands:
        key = "/".join(c.dest_path.split("/")[:depth])
        a = agg.setdefault(key, [0, 0])
        a[0] += 1
        a[1] += c.size
    return sorted(((k, n, b) for k, (n, b) in agg.items()), key=lambda r: -r[2])
