"""Copy each unique file once into Files/: temp name, fsync, re-hash, rename."""

from __future__ import annotations

import datetime as dt
import errno
import os
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import blake3

from drivecon import fault
from drivecon.catalog import Catalog
from drivecon.console import CONSOLE, PROGRESS_SECS, Console
from drivecon.hashing import CHUNK, hash_file
from drivecon.placement import Placer

TMP_PREFIX = ".dc-"
TMP_SUFFIX = ".tmp"


class DestinationFull(RuntimeError):
    """The destination ran out of space; resume after freeing some."""


class SourceChanged(RuntimeError):
    """The source file no longer matches the hash recorded for it."""


class DestCollision(RuntimeError):
    """A different file already sits at the chosen destination path."""


def tmp_name(hash_: str) -> str:
    return f"{TMP_PREFIX}{hash_[:20]}{TMP_SUFFIX}"


def sweep_tmp(dest_root: Path) -> int:
    """Remove leftover temp files from an interrupted run."""
    n = 0
    files = dest_root / "Files"
    if files.is_dir():
        for dirpath, _dirs, names in os.walk(files):
            for name in names:
                if name.startswith(TMP_PREFIX) and name.endswith(TMP_SUFFIX):
                    (Path(dirpath) / name).unlink()
                    n += 1
    return n


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def copy_blob(
    src: Path, final: Path, expected: str, mtime: float, fault_prefix: str = "copy"
) -> None:
    """Copy ``src`` to ``final`` safely. A crash never leaves a half-written file under
    its final name."""
    final.parent.mkdir(parents=True, exist_ok=True)
    tmp = final.parent / tmp_name(expected)
    try:
        h = blake3.blake3()
        with open(src, "rb") as fin, open(tmp, "wb") as fout:
            while chunk := fin.read(CHUNK):
                h.update(chunk)
                fout.write(chunk)
            fault.checkpoint(f"{fault_prefix}.after_temp_write")
            fout.flush()
            os.fsync(fout.fileno())
        fault.checkpoint(f"{fault_prefix}.after_fsync")
        if h.hexdigest() != expected:
            raise SourceChanged(str(src))
        if hash_file(tmp)[0] != expected:
            raise OSError(errno.EIO, "re-hash of the copy does not match", str(tmp))
        os.utime(tmp, (mtime, mtime))
        os.replace(tmp, final)
        fault.checkpoint(f"{fault_prefix}.after_rename")
        _fsync_dir(final.parent)
    except OSError as e:
        if e.errno == errno.ENOSPC:
            raise DestinationFull(str(final)) from e
        raise
    finally:
        tmp.unlink(missing_ok=True)


@dataclass
class CopyStats:
    copied: int = 0
    bytes: int = 0
    duplicates: int = 0
    errors: int = 0
    changed: int = 0


# --- batched durability ------------------------------------------------------------
#
# Small files used to cost one fsync each, plus a directory fsync after each rename. On a
# USB disk through an NTFS driver that is a few files a second. Copies are now made durable
# in batches: write up to BATCH_FILES temp files (or BATCH_BYTES), make them durable, re-hash
# and rename each, make the renames durable, then record them in the catalog. The guarantee
# is unchanged: a file only ever gets its final name after its bytes are on disk, and the
# catalog only says "copied" after the rename is on disk.
#
# "fs" mode makes a batch durable with one syncfs() of the destination filesystem. It is
# used only where that is known to flush to the device: local block filesystems. FUSE
# (ntfs-3g), drvfs (WSL), 9p and network shares use "file" mode: an fsync per file and per
# folder, as before, still batched for the catalog.

BATCH_FILES = 100
BATCH_BYTES = 256 * 1000 * 1000
SYNCFS_FS = frozenset({"ntfs3", "ext2", "ext3", "ext4", "xfs", "btrfs", "f2fs", "vfat", "exfat"})


def fs_type(path: Path) -> str:
    """Filesystem type of the mount holding ``path`` ('' when it cannot be told)."""
    try:
        target = os.path.realpath(path)
        best, kind = "", ""
        with open("/proc/self/mounts", encoding="utf-8") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 3:
                    continue
                mnt = parts[1].replace("\\040", " ")
                inside = target == mnt or target.startswith(mnt.rstrip("/") + "/")
                if inside and len(mnt) >= len(best):
                    best, kind = mnt, parts[2]
        return kind
    except OSError:
        return ""


def _syncfs(path: Path) -> bool:
    try:
        import ctypes
        import ctypes.util

        libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        fn = libc.syncfs
    except (OSError, AttributeError):
        return False
    fd = os.open(path, os.O_RDONLY)
    try:
        return bool(fn(fd) == 0)
    finally:
        os.close(fd)


def sync_mode(dest_root: Path) -> str:
    """'fs' or 'file'; CONSOLIDATE_SYNC=fs|file overrides (tests)."""
    forced = os.environ.get("CONSOLIDATE_SYNC", "")
    if forced in ("fs", "file"):
        return forced
    return "fs" if fs_type(dest_root) in SYNCFS_FS else "file"


def _make_durable(mode: str, dest_root: Path, files: list[Path], dirs: set[Path]) -> None:
    if mode == "fs" and _syncfs(dest_root):
        return
    for f in files:
        try:
            fd = os.open(f, os.O_RDWR)
        except OSError:
            continue  # already renamed or removed; its directory is synced below
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    for d in dirs:
        _fsync_dir(d)


@dataclass
class _Staged:
    hash: str
    rows: list[tuple[int, bytes, str, float]]
    row: tuple[int, bytes, str, float]  # the row whose source was copied
    tmp: Path
    final: Path
    mtime: float
    size: int


def copy_source(
    cat: Catalog,
    source_id: int,
    root: Path,
    placer: Placer,
    dest_root: Path,
    *,
    exif: Callable[[Path], str | None] = lambda p: None,
    photo_exts: frozenset[str] = frozenset(),
    stop: threading.Event | None = None,
    progress: Callable[[CopyStats], None] | None = None,
    progress_every: int = 1000,
    throttle: float = 0.0,
    batch: int = BATCH_FILES,
    progress_secs: float = PROGRESS_SECS,
    console: Console = CONSOLE,
) -> CopyStats:
    """Place and copy every hashed file of one source. Resumable at any point."""
    import time

    stop = stop or threading.Event()
    stats = CopyStats()
    sweep_tmp(dest_root)
    mode = sync_mode(dest_root)
    conn = cat.conn
    with cat.lock:
        hashes = [
            r[0]
            for r in conn.execute(
                "SELECT hash FROM files WHERE source_id=? AND status='hashed'"
                " GROUP BY hash ORDER BY MIN(id)",
                (source_id,),
            )
        ]
    staged: list[_Staged] = []
    staged_bytes = 0
    pending = 0  # hashes handled since the last catalog commit
    last_progress = time.monotonic()

    def flush() -> None:
        nonlocal staged, staged_bytes, pending
        batch_now, staged, staged_bytes, pending = staged, [], 0, 0
        try:
            _finish_batch(cat, source_id, batch_now, mode, dest_root, stats)
        finally:
            for st in batch_now:
                st.tmp.unlink(missing_ok=True)
        cat.commit()

    try:
        for h in hashes:
            if stop.is_set():
                break
            if throttle:
                time.sleep(throttle)
            with cat.lock:
                done = conn.execute(
                    "SELECT 1 FROM blobs WHERE hash=? AND copied_at IS NOT NULL", (h,)
                ).fetchone()
                rows = conn.execute(
                    "SELECT id, rel_path_raw, ext, mtime FROM files"
                    " WHERE source_id=? AND hash=? AND status='hashed' ORDER BY id",
                    (source_id, h),
                ).fetchall()
            if done:
                with cat.lock:
                    conn.execute(
                        "UPDATE files SET status='duplicate'"
                        " WHERE source_id=? AND hash=? AND status='hashed'",
                        (source_id, h),
                    )
                stats.duplicates += len(rows)
            elif rows:
                console.working_on("copy", os.fsdecode(rows[0][1]))
                try:
                    st = _stage_one(cat, source_id, root, placer, dest_root, h, rows, exif,
                                    photo_exts, stats)  # fmt: skip
                except DestinationFull:
                    # The temps already staged are complete: keep them before stopping.
                    # Discarding them would lose the whole batch, and a destination with
                    # less free space than one batch would then never make progress.
                    flush()
                    raise
                if st is not None:
                    staged.append(st)
                    staged_bytes += st.size
            pending += 1
            if pending >= batch or staged_bytes >= BATCH_BYTES:
                flush()
            now = time.monotonic()
            due = (stats.copied + stats.duplicates) % progress_every == 0
            if progress and (due or now - last_progress >= progress_secs):
                last_progress = now
                progress(stats)
        if staged or pending:
            flush()
    finally:
        for st in staged:  # interrupted or failed: never left under a temp name
            st.tmp.unlink(missing_ok=True)
        cat.commit()
    return stats


def _stage_one(
    cat: Catalog,
    source_id: int,
    root: Path,
    placer: Placer,
    dest_root: Path,
    h: str,
    rows: list[tuple[int, bytes, str, float]],
    exif: Callable[[Path], str | None],
    photo_exts: frozenset[str],
    stats: CopyStats,
) -> _Staged | None:
    """Place ``h`` and write it to a temp file beside its final path (not yet durable)."""
    conn = cat.conn
    first_path = root / os.fsdecode(rows[0][1])
    exif_date = exif(first_path) if (rows[0][2] or "").lower() in photo_exts else None
    with cat.lock:
        placement = placer.place(h, exif_date)
        oldest = conn.execute("SELECT oldest_mtime FROM blobs WHERE hash=?", (h,)).fetchone()[0]
    final = dest_root / placement.dest_path
    if final.exists():
        # A crash after the rename but before the catalog commit leaves a good copy.
        if hash_file(final)[0] != h:
            _fail(cat, rows, "dest-collision: " + placement.dest_path, stats)
            return None
        _record_copied(cat, source_id, h, rows, rows[0], final.stat().st_size, stats)
        return None
    for row in rows:
        src = root / os.fsdecode(row[1])
        try:
            tmp, size = _write_tmp(src, final, h)
        except SourceChanged:
            with cat.lock:
                conn.execute("UPDATE files SET status='seen', hash=NULL WHERE id=?", (row[0],))
                _drop_unreferenced_blob(conn, h)
            stats.changed += 1
            continue
        except DestinationFull:
            raise
        except OSError as e:
            with cat.lock:
                conn.execute(
                    "UPDATE files SET status='error', error=? WHERE id=?", (str(e), row[0])
                )
            stats.errors += 1
            continue
        return _Staged(h, rows, row, tmp, final, oldest, size)
    return None


def _write_tmp(src: Path, final: Path, expected: str) -> tuple[Path, int]:
    """Copy ``src`` beside ``final`` under a temp name, checking the bytes read on the way."""
    final.parent.mkdir(parents=True, exist_ok=True)
    tmp = final.parent / tmp_name(expected)
    n = 0
    try:
        h = blake3.blake3()
        with open(src, "rb") as fin, open(tmp, "wb") as fout:
            while chunk := fin.read(CHUNK):
                h.update(chunk)
                fout.write(chunk)
                n += len(chunk)
            fault.checkpoint("copy.after_temp_write")
        if h.hexdigest() != expected:
            raise SourceChanged(str(src))
    except BaseException as e:
        tmp.unlink(missing_ok=True)
        if isinstance(e, OSError) and e.errno == errno.ENOSPC:
            raise DestinationFull(str(final)) from e
        raise
    return tmp, n


def _finish_batch(
    cat: Catalog, source_id: int, staged: list[_Staged], mode: str, dest_root: Path,
    stats: CopyStats,
) -> None:  # fmt: skip
    """Durable bytes, then re-hash and rename, then durable names, then the catalog."""
    if not staged:
        return
    try:
        _make_durable(mode, dest_root, [st.tmp for st in staged], set())
    except OSError as e:
        if e.errno == errno.ENOSPC:
            raise DestinationFull(str(dest_root)) from e
        raise
    renamed: list[_Staged] = []
    for st in staged:
        fault.checkpoint("copy.after_fsync")
        try:
            if hash_file(st.tmp)[0] != st.hash:
                raise OSError(errno.EIO, "re-hash of the copy does not match", str(st.tmp))
            os.utime(st.tmp, (st.mtime, st.mtime))
            os.replace(st.tmp, st.final)
        except OSError as e:
            st.tmp.unlink(missing_ok=True)
            if e.errno == errno.ENOSPC:  # keep what is already in place, then stop
                _record_renamed(cat, source_id, renamed, mode, dest_root, stats)
                raise DestinationFull(str(st.final)) from e
            with cat.lock:
                cat.conn.execute(
                    "UPDATE files SET status='error', error=? WHERE id=?", (str(e), st.row[0])
                )
            stats.errors += 1
            continue
        fault.checkpoint("copy.after_rename")
        renamed.append(st)
    _record_renamed(cat, source_id, renamed, mode, dest_root, stats)


def _record_renamed(
    cat: Catalog, source_id: int, renamed: list[_Staged], mode: str, dest_root: Path,
    stats: CopyStats,
) -> None:  # fmt: skip
    _make_durable(mode, dest_root, [], {st.final.parent for st in renamed})
    for st in renamed:
        _record_copied(cat, source_id, st.hash, st.rows, st.row, st.final.stat().st_size, stats)


def _record_copied(
    cat: Catalog,
    source_id: int,
    h: str,
    rows: list[tuple[int, bytes, str, float]],
    copied_row: tuple[int, bytes, str, float],
    size: int,
    stats: CopyStats,
) -> None:
    conn = cat.conn
    now = cat.clock().isoformat()
    with cat.lock:
        conn.execute(
            "UPDATE blobs SET copied_at=?, verified_at=?, size=? WHERE hash=?", (now, now, size, h)
        )
        # Mark the row at the chosen best path as the copied one, if it is in this source.
        best = conn.execute(
            "SELECT id FROM files WHERE source_id=? AND hash=? AND status='hashed'"
            " AND logical_path=(SELECT original_name FROM blobs WHERE hash=?) ORDER BY id",
            (source_id, h, h),
        ).fetchone()
        conn.execute("UPDATE files SET status='copied' WHERE id=?", ((best or copied_row)[0],))
        conn.execute(
            "UPDATE files SET status='duplicate' WHERE source_id=? AND hash=? AND status='hashed'",
            (source_id, h),
        )
    stats.copied += 1
    stats.duplicates += len(rows) - 1
    stats.bytes += size


def _drop_unreferenced_blob(conn: sqlite3.Connection, h: str) -> None:
    """Free the destination path of a hash no file refers to any more (never copied)."""
    live = conn.execute(
        "SELECT 1 FROM files WHERE hash=? AND status IN ('hashed','duplicate','copied')", (h,)
    ).fetchone()
    if not live:
        conn.execute("DELETE FROM blobs WHERE hash=? AND copied_at IS NULL", (h,))
        conn.execute("DELETE FROM conflicts WHERE hash=?", (h,))


def _fail(
    cat: Catalog, rows: list[tuple[int, bytes, str, float]], msg: str, stats: CopyStats
) -> None:
    with cat.lock:
        for r in rows:
            cat.conn.execute("UPDATE files SET status='error', error=? WHERE id=?", (msg, r[0]))
    stats.errors += len(rows)


@dataclass
class VerifyResult:
    checked: int = 0
    missing: int = 0
    corrupt: int = 0
    orphans: int = 0
    tmp_removed: int = 0

    @property
    def ok(self) -> bool:
        return not (self.missing or self.corrupt or self.orphans)


def verify(cat: Catalog, dest_root: Path, now: dt.datetime) -> VerifyResult:
    """Re-hash every copied file against the catalog; report missing, corrupt and orphans."""
    res = VerifyResult(tmp_removed=sweep_tmp(dest_root))
    known: set[str] = set()
    with cat.lock:
        rows = cat.conn.execute(
            "SELECT hash, dest_path FROM blobs WHERE copied_at IS NOT NULL ORDER BY dest_path"
        ).fetchall()
    for h, rel in rows:
        known.add(rel.lower())
        path = dest_root / rel
        res.checked += 1
        if not path.is_file():
            res.missing += 1
            print(f"MISSING  {rel}")
        elif hash_file(path)[0] != h:
            res.corrupt += 1
            print(f"CORRUPT  {rel}")
        else:
            with cat.lock:
                cat.conn.execute(
                    "UPDATE blobs SET verified_at=? WHERE hash=?", (now.isoformat(), h)
                )
    files = dest_root / "Files"
    if files.is_dir():
        for dirpath, _d, names in os.walk(files):
            for name in names:
                rel = (Path(dirpath) / name).relative_to(dest_root).as_posix()
                if rel.lower() not in known:
                    res.orphans += 1
                    print(f"ORPHAN   {rel}")
    cat.commit()
    return res
