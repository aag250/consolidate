"""Walk a mounted source read-only, classify every file and record it in the catalog."""

from __future__ import annotations

import datetime as dt
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from drivecon import fault
from drivecon.catalog import Catalog
from drivecon.pending import PendingTracker, refresh_pending
from drivecon.rules import Decision, Rules

# Content types libmagic reports for extension-less files, mapped to an extension.
MIME_EXT = {
    "application/pdf": "pdf",
    "text/plain": "txt",
    "text/html": "html",
    "text/xml": "xml",
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/tiff": "tif",
    "image/bmp": "bmp",
    "application/zip": "zip",
    "application/x-7z-compressed": "7z",
    "application/gzip": "gz",
    "application/msword": "doc",
    "application/vnd.ms-excel": "xls",
    "application/vnd.ms-powerpoint": "ppt",
    "audio/mpeg": "mp3",
    "video/mp4": "mp4",
    "application/x-dosexec": "exe",
    "application/x-executable": "exe",
    "application/x-sharedlib": "so",
}

Sniffer = Callable[[str], str]


def sniff_mime(path: str) -> str:
    import magic  # needs libmagic1

    return str(magic.from_file(path, mime=True))


@dataclass
class ScanStats:
    files_seen: int = 0
    included: int = 0
    included_bytes: int = 0
    pending: int = 0
    excluded_files: int = 0
    excluded_bytes: int = 0
    errors: int = 0


UPSERT = """
INSERT INTO files (source_id, rel_path_raw, rel_path, size, mtime, ctime, ext, mime, decision,
                   rule_matched, status, error, scanned_at)
VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
ON CONFLICT (source_id, rel_path_raw) DO UPDATE SET
  rel_path=excluded.rel_path, ext=excluded.ext, mime=excluded.mime,
  decision=excluded.decision, rule_matched=excluded.rule_matched, scanned_at=excluded.scanned_at,
  error=excluded.error,
  hash = CASE WHEN files.size IS excluded.size AND files.mtime IS excluded.mtime
              THEN files.hash ELSE NULL END,
  status = CASE WHEN files.size IS excluded.size AND files.mtime IS excluded.mtime
                     AND files.status IN ('hashed','copied','duplicate')
                     AND excluded.status = 'seen'
                THEN files.status ELSE excluded.status END,
  size=excluded.size, mtime=excluded.mtime, ctime=excluded.ctime
"""


class Scanner:
    def __init__(
        self,
        cat: Catalog,
        source_id: int,
        root: Path,
        rules: Rules,
        tracker: PendingTracker,
        *,
        sniffer: Sniffer = sniff_mime,
        batch_size: int = 1000,
        stop: threading.Event | None = None,
        progress: Callable[[ScanStats], None] | None = None,
        progress_every: int = 1000,
        throttle: float = 0.0,
    ) -> None:
        self.cat, self.sid, self.root, self.rules, self.tracker = (
            cat,
            source_id,
            root,
            rules,
            tracker,
        )
        self.sniffer, self.batch_size = sniffer, batch_size
        self.stop = stop or threading.Event()
        self.progress, self.progress_every, self.throttle = progress, progress_every, throttle
        self.stats = ScanStats()
        self._excluded: dict[tuple[str, str], list[int]] = {}
        self._uncommitted = 0
        self._now = cat.clock().isoformat()

    # --- walking ----------------------------------------------------------------------

    def run(self) -> ScanStats:
        with self.cat.lock:
            self.cat.conn.execute("DELETE FROM excluded_dirs WHERE source_id=?", (self.sid,))
        stack: list[list[str]] = [[]]
        while stack and not self.stop.is_set():
            segs = stack.pop()
            try:
                entries = sorted(os.scandir(self.root.joinpath(*segs)), key=lambda e: e.name)
            except OSError as e:
                self._record_error("/".join(segs) or ".", e)
                continue
            subdirs: list[list[str]] = []
            for entry in entries:
                if self.stop.is_set():
                    break
                child = [*segs, entry.name]
                try:
                    if entry.is_symlink() or not (entry.is_dir() or entry.is_file()):
                        self._count_excluded(segs, "non-regular", 0)
                    elif entry.is_dir():
                        pat = self.rules.excluded_dir(child)
                        if pat:
                            self._walk_excluded(child, pat)
                        else:
                            subdirs.append(child)
                    else:
                        self._file(entry, child)
                except OSError as e:
                    self._record_error("/".join(child), e)
            stack.extend(reversed(subdirs))
        with self.cat.lock:
            self._flush_excluded()
            refresh_pending(self.cat, self.cat.clock())
            if not self.stop.is_set():
                self.cat.conn.execute(
                    "UPDATE sources SET last_scan_at=? WHERE id=?", (self._now, self.sid)
                )
            self._commit()
        return self.stats

    def _walk_excluded(self, top: list[str], pattern: str) -> None:
        """Count a pruned tree, rescuing only files that a path exception includes."""
        stack = [top]
        while stack and not self.stop.is_set():
            segs = stack.pop()
            try:
                entries = sorted(os.scandir(self.root.joinpath(*segs)), key=lambda e: e.name)
            except OSError as e:
                self._record_error("/".join(segs), e)
                continue
            for entry in entries:
                child = [*segs, entry.name]
                try:
                    if entry.is_symlink():
                        continue
                    if entry.is_dir():
                        stack.append(child)
                        continue
                    size = entry.stat(follow_symlinks=False).st_size
                    d = self.rules.classify("/".join(child), size)
                    if d.rule.startswith("exception:"):
                        self._file(entry, child)
                    else:
                        self._count_excluded(top, pattern, size)
                except OSError as e:
                    self._record_error("/".join(child), e)

    # --- recording --------------------------------------------------------------------

    def _file(self, entry: os.DirEntry[str], segs: list[str]) -> None:
        if self.throttle:
            time.sleep(self.throttle)
        rel = "/".join(segs)
        st = entry.stat(follow_symlinks=False)
        self.stats.files_seen += 1
        d = self.rules.classify(rel, st.st_size)
        mime = ""
        if d.action == "sniff":
            mime = self.sniffer(entry.path)
            ext = MIME_EXT.get(mime)
            d = (
                self.rules.resolve_ext(ext, "sniffed:")
                if ext
                else self.rules.resolve_ext(f"mime:{mime.lower()}")
            )
        if d.action in ("include", "pending"):
            self._upsert(rel, st, d, mime)
        else:
            self._count_excluded(segs[:-1], d.rule, st.st_size)
        if self.progress and self.stats.files_seen % self.progress_every == 0:
            self.progress(self.stats)

    def _upsert(self, rel: str, st: os.stat_result, d: Decision, mime: str) -> None:
        pending = d.action == "pending"
        with self.cat.lock:
            self.cat.conn.execute(
                UPSERT,
                (
                    self.sid, os.fsencode(rel), rel.encode("utf-8", "replace").decode(),
                    st.st_size, st.st_mtime, st.st_ctime, d.ext, mime or None, d.action,
                    d.rule, "pending_decision" if pending else "seen", None, self._now,
                ),
            )  # fmt: skip
            self._uncommitted += 1
            if self._uncommitted >= self.batch_size:
                self._commit()
        if pending:
            self.stats.pending += 1
            self.tracker.add(d.ext, rel, st.st_size)
        else:
            self.stats.included += 1
            self.stats.included_bytes += st.st_size

    def _record_error(self, rel: str, err: OSError) -> None:
        self.stats.errors += 1
        with self.cat.lock:
            self.cat.conn.execute(
                UPSERT,
                (self.sid, os.fsencode(rel), rel.encode("utf-8", "replace").decode(),
                 None, None, None, "", None, "error", "read-error", "error", str(err), self._now),
            )  # fmt: skip
            self._uncommitted += 1

    def _count_excluded(self, dir_segs: list[str], rule: str, size: int) -> None:
        key = ("/".join(dir_segs), rule)
        slot = self._excluded.setdefault(key, [0, 0])
        slot[0] += 1
        slot[1] += size
        self.stats.excluded_files += 1
        self.stats.excluded_bytes += size

    def _flush_excluded(self) -> None:
        for (rel, rule), (count, total) in self._excluded.items():
            self.cat.conn.execute(
                "INSERT INTO excluded_dirs (source_id, rel_path, rule_matched, file_count,"
                " total_bytes) VALUES (?,?,?,?,?)",
                (self.sid, rel, rule, count, total),
            )

    def _commit(self) -> None:
        with self.cat.lock:
            fault.checkpoint("scan.batch_commit")
            refresh_pending(self.cat, self.cat.clock())
            self.cat.commit()
            self._uncommitted = 0


def default_now() -> dt.datetime:
    return dt.datetime.now()
