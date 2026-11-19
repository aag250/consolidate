"""The secondary drive: a second, independent NTFS copy kept in step with the primary.

The secondary is registered once by its NTFS volume serial. ``sync_mirror`` copies
everything new (files, intake sheets, reports, a fresh catalog copy), each file via a temp
name, fsync, re-hash against the catalog and rename. It never deletes: files that should no
longer be there are listed in a report and removed only with ``prune``.
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import os
import shutil
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from drivecon import fault
from drivecon.catalog import Catalog, _atomic_copy_db
from drivecon.copier import DestinationFull, SourceChanged, VerifyResult, copy_blob, sweep_tmp
from drivecon.hashing import hash_file

MARKER = ".drivecon-mirror.json"


class MirrorError(RuntimeError):
    """The secondary is missing, unregistered or the wrong drive."""


@dataclass
class Mirror:
    id: int
    label: str
    serial: str
    root: Path


def register_mirror(cat: Catalog, root: Path, label: str, serial: str) -> Mirror:
    """Register a secondary by its volume serial and mark the drive so it is recognized."""
    if not root.is_dir():
        raise MirrorError(f"{root} is not mounted")
    marker = root / MARKER
    if marker.exists() and json.loads(marker.read_text()).get("serial") != serial:
        raise MirrorError(f"{root} already belongs to another mirror registration")
    row = cat.conn.execute(
        "SELECT id, label FROM mirrors WHERE volume_serial=?", (serial,)
    ).fetchone()
    if row:
        mid = int(row[0])
    else:
        with cat.lock:
            cur = cat.conn.execute(
                "INSERT INTO mirrors (label, volume_serial, registered_at) VALUES (?,?,?)",
                (label, serial, cat.clock().isoformat()),
            )
            assert cur.lastrowid is not None
            mid = cur.lastrowid
            cat.commit()
    marker.write_text(json.dumps({"serial": serial, "label": label}))
    return Mirror(mid, label, serial, root)


def open_mirror(cat: Catalog, root: Path) -> Mirror:
    """Identify the drive mounted at ``root``; refuse if it is not a registered secondary."""
    marker = root / MARKER
    if not root.is_dir() or not marker.is_file():
        raise MirrorError(f"{root} is not a registered secondary (is it plugged in and mounted?)")
    serial = json.loads(marker.read_text()).get("serial", "")
    row = cat.conn.execute(
        "SELECT id, label FROM mirrors WHERE volume_serial=?", (serial,)
    ).fetchone()
    if not row:
        raise MirrorError(f"drive {serial!r} at {root} is not registered in this catalog")
    return Mirror(int(row[0]), row[1], serial, root)


def behind(cat: Catalog, mirror_id: int) -> tuple[int, int]:
    """(files, bytes) on the primary that the secondary does not have yet."""
    row = cat.conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(size), 0) FROM blobs b WHERE b.copied_at IS NOT NULL"
        " AND NOT EXISTS (SELECT 1 FROM mirror_blobs m WHERE m.mirror_id=? AND m.hash=b.hash)",
        (mirror_id,),
    ).fetchone()
    return int(row[0]), int(row[1])


@dataclass
class SyncStats:
    copied: int = 0
    bytes: int = 0
    skipped_existing: int = 0
    errors: list[str] = field(default_factory=list)
    extras: list[str] = field(default_factory=list)
    pruned: int = 0
    small_files: int = 0


def _walk_files(root: Path) -> list[str]:
    out = []
    base = root / "Files"
    if base.is_dir():
        for dirpath, _d, names in os.walk(base):
            for n in names:
                out.append((Path(dirpath) / n).relative_to(root).as_posix())
    return out


def find_extras(cat: Catalog, mirror: Mirror) -> list[str]:
    """Files on the secondary that the catalog no longer expects there."""
    wanted = {
        r[0].lower()
        for r in cat.conn.execute("SELECT dest_path FROM blobs WHERE copied_at IS NOT NULL")
    }
    return sorted(p for p in _walk_files(mirror.root) if p.lower() not in wanted)


def _copy_small(primary: Path, mirror: Path, folder: str) -> int:
    """Copy intake sheets or reports across (atomic, only if different)."""
    n = 0
    src_dir = primary / folder
    if not src_dir.is_dir():
        return 0
    for src in sorted(p for p in src_dir.iterdir() if p.is_file() and not p.name.endswith(".tmp")):
        dst = mirror / folder / src.name
        if dst.exists() and hash_file(dst)[0] == hash_file(src)[0]:
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(dst.name + ".tmp")
        shutil.copyfile(src, tmp)
        with open(tmp, "rb") as f:
            os.fsync(f.fileno())
        os.replace(tmp, dst)
        n += 1
    return n


def sync_mirror(
    cat: Catalog,
    primary: Path,
    mirror: Mirror,
    *,
    stop: threading.Event | None = None,
    progress: Callable[[SyncStats], None] | None = None,
    progress_every: int = 1000,
    batch: int = 100,
    prune: Callable[[list[str]], bool] | None = None,
) -> SyncStats:
    """Bring the secondary up to date. Resumable at any point; never deletes unless
    ``prune`` is given and confirms."""
    stop = stop or threading.Event()
    stats = SyncStats()
    sweep_tmp(mirror.root)
    with cat.lock:
        todo = cat.conn.execute(
            "SELECT b.hash, b.dest_path, COALESCE(b.oldest_mtime, 0) FROM blobs b"
            " WHERE b.copied_at IS NOT NULL AND NOT EXISTS (SELECT 1 FROM mirror_blobs m"
            " WHERE m.mirror_id=? AND m.hash=b.hash) ORDER BY b.dest_path",
            (mirror.id,),
        ).fetchall()
    pending = 0
    try:
        for h, rel, mtime in todo:
            if stop.is_set():
                break
            final = mirror.root / rel
            if final.exists():
                if hash_file(final)[0] != h:
                    stats.errors.append(f"differs on secondary, not overwritten: {rel}")
                    continue
                stats.skipped_existing += 1
            else:
                try:
                    copy_blob(primary / rel, final, h, mtime, fault_prefix="sync")
                except SourceChanged:
                    stats.errors.append(f"primary copy no longer matches the catalog: {rel}")
                    continue
                except FileNotFoundError:
                    stats.errors.append(f"missing on primary: {rel}")
                    continue
                except DestinationFull:
                    raise
                stats.copied += 1
                stats.bytes += final.stat().st_size
            with cat.lock:
                cat.conn.execute(
                    "INSERT OR REPLACE INTO mirror_blobs (mirror_id, hash, synced_at)"
                    " VALUES (?,?,?)",
                    (mirror.id, h, cat.clock().isoformat()),
                )
            pending += 1
            if progress and (stats.copied + stats.skipped_existing) % progress_every == 0:
                progress(stats)
            if pending >= batch:
                cat.commit()
                pending = 0
    finally:
        cat.commit()
    if stop.is_set():
        return stats

    stats.extras = find_extras(cat, mirror)
    if stats.extras and prune is not None and prune(stats.extras):
        for rel in stats.extras:
            (mirror.root / rel).unlink(missing_ok=True)
            stats.pruned += 1
        stats.extras = find_extras(cat, mirror)
    _write_extras_report(primary, stats.extras)
    stats.small_files = _copy_small(primary, mirror.root, "_intake") + _copy_small(
        primary, mirror.root, "_reports"
    )
    with cat.lock:
        cat.conn.execute(
            "UPDATE mirrors SET last_sync_at=? WHERE id=?", (cat.clock().isoformat(), mirror.id)
        )
        cat.commit()
        fault.checkpoint("sync.before_catalog_copy")
        _atomic_copy_db(cat.conn, mirror.root / "_catalog" / "catalog.sqlite")
    return stats


def _write_extras_report(primary: Path, extras: list[str]) -> None:
    out = primary / "_reports" / "mirror-extra.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["path_on_secondary"])
        w.writerows([[e] for e in extras])
    os.replace(tmp, out)


def verify_mirror(cat: Catalog, mirror: Mirror, now: dt.datetime) -> VerifyResult:
    """Re-hash every file on the secondary against the catalog."""
    res = VerifyResult(tmp_removed=sweep_tmp(mirror.root))
    rows = cat.conn.execute(
        "SELECT b.hash, b.dest_path FROM blobs b JOIN mirror_blobs m ON m.hash=b.hash"
        " WHERE m.mirror_id=? ORDER BY b.dest_path",
        (mirror.id,),
    ).fetchall()
    for h, rel in rows:
        res.checked += 1
        path = mirror.root / rel
        if not path.is_file():
            res.missing += 1
            print(f"MISSING  {rel}")
        elif hash_file(path)[0] != h:
            res.corrupt += 1
            print(f"CORRUPT  {rel}")
    extras = find_extras(cat, mirror)
    for rel in extras:
        res.orphans += 1
        print(f"EXTRA    {rel}")
    return res
