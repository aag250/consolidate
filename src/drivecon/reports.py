"""Per-source results, the retire-safe verdict, `where` lookups and the CSV reports."""

from __future__ import annotations

import csv
import datetime as dt
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from drivecon.catalog import Catalog
from drivecon.intake import WIDTH, human
from drivecon.versions import CSV_HEADER, csv_rows, find_version_groups

HASH_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass
class SourceSummary:
    label: str
    scanned: bool
    included: int = 0
    excluded: int = 0
    new_unique: int = 0
    new_bytes: int = 0
    duplicates: int = 0
    held: int = 0
    errors: int = 0
    unfinished: int = 0  # included files not yet copied or recorded as duplicates
    reasons: list[str] = field(default_factory=list)

    @property
    def files_seen(self) -> int:
        return self.included + self.excluded

    @property
    def retire_safe(self) -> bool:
        return not self.reasons


def summarize(cat: Catalog, source_id: int) -> SourceSummary:
    """Everything this drive held that matters is in the destination, verified."""
    q = cat.conn.execute
    label, last_scan = q(
        "SELECT label, last_scan_at FROM sources WHERE id=?", (source_id,)
    ).fetchone()
    s = SourceSummary(label, last_scan is not None)
    counts = dict(
        q(
            "SELECT status, COUNT(*) FROM files WHERE source_id=?"
            " AND decision IS NOT 'exclude' GROUP BY status",
            (source_id,),
        )
    )
    s.included = sum(counts.values())
    s.excluded = q(
        "SELECT COALESCE(SUM(file_count),0) FROM excluded_dirs WHERE source_id=?", (source_id,)
    ).fetchone()[0]
    s.new_unique = counts.get("copied", 0)
    s.duplicates = counts.get("duplicate", 0)
    s.held = counts.get("pending_decision", 0)
    s.errors = counts.get("error", 0)
    s.unfinished = counts.get("seen", 0) + counts.get("hashed", 0)
    s.new_bytes = q(
        "SELECT COALESCE(SUM(size),0) FROM files WHERE source_id=? AND status='copied'"
        " AND decision IS NOT 'exclude'",
        (source_id,),
    ).fetchone()[0]
    unverified = q(
        "SELECT COUNT(*) FROM files f JOIN blobs b ON b.hash=f.hash WHERE f.source_id=?"
        " AND f.status IN ('copied','duplicate')"
        " AND (b.copied_at IS NULL OR b.verified_at IS NULL)",
        (source_id,),
    ).fetchone()[0]
    if not s.scanned:
        s.reasons.append("never fully scanned")
    if s.held:
        s.reasons.append(f"{s.held} files wait on unknown-extension decisions")
    if s.errors:
        s.reasons.append(f"{s.errors} files failed to read")
    if s.unfinished:
        s.reasons.append(f"{s.unfinished} files not copied yet")
    if unverified:
        s.reasons.append(f"{unverified} files not verified in the destination")
    return s


def summarize_all(cat: Catalog) -> list[SourceSummary]:
    ids = [r[0] for r in cat.conn.execute("SELECT id FROM sources ORDER BY label")]
    return [summarize(cat, i) for i in ids]


def results_block(s: SourceSummary, today: dt.date) -> str:
    """Plain text under 72 columns, appended to the drive's intake sheet."""
    lines = [
        f" Run {today.isoformat()}",
        f"   Files seen      {s.files_seen}  (included {s.included}, excluded {s.excluded})",
        f"   New unique      {s.new_unique}  ({human(s.new_bytes)})",
        f"   Duplicates      {s.duplicates}  (already in the backup)",
        f"   Held / errors   {s.held} / {s.errors}",
    ]
    if s.retire_safe:
        lines.append("   SAFE TO RETIRE: yes")
    else:
        lines.append("   SAFE TO RETIRE: NO")
        lines += [f"     - {r}"[:WIDTH] for r in s.reasons]
    return "\n".join(lines)


def sources_table(rows: list[SourceSummary]) -> str:
    head = f"{'drive':<18}{'seen':>8}{'incl':>8}{'new':>8}{'new size':>11}{'dup':>7}  retire"
    out = [head, "-" * len(head)]
    for s in rows:
        out.append(
            f"{s.label:<18}{s.files_seen:>8}{s.included:>8}{s.new_unique:>8}"
            f"{human(s.new_bytes):>11}{s.duplicates:>7}  "
            + ("yes" if s.retire_safe else "NO: " + "; ".join(s.reasons))
        )
    return "\n".join(out)


def where(cat: Catalog, query: str) -> list[tuple[str, str, str, str]]:
    """Every original location of a file, given its hash or a path (exact, or a trailing part).

    Returns (label, original path, status, destination path)."""
    q = cat.conn.execute
    if HASH_RE.match(query):
        hashes = [query]
    else:
        like = "%" + query.replace("\\", "/").lstrip("/").replace("%", r"\%") + "%"
        hashes = [
            r[0]
            for r in q(
                "SELECT DISTINCT hash FROM files WHERE hash IS NOT NULL AND"
                " (rel_path=? OR logical_path=? OR rel_path LIKE ? ESCAPE '\\')",
                (query, query, like),
            )
        ]
    out: list[tuple[str, str, str, str]] = []
    for h in sorted(hashes):
        out += q(
            "SELECT s.label, f.rel_path, f.status, COALESCE(b.dest_path, '-') FROM files f"
            " JOIN sources s ON s.id=f.source_id LEFT JOIN blobs b ON b.hash=f.hash"
            " WHERE f.hash=? ORDER BY s.label, f.rel_path",
            (h,),
        ).fetchall()
    return out


def _write_csv(path: Path, header: list[str], rows: list[tuple[object, ...]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def conflict_rows(cat: Catalog) -> list[tuple[object, ...]]:
    return cat.conn.execute(
        "SELECT c.dest_path, c.chosen_name, s.label, c.hash FROM conflicts c"
        " LEFT JOIN sources s ON s.id=c.source_id ORDER BY c.dest_path, c.chosen_name"
    ).fetchall()


def renamed_rows(cat: Catalog) -> list[tuple[object, ...]]:
    return cat.conn.execute(
        "SELECT original_name, dest_path, hash FROM blobs WHERE renamed=1 ORDER BY dest_path"
    ).fetchall()


def error_rows(cat: Catalog) -> list[tuple[object, ...]]:
    return cat.conn.execute(
        "SELECT s.label, f.rel_path, f.error FROM files f JOIN sources s ON s.id=f.source_id"
        " WHERE f.status='error' ORDER BY s.label, f.rel_path"
    ).fetchall()


def unknown_rows(cat: Catalog, label: str | None = None) -> list[tuple[str, str, int, int, str]]:
    """Extensions on no list that scans skipped: (drive, ext, files, bytes, examples)."""
    q = (
        "SELECT s.label, u.ext, u.file_count, u.total_bytes, u.example_paths"
        " FROM unknown_extensions u JOIN sources s ON s.id = u.source_id"
    )
    args: tuple[str, ...] = ()
    if label:
        q += " WHERE s.label=?"
        args = (label,)
    rows = cat.conn.execute(q + " ORDER BY u.total_bytes DESC, u.ext", args).fetchall()
    return [(d, e, n, b, " | ".join(json.loads(x or "[]"))) for d, e, n, b, x in rows]


def write_reports(cat: Catalog, dest: Path) -> None:
    """Refresh _reports/ (conflicts, renamed paths, read errors)."""
    out = dest / "_reports"
    _write_csv(out / "conflicts.csv", ["wanted_path", "chosen_path", "drive", "hash"],
               conflict_rows(cat))  # fmt: skip
    _write_csv(
        out / "renamed.csv", ["original_path", "destination_path", "hash"], renamed_rows(cat)
    )
    _write_csv(out / "errors.csv", ["drive", "path", "error"], error_rows(cat))
    _write_csv(out / "versions.csv", CSV_HEADER, csv_rows(find_version_groups(cat)))
    _write_csv(out / "unknown-excluded.csv", ["drive", "extension", "files", "bytes", "examples"],
               list[tuple[object, ...]](unknown_rows(cat)))  # fmt: skip
