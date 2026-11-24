"""Pure path logic: merging trees across drives, NTFS-safe names and best-path scoring."""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

from drivecon.config import PhotoRules
from drivecon.rules import match_segments, split_path

# --- stripping -----------------------------------------------------------------------


def strip_prefix(segs: Sequence[str], patterns: Sequence[str], ci: bool = True) -> list[str]:
    """Remove drive letters, profile folders and backup wrappers from the front of a path.

    The file name is never removed. Patterns apply repeatedly, first match wins.
    """
    out = list(segs)
    if len(out) > 1 and re.fullmatch(r"[A-Za-z]:", out[0]):
        out = out[1:]
    parsed = [split_path(p) for p in patterns]
    changed = True
    while changed:
        changed = False
        for p in parsed:
            if p and len(p) < len(out) and match_segments(p, out[: len(p)], ci, whole=True):
                out = out[len(p) :]
                changed = True
                break
    return out


# Folder names a Windows user profile holds (Vista and later, plus XP). A folder holding
# at least PROFILE_MIN_FOLDERS of them is treated as a copied profile, whatever it is
# called ("KG 10.23.21 Backup", "old_laptop/joe"), and merged like Users/<name>/.
PROFILE_FOLDERS = frozenset(
    {
        "desktop", "documents", "downloads", "pictures", "music", "videos", "favorites",
        "contacts", "links", "saved games", "searches", "3d objects", "appdata", "onedrive",
        "my documents", "application data", "local settings", "start menu", "cookies",
    }
)  # fmt: skip
PROFILE_MIN_FOLDERS = 3


def profile_roots(dirs: Iterable[Sequence[str]]) -> list[tuple[str, ...]]:
    """Folders that look like a copied user profile, given every known directory path."""
    children: dict[tuple[str, ...], set[str]] = defaultdict(set)
    for d in dirs:
        for i in range(len(d)):
            children[tuple(d[:i])].add(d[i].lower())
    found = [
        p for p, kids in children.items() if len(kids & PROFILE_FOLDERS) >= PROFILE_MIN_FOLDERS
    ]
    return sorted(found, key=lambda p: (-len(p), p))  # deepest first: nested copies strip first


def _glob_escape(seg: str) -> str:
    return re.sub(r"([*?\[])", r"[\1]", seg)


def profile_prefixes(roots: Sequence[tuple[str, ...]]) -> list[str]:
    """Strip patterns for detected profiles: <root>/Documents/ (and My Documents/), then <root>/."""
    out: list[str] = []
    for r in roots:
        base = "".join(_glob_escape(s) + "/" for s in r)
        out += [base + "Documents/", base + "My Documents/"]
        if base:
            out.append(base)
    return out


# --- NTFS names ----------------------------------------------------------------------

FORBIDDEN = set('\\:*?"<>|')
RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10)),
    "COM¹", "COM²", "COM³", "LPT¹", "LPT²", "LPT³",
}  # fmt: skip
MAX_COMPONENT = 255  # UTF-16 code units
MAX_EXT = 16


def _units(s: str) -> int:
    return len(s.encode("utf-16-le", "surrogatepass")) // 2


def _truncate_units(s: str, limit: int) -> str:
    while _units(s) > limit:
        s = s[:-1]
    return s


def split_ext(name: str) -> tuple[str, str]:
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem or len(ext) > MAX_EXT:
        return name, ""
    return stem, "." + ext


def sanitize_segment(name: str) -> str:
    """Make one path component legal on NTFS: forbidden characters become ``_``,
    trailing dots and spaces are trimmed, reserved device names get a leading ``_``."""
    out = "".join("_" if c in FORBIDDEN or ord(c) < 32 else c for c in name)
    if _units(out) > MAX_COMPONENT - 1:  # leave room for a possible leading "_"
        stem, ext = split_ext(out)
        out = _truncate_units(stem, MAX_COMPONENT - 1 - _units(ext)) + ext
    out = out.rstrip(" .")
    if not out:
        return "_"
    if out.split(".")[0].rstrip(" ").upper() in RESERVED:
        out = "_" + out
    return out


def sanitize_path(segs: Sequence[str]) -> list[str]:
    return [sanitize_segment(s) for s in segs]


def shorten_name(name: str, target: int) -> str:
    """Shorten to about ``target`` characters, with a short hash so siblings stay distinct."""
    stem, ext = split_ext(name)
    h = hashlib.sha1(name.encode("utf-8", "surrogatepass")).hexdigest()[:4]
    keep = max(1, target - len(ext) - 5)
    return stem[:keep].rstrip(" .") + "~" + h + ext


MIN_FOLDER = 8


def path_length(segs: Sequence[str], prefix_len: int = 3) -> int:
    """Length of the Windows path ``X:\\a\\b\\c`` (prefix_len covers ``X:\\``)."""
    return prefix_len + sum(len(s) for s in segs) + max(0, len(segs) - 1)


def limit_length(
    segs: Sequence[str], limit: int = 240, prefix_len: int = 3
) -> tuple[list[str], bool]:
    """Keep the full path under ``limit`` characters, shortening the deepest folders first,
    then the file name. Returns (segments, fits)."""
    out = list(segs)
    while path_length(out, prefix_len) >= limit:
        excess = path_length(out, prefix_len) - (limit - 1)
        idx = next((i for i in range(len(out) - 2, -1, -1) if len(out[i]) > MIN_FOLDER), None)
        if idx is not None:
            out[idx] = shorten_name(out[idx], max(MIN_FOLDER, len(out[idx]) - excess))
            continue
        name = out[-1]
        stem, ext = split_ext(name)
        if len(stem) > MIN_FOLDER:
            out[-1] = shorten_name(name, max(MIN_FOLDER + len(ext), len(name) - excess))
            continue
        return out, False
    return out, True


# --- photos --------------------------------------------------------------------------


def is_camera_dump(segs: Sequence[str], photos: PhotoRules, ext: str, is_sd_card: bool) -> bool:
    """A stripped path that is a camera dump: DCIM/, 100CANON-style folders, Camera Roll/,
    or the root of an SD card."""
    if ext.lower() not in photos.extensions:
        return False
    if len(segs) == 1:
        return is_sd_card
    folders = {f.lower() for f in photos.camera_folders}
    rx = re.compile(photos.camera_folder_regex)
    return any(s.lower() in folders or rx.fullmatch(s) for s in segs[:-1])


# --- scoring -------------------------------------------------------------------------

MARKER = re.compile(r"(?<![A-Za-z])(backup|copy|old)(?![A-Za-z])|\(\d+\)", re.IGNORECASE)
POOR_FOLDERS = {"desktop", "downloads"}


@dataclass(frozen=True)
class Candidate:
    source_id: int
    label: str
    logical: tuple[str, ...]  # stripped path segments
    mtime: float


def score_key(c: Candidate, drives_seen: int) -> tuple[object, ...]:
    """Sort key; the smallest key is the best path.

    In order: a path seen on more than one drive, then deeper named folders over
    Desktop/, Downloads/ or the drive root, then no backup/copy/old/(1) in the name,
    then the oldest modified time."""
    named = len(c.logical) >= 2 and c.logical[0].lower() not in POOR_FOLDERS
    stem, _ = split_ext(c.logical[-1])
    return (
        -drives_seen,
        not named,
        -len(c.logical) if named else 0,
        bool(MARKER.search(stem)),
        c.mtime,
        tuple(s.lower() for s in c.logical),
        c.source_id,
    )


def best_candidate(cands: Sequence[Candidate]) -> Candidate:
    seen: dict[tuple[str, ...], set[int]] = {}
    for c in cands:
        seen.setdefault(tuple(s.lower() for s in c.logical), set()).add(c.source_id)
    return min(cands, key=lambda c: score_key(c, len(seen[tuple(s.lower() for s in c.logical)])))


# --- conflicts -----------------------------------------------------------------------


def conflict_name(
    name: str, label: str, mtime: float, is_taken: Callable[[str], bool], dirpath: str
) -> str:
    """``budget.xlsx`` -> ``budget (Dell-2009-C, 2011-03-04).xlsx``, numbered if still taken.

    ``is_taken`` is asked about full destination paths; it must compare case-insensitively,
    as NTFS does."""
    import datetime as dt

    day = dt.datetime.fromtimestamp(mtime, dt.UTC).date().isoformat()
    stem, ext = split_ext(name)
    base = f"{stem} ({label}, {day})"
    cand, n = base + ext, 1
    while is_taken(f"{dirpath}/{cand}"):
        n += 1
        cand = f"{base} {n}{ext}"
    return cand
