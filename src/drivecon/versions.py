"""Spot files that look like versions of one another: same name, same or similar folder,
different content. Both are always kept; this only suggests which might be newer."""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from drivecon.catalog import Catalog

MTIME_TOLERANCE = 2.0  # FAT stores modified times to 2 seconds
SIBLING_NAME_SIMILARITY = 0.8
PHOTO_PREFIX = "files/photos/"  # camera dumps reuse IMG_0001.jpg names: not versions


@dataclass(frozen=True)
class Member:
    hash: str
    dest_path: str
    size: int
    mtime: float
    drives: tuple[str, ...] = ()
    found_at: tuple[str, ...] = ()


@dataclass
class VersionGroup:
    name: str
    members: list[Member]  # newest first
    notes: list[str] = field(default_factory=list)

    @property
    def newest(self) -> Member:
        return self.members[0]


def has_newer_claim(g: VersionGroup) -> bool:
    """True when the dates support saying one file may be newer than the others."""
    return any("newer version" in n for n in g.notes)


def folders_similar(a: tuple[str, ...], b: tuple[str, ...]) -> bool:
    """Same folder; one inside the other; the same last folder name under different parents
    (``work/taxes``, ``old-pc/taxes``); or sibling folders with near-identical names
    (``plan``, ``plan-old``). Siblings like ``2011`` and ``2012`` are different documents."""
    if a == b:
        return True
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    if long_[: len(short)] == short:
        return True
    if a[-1] == b[-1]:
        return True
    if a[:-1] == b[:-1]:
        x, y = sorted((a[-1], b[-1]), key=len)
        if len(x) >= 3 and y.startswith(x):  # "plan" and "plan-old"
            return True
        return SequenceMatcher(None, x, y).ratio() >= SIBLING_NAME_SIMILARITY
    return False


def _folder(dest_path: str) -> tuple[str, ...]:
    parts = dest_path.lower().split("/")
    return tuple(parts[1:-1])  # drop "Files" and the file name


def _components(items: list[tuple[str, tuple[str, ...]]]) -> list[list[str]]:
    """Group blob hashes whose folders are (transitively) similar."""
    parent = {h: h for h, _ in items}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i, (h1, f1) in enumerate(items):
        for h2, f2 in items[i + 1 :]:
            if folders_similar(f1, f2):
                parent[find(h1)] = find(h2)
    out: dict[str, list[str]] = defaultdict(list)
    for h, _ in items:
        out[find(h)].append(h)
    return list(out.values())


def describe(group: list[Member]) -> list[str]:
    """Notes on the group, newest first. Never claims more than the dates and sizes show."""
    newest, oldest = group[0], group[-1]
    spread = newest.mtime - oldest.mtime
    if spread <= MTIME_TOLERANCE:
        if len({m.size for m in group}) == 1:
            return [
                "Same size and date but different content: possible corruption, or a "
                "same-size edit. Keep both and compare."
            ]
        return ["Same modified date but different sizes: cannot tell which is newer."]
    note = f"{_name(newest)} may be a newer version (modified {_day(newest.mtime)}"
    note += f", {newest.size} bytes) of {_name(oldest)} (modified {_day(oldest.mtime)}"
    note += f", {oldest.size} bytes)."
    return [note]


def _name(m: Member) -> str:
    return m.dest_path + (f" [{', '.join(m.drives)}]" if m.drives else "")


def _day(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.UTC).date().isoformat()


def find_version_groups(cat: Catalog) -> list[VersionGroup]:
    """Every set of copied files sharing a name, in similar folders, with different content."""
    by_name: dict[str, list[tuple[str, str, int, float]]] = defaultdict(list)
    for h, dest, original, size, mtime in cat.conn.execute(
        "SELECT hash, dest_path, COALESCE(original_name, dest_path), size,"
        " COALESCE(oldest_mtime, 0) FROM blobs WHERE copied_at IS NOT NULL ORDER BY dest_path"
    ):
        if dest.lower().startswith(PHOTO_PREFIX):
            continue
        by_name[original.rsplit("/", 1)[-1].lower()].append((h, dest, size, mtime))

    groups: list[VersionGroup] = []
    for name, blobs in sorted(by_name.items()):
        if len(blobs) < 2:
            continue
        info = {h: (dest, size, mtime) for h, dest, size, mtime in blobs}
        for comp in _components([(h, _folder(info[h][0])) for h in info]):
            if len(comp) < 2:
                continue
            members = [_member(cat, h, *info[h]) for h in comp]
            members.sort(key=lambda m: (-m.mtime, m.dest_path))
            groups.append(VersionGroup(name, members, describe(members)))
    groups.sort(key=lambda g: (g.name, g.members[0].dest_path))
    return groups


def _member(cat: Catalog, h: str, dest: str, size: int, mtime: float) -> Member:
    rows = cat.conn.execute(
        "SELECT s.label, f.rel_path FROM files f JOIN sources s ON s.id=f.source_id"
        " WHERE f.hash=? ORDER BY s.label, f.rel_path",
        (h,),
    ).fetchall()
    drives = tuple(dict.fromkeys(r[0] for r in rows))
    return Member(h, dest, size, mtime, drives, tuple(f"{r[0]}:{r[1]}" for r in rows))


def csv_rows(groups: list[VersionGroup]) -> list[tuple[object, ...]]:
    out: list[tuple[object, ...]] = []
    for n, g in enumerate(groups, 1):
        for rank, m in enumerate(g.members, 1):
            out.append(
                (n, g.name, rank, m.dest_path, m.size, _day(m.mtime), "; ".join(m.drives),
                 " ".join(g.notes) if rank == 1 else "")
            )  # fmt: skip
    return out


CSV_HEADER = ["group", "name", "rank_newest_first", "path", "bytes", "modified", "drives", "note"]
