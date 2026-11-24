"""Decide where each unique file lands under Files/, and record it in ``blobs``."""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass

from drivecon.catalog import Catalog
from drivecon.config import PathRules
from drivecon.paths import (
    Candidate,
    best_candidate,
    conflict_name,
    is_camera_dump,
    limit_length,
    profile_prefixes,
    profile_roots,
    sanitize_path,
    strip_prefix,
)
from drivecon.rules import split_path


@dataclass(frozen=True)
class Placement:
    dest_path: str  # relative to the destination root, e.g. "Files/finance/w2.pdf"
    renamed: bool  # sanitized or shortened
    conflict: bool  # same path as a different file; source and date were added
    fits: bool  # False only for pathological paths that could not be shortened enough


def source_profile_roots(cat: Catalog, source_id: int) -> list[tuple[str, ...]]:
    """Copied user profiles on a source, judged by the folders the scan saw.

    Uses the folders of kept files and of excluded ones (AppData, junk-only folders), so
    a profile is recognized even when only some of its folders hold documents.
    """
    dirs: set[tuple[str, ...]] = set()
    for (rel,) in cat.conn.execute("SELECT rel_path FROM files WHERE source_id=?", (source_id,)):
        dirs.add(tuple(split_path(rel)[:-1]))
    for (rel,) in cat.conn.execute(
        "SELECT rel_path FROM excluded_dirs WHERE source_id=?", (source_id,)
    ):
        dirs.add(tuple(split_path(rel)))
    return profile_roots(dirs)


class Placer:
    def __init__(self, cat: Catalog, rules: PathRules) -> None:
        self.cat, self.rules = cat, rules
        self._auto: dict[int, list[str]] = {}  # source_id -> detected profile prefixes

    def _auto_prefixes(self, source_id: int) -> list[str]:
        if source_id not in self._auto:
            self._auto[source_id] = profile_prefixes(source_profile_roots(self.cat, source_id))
        return self._auto[source_id]

    def _is_taken(self, path: str) -> bool:
        row = self.cat.conn.execute(
            "SELECT 1 FROM blobs WHERE lower(dest_path)=lower(?)", (path,)
        ).fetchone()
        return row is not None

    def place(self, hash_: str, exif_date: str | None = None) -> Placement:
        """Compute and record the destination for ``hash_`` (idempotent). Does not commit."""
        conn = self.cat.conn
        row = conn.execute(
            "SELECT dest_path, renamed FROM blobs WHERE hash=? AND dest_path IS NOT NULL",
            (hash_,),
        ).fetchone()
        if row:
            return Placement(row[0], bool(row[1]), False, True)

        rows = conn.execute(
            "SELECT f.id, f.source_id, s.label, s.interface, s.strip_prefixes, f.rel_path,"
            " f.size, f.mtime, f.ext FROM files f JOIN sources s ON s.id = f.source_id"
            " WHERE f.hash=? AND f.status IN ('hashed','duplicate','copied')"
            " AND f.decision IS NOT 'exclude' ORDER BY f.id",
            (hash_,),
        ).fetchall()
        if not rows:
            raise LookupError(f"no hashed files for {hash_}")
        cands: list[Candidate] = []
        meta: dict[int, tuple[str, int]] = {}  # source_id -> (interface, size)
        for fid, sid, label, iface, extra, rel, size, mtime, _ext in rows:
            prefixes = [*json.loads(extra or "[]"), *self._auto_prefixes(sid),
                        *self.rules.strip_prefixes]  # fmt: skip
            segs = strip_prefix(split_path(rel), prefixes)
            conn.execute("UPDATE files SET logical_path=? WHERE id=?", ("/".join(segs), fid))
            cands.append(Candidate(sid, label, tuple(segs), mtime or 0.0))
            meta[sid] = (iface or "", size or 0)
        best = best_candidate(cands)
        oldest = min(c.mtime for c in cands)
        size = rows[0][6] or 0
        ext = (rows[0][8] or "").lower()
        original = "/".join(best.logical)

        segs = list(best.logical)
        if is_camera_dump(segs, self.rules.photos, ext, meta[best.source_id][0] == "sd"):
            when = _photo_date(exif_date, oldest)
            segs = ["Photos", f"{when.year:04}", f"{when.month:02}", segs[-1]]

        intended = ["Files", *segs]
        full, fits = limit_length(["Files", *sanitize_path(segs)])
        renamed = full != intended  # sanitized or shortened (conflict naming is separate)
        plain = "/".join(full)
        conflict = self._is_taken(plain)
        if conflict:
            dirpath = "/".join(full[:-1])
            name = conflict_name(full[-1], best.label, best.mtime, self._is_taken, dirpath)
            full, fits = limit_length([*full[:-1], name])
        dest = "/".join(full)
        conn.execute(
            "INSERT INTO blobs (hash, size, ext, dest_path, original_name, renamed,"
            " oldest_mtime, exif_date) VALUES (?,?,?,?,?,?,?,?)"
            " ON CONFLICT(hash) DO UPDATE SET dest_path=excluded.dest_path,"
            " original_name=excluded.original_name, renamed=excluded.renamed,"
            " oldest_mtime=excluded.oldest_mtime, exif_date=excluded.exif_date",
            (hash_, size, ext, dest, original, int(renamed), oldest, exif_date),
        )
        if conflict:
            conn.execute(
                "INSERT INTO conflicts (dest_path, hash, chosen_name, source_id) VALUES (?,?,?,?)",
                (plain, hash_, dest, best.source_id),
            )
        return Placement(dest, renamed, conflict, fits)


def _photo_date(exif_date: str | None, oldest_mtime: float) -> dt.datetime:
    if exif_date:
        try:
            return dt.datetime.fromisoformat(exif_date)
        except ValueError:
            pass
    return dt.datetime.fromtimestamp(oldest_mtime, dt.UTC)
