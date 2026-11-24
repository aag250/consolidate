import datetime as dt
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Annotated, Literal, cast

import typer

from drivecon.catalog import Catalog
from drivecon.config import load_file_types, load_path_rules, record_decision
from drivecon.console import CONSOLE
from drivecon.copier import CopyStats, DestinationFull, copy_source, verify
from drivecon.hashing import HashStats, HashWorker
from drivecon.intake import append_results, human, run_intake, synthetic_device
from drivecon.mirror import (
    MirrorError,
    behind,
    open_mirror,
    register_mirror,
    sync_mirror,
    verify_mirror,
)
from drivecon.pending import PendingInfo, PendingTracker, release_decided
from drivecon.photos import exif_date
from drivecon.placement import Placer, source_profile_roots
from drivecon.prune import find_candidates, prune, top_folders, write_listing
from drivecon.reports import (
    conflict_rows,
    renamed_rows,
    results_block,
    sources_table,
    summarize,
    summarize_all,
    unknown_rows,
    where,
    write_reports,
)
from drivecon.rules import Rules
from drivecon.scan import Scanner, ScanStats
from drivecon.session import apply_answer, ask_extension, run_scan_session
from drivecon.versions import find_version_groups, has_newer_claim

app = typer.Typer(help="Consolidate old drives into one deduplicated NTFS backup.")

DestOpt = Annotated[Path, typer.Option(envvar="DRIVECON_DEST", help="Destination drive.")]
ModeOpt = Annotated[str, typer.Option(envvar="DRIVECON_CATALOG_MODE", help="native or wsl.")]
WorkOpt = Annotated[
    Path | None, typer.Option(envvar="DRIVECON_WORK_DIR", help="WSL2 catalog working copy.")
]


@app.callback()
def main() -> None:
    """drivecon command line."""


def open_catalog(dest: Path, mode: str, work_dir: Path | None) -> Catalog:
    if mode not in ("native", "wsl"):
        raise typer.BadParameter("catalog mode must be native or wsl")
    return Catalog(dest, cast(Literal["native", "wsl"], mode), work_dir)


@app.command()
def intake(
    label: Annotated[str, typer.Option(help="Label for this drive, e.g. Dell-2009-C.")],
    device_info: Annotated[Path | None, typer.Option(help="JSON collected by intake.sh.")] = None,
    source_dir: Annotated[
        Path | None,
        typer.Option(help="Register a mounted folder with no block device (SMB share)."),
    ] = None,
    smart_json: Annotated[
        Path | None, typer.Option(help="Use this smartctl JSON instead of the device's.")
    ] = None,
    yes: Annotated[bool, typer.Option(help="Continue past SMART warnings without asking.")] = False,
    strip_prefix: Annotated[
        list[str] | None, typer.Option(help="Extra folder prefix to strip, e.g. 'OldPC/'.")
    ] = None,
    dest: DestOpt = Path("/dest"),
    catalog_mode: ModeOpt = "native",
    work_dir: WorkOpt = None,
) -> None:
    """Register a mounted source, check SMART and write _intake/<label>.txt."""
    if (device_info is None) == (source_dir is None):
        raise typer.BadParameter("give exactly one of --device-info or --source-dir")
    data = (
        json.loads(device_info.read_text()) if device_info else synthetic_device(label, source_dir)  # type: ignore[arg-type]
    )
    if smart_json:
        data["smart"] = json.loads(smart_json.read_text())
    cat = open_catalog(dest, catalog_mode, work_dir)
    try:
        result = run_intake(label, data, dest, cat, dt.date.today(), strip_prefix or ())
    finally:
        cat.close()
    if result.already_known:
        typer.echo(f"Recognized drive already registered as {result.label}.")
    typer.echo(f"Intake sheet: {result.sheet_path}")
    if result.smart.has_problem:
        for w in result.smart.warnings:
            typer.secho(f"SMART WARNING: {w}", fg=typer.colors.RED, err=True)
        if not yes and not sys.stdin.isatty():
            typer.echo("Not a terminal: pass --yes to continue past SMART warnings.", err=True)
            raise typer.Exit(3)
        if not yes and not typer.confirm("Continue with this drive?", default=False):
            raise typer.Exit(3)


@app.command(name="source-label")
def source_label(
    uuid: Annotated[str, typer.Option(help="Filesystem UUID of a partition.")],
    dest: DestOpt = Path("/dest"),
    catalog_mode: ModeOpt = "native",
    work_dir: WorkOpt = None,
) -> None:
    """Print the label a partition is registered under, or nothing if it is new."""
    cat = open_catalog(dest, catalog_mode, work_dir)
    try:
        row = cat.conn.execute("SELECT label FROM sources WHERE fs_uuid=?", (uuid,)).fetchone()
    finally:
        cat.close()
    if row:
        typer.echo(row[0])


MirrorOpt = Annotated[Path, typer.Option(envvar="DRIVECON_MIRROR", help="Secondary drive.")]
ConfigOpt = Annotated[Path, typer.Option(envvar="DRIVECON_CONFIG", help="Folder with the TOMLs.")]
SourcesOpt = Annotated[Path, typer.Option(envvar="DRIVECON_SOURCES", help="Folder of sources.")]
OsOpt = Annotated[str, typer.Option("--os", help="windows, linux or auto (from fs type).")]
WINDOWS_FS = {"ntfs", "ntfs3", "fuseblk", "vfat", "fat", "exfat", "msdos", "dir"}


def load_rules(config_dir: Path, os_kind: str, fs_type: str | None) -> Rules:
    if os_kind == "auto":
        os_kind = "windows" if (fs_type or "dir") in WINDOWS_FS else "linux"
    if os_kind not in ("windows", "linux"):
        raise typer.BadParameter("--os must be windows, linux or auto")
    return Rules(
        load_file_types(config_dir / "file-types.toml"),
        load_path_rules(config_dir / "paths.toml"),
        os_kind=cast(Literal["windows", "linux"], os_kind),
    )


_CTX: tuple[Catalog, int] | None = None  # the catalog and source the live command works on


def _live(cat: Catalog, sid: int) -> None:
    """Start progress meters and the heartbeat/reminder ticker; _quiet() stops them."""
    global _CTX
    CONSOLE.reset()
    _CTX = (cat, sid)
    with cat.lock:
        row = cat.conn.execute(
            "SELECT used_bytes, interface FROM sources WHERE id=?", (sid,)
        ).fetchone()
    # A drive's used space bounds the scan; a folder source (network share) reports its
    # whole filesystem's, which says nothing about the folder, so it gets counts only.
    used = row[0] if row and row[0] and row[1] != "dir" else None
    scan = CONSOLE.meter("scan")
    scan.total, scan.final_total = used, True
    CONSOLE.start()


def _quiet() -> None:
    global _CTX
    CONSOLE.stop()
    _CTX = None


def _progress(stats: ScanStats) -> None:
    walked = stats.included_bytes + stats.pending_bytes + stats.excluded_bytes
    CONSOLE.meter("scan").update(walked, time.monotonic())
    CONSOLE.status(
        "scan", f"{stats.files_seen} files, {stats.included} included, {stats.pending} held"
    )


def _test_hooks() -> tuple[float, int]:
    """Test-only knobs: slow the scan and report progress often."""
    return (
        float(os.environ.get("CONSOLIDATE_THROTTLE", "0")),
        int(os.environ.get("CONSOLIDATE_PROGRESS_EVERY", "1000")),
    )


def _batches() -> tuple[int, int, int]:
    """Commit batch sizes for scan, hash and copy; CONSOLIDATE_BATCH overrides (tests)."""
    n = int(os.environ.get("CONSOLIDATE_BATCH", "0"))
    return (n, n, n) if n else (1000, 100, 100)


def ensure_read_only(root: Path) -> None:
    """Refuse to touch a source that could be written to (sources are never modified)."""
    if os.environ.get("DRIVECON_ALLOW_WRITABLE_SOURCES"):  # tests use plain directories
        return
    try:
        read_only = bool(os.statvfs(root).f_flag & os.ST_RDONLY)
    except OSError as e:
        typer.echo(f"Cannot read source {root}: {e}", err=True)
        raise typer.Exit(2) from e
    if not read_only:
        typer.echo(
            f"Refusing to run: {root} is mounted read-write. Mount it read-only "
            "(intake.sh does this) and bind it :ro.",
            err=True,
        )
        raise typer.Exit(2)


def _source(cat: Catalog, label: str) -> tuple[int, str | None]:
    row = cat.conn.execute("SELECT id, fs_type FROM sources WHERE label=?", (label,)).fetchone()
    if row is None:
        raise typer.BadParameter(f"{label} is not registered; run intake first")
    return int(row[0]), row[1]


def _summarize(stats: ScanStats) -> None:
    typer.echo(
        f"Seen {stats.files_seen}; included {stats.included} ({human(stats.included_bytes)}); "
        f"excluded {stats.excluded_files} ({human(stats.excluded_bytes)}); "
        f"held {stats.pending}; errors {stats.errors}"
    )
    if stats.dropped:
        typer.echo(f"{stats.dropped} files from an earlier scan are now excluded or gone: dropped.")
    if stats.retired:
        typer.echo(
            f"{stats.retired} files already in the backup are now excluded: "
            "see `prune-excluded` to remove them from the backup."
        )


@app.command()
def scan(
    label: str,
    dest: DestOpt = Path("/dest"),
    config: ConfigOpt = Path("/config"),
    sources_root: SourcesOpt = Path("/sources"),
    os_kind: OsOpt = "auto",
    catalog_mode: ModeOpt = "native",
    work_dir: WorkOpt = None,
) -> None:
    """Walk and classify a registered source; safe to re-run."""
    cat = open_catalog(dest, catalog_mode, work_dir)
    try:
        sid, fs_type = _source(cat, label)
        _live(cat, sid)
        ensure_read_only(sources_root / label)
        rules = load_rules(config, os_kind, fs_type)
        throttle, every = _test_hooks()
        interactive = sys.stdin.isatty()
        res = run_scan_session(
            cat, sid, sources_root / label, rules, config / "file-types.toml",
            interactive=interactive, today=dt.date.today(), progress=_progress,
            progress_every=every, throttle=throttle,
        )  # fmt: skip
        _summarize(res.stats)
        _unknown_summary(res.stats, cat, sid)
    finally:
        _quiet()
        cat.close()
    if res.interrupted:
        typer.echo("Interrupted; held files stay pending. Re-run scan to continue.", err=True)
        raise typer.Exit(130)
    held = res.stats.pending - res.released
    if held > 0:
        typer.echo(f"{held} files held for unknown extensions; run `decide`.")


@app.command(name="dry-run")
def dry_run(
    label: str,
    config: ConfigOpt = Path("/config"),
    sources_root: SourcesOpt = Path("/sources"),
    os_kind: OsOpt = "auto",
) -> None:
    """Scan and report what would be copied, writing nothing to Files/ or the real catalog."""
    ensure_read_only(sources_root / label)
    with tempfile.TemporaryDirectory() as tmp:
        cat = Catalog(Path(tmp))
        try:
            sid = cat.register_source(label, None)
            rules = load_rules(config, os_kind, None)
            tracker = PendingTracker()
            stats = Scanner(cat, sid, sources_root / label, rules, tracker).run()
            _summarize(stats)
            for ext, count, total in cat.conn.execute(
                "SELECT ext, file_count, total_bytes FROM pending_extensions ORDER BY ext"
            ):
                typer.echo(f"  unknown .{ext}: {count} files, {human(total)}")
            for line in _profile_lines(cat, sid):
                typer.echo(line)
        finally:
            cat.close()


@app.command()
def decide(
    dest: DestOpt = Path("/dest"),
    config: ConfigOpt = Path("/config"),
    os_kind: OsOpt = "auto",
    catalog_mode: ModeOpt = "native",
    work_dir: WorkOpt = None,
) -> None:
    """Answer queued unknown-extension prompts."""
    cat = open_catalog(dest, catalog_mode, work_dir)
    try:
        rules = load_rules(config, os_kind, None)
        rows = cat.conn.execute(
            "SELECT ext, file_count, total_bytes, example_paths FROM pending_extensions "
            "WHERE decision IS NULL ORDER BY ext"
        ).fetchall()
        if not rows:
            typer.echo("Nothing to decide.")
            return
        for ext, count, total, examples in rows:
            ex = json.loads(examples or "[]")
            answer = ask_extension(PendingInfo(ext, count, total, ex))
            if answer != "later":
                apply_answer(ext, answer, ex[0] if ex else "", rules,
                             config / "file-types.toml", dt.date.today())  # fmt: skip
        released = release_decided(cat, rules, cat.clock())
        cat.commit()
        typer.echo(f"Released {released} held files.")
    finally:
        cat.close()


report_app = typer.Typer(help="Reports.")
app.add_typer(report_app, name="report")


@report_app.command("types")
def report_types(
    dest: DestOpt = Path("/dest"),
    catalog_mode: ModeOpt = "native",
    work_dir: WorkOpt = None,
) -> None:
    """Unknown and pending extensions by count and size."""
    cat = open_catalog(dest, catalog_mode, work_dir)
    try:
        rows = cat.conn.execute(
            "SELECT ext, file_count, total_bytes, example_paths FROM pending_extensions "
            "WHERE decision IS NULL ORDER BY total_bytes DESC"
        ).fetchall()
    finally:
        cat.close()
    if not rows:
        typer.echo("No pending extensions.")
    for ext, count, total, examples in rows:
        first = (json.loads(examples or "[]") or [""])[0]
        typer.echo(f".{ext:<12} {count:>7} files {human(total):>10}   e.g. {first}")


@report_app.command("unknown")
def report_unknown(
    label: Annotated[str | None, typer.Argument(help="Only this drive.")] = None,
    dest: DestOpt = Path("/dest"),
    catalog_mode: ModeOpt = "native",
    work_dir: WorkOpt = None,
) -> None:
    """Extensions on no list that were skipped (unknown = "exclude"), biggest first."""
    cat = open_catalog(dest, catalog_mode, work_dir)
    try:
        rows = unknown_rows(cat, label)
    finally:
        cat.close()
    if not rows:
        typer.echo("No unknown extensions were skipped.")
        return
    for drive, ext, n, size, examples in rows:
        first = examples.split(" | ")[0]
        typer.echo(f"{drive:<16} .{ext:<14} {n:>7} files {human(size):>10}   e.g. {first}")
    typer.echo("To keep one from now on: drivecon include-ext .EXT, then re-run that drive.")


@app.command(name="include-ext")
def include_ext(
    exts: Annotated[list[str], typer.Argument(help="Extensions to keep, e.g. .3mf .blend")],
    config: ConfigOpt = Path("/config"),
    dest: DestOpt = Path("/dest"),
    catalog_mode: ModeOpt = "native",
    work_dir: WorkOpt = None,
) -> None:
    """Keep an extension that was skipped as unknown; re-run the drives that had it."""
    cat = open_catalog(dest, catalog_mode, work_dir)
    try:
        today = dt.date.today()
        drives: set[str] = set()
        for raw in exts:
            ext = raw.strip().lstrip(".").lower()
            if not ext:
                continue
            rows = cat.conn.execute(
                "SELECT s.label, u.example_paths FROM unknown_extensions u"
                " JOIN sources s ON s.id = u.source_id WHERE u.ext=?",
                (ext,),
            ).fetchall()
            example = next((json.loads(x or "[]")[0] for _, x in rows if json.loads(x or "[]")), "")
            record_decision(config / "file-types.toml", ext, "include", example, today)
            drives.update(r[0] for r in rows)
            typer.echo(
                f".{ext}: included from now on" + (f" (seen on {len(rows)} drives)" if rows else "")
            )
    finally:
        cat.close()
    if drives:
        typer.echo("Re-run these drives to copy the files: " + ", ".join(sorted(drives)))


def _unknown_summary(stats: ScanStats, cat: Catalog, sid: int) -> None:
    if not stats.unknown_files:
        return
    rows = unknown_rows(
        cat, cat.conn.execute("SELECT label FROM sources WHERE id=?", (sid,)).fetchone()[0]
    )
    typer.echo(
        f"Skipped {stats.unknown_files} files ({human(stats.unknown_bytes)}) with "
        f"{len(rows)} unknown extensions:"
    )
    for _drive, ext, n, size, examples in rows[:8]:
        typer.echo(f"  .{ext:<14} {n:>7} files {human(size):>10}   e.g. {examples.split(' | ')[0]}")
    if len(rows) > 8:
        typer.echo(f"  ... and {len(rows) - 8} more")
    typer.echo("Full list: `report unknown`. To keep one: `include-ext .EXT`, then re-run.")


def _hash_progress(stats: HashStats) -> None:
    if _CTX is not None:
        cat, sid = _CTX
        with cat.lock:
            done, total = cat.conn.execute(
                "SELECT COALESCE(SUM(CASE WHEN status<>'seen' THEN size END), 0),"
                " COALESCE(SUM(size), 0) FROM files"
                " WHERE source_id=? AND status IN ('seen','hashed','duplicate','copied')",
                (sid,),
            ).fetchone()
        m = CONSOLE.meter("hash")
        m.final_total = CONSOLE.is_finished("scan")  # the total grows until the scan is done
        m.update(done, time.monotonic(), total)
    CONSOLE.status("hash", f"{stats.hashed} files, {human(stats.bytes)}")


def _copy_progress(stats: CopyStats) -> None:
    CONSOLE.meter("copy").update(stats.bytes, time.monotonic())
    CONSOLE.status("copy", f"{stats.copied} copied, {stats.duplicates} duplicates")


def _copy_total(cat: Catalog, sid: int) -> int:
    """Bytes this source still has to copy: each new content once."""
    with cat.lock:
        row = cat.conn.execute(
            "SELECT COALESCE(SUM(size), 0) FROM (SELECT f.hash, MAX(f.size) AS size FROM files f"
            " LEFT JOIN blobs b ON b.hash=f.hash AND b.copied_at IS NOT NULL"
            " WHERE f.source_id=? AND f.status='hashed' AND b.hash IS NULL GROUP BY f.hash)",
            (sid,),
        ).fetchone()
    return int(row[0])


def _copy_phase(
    cat: Catalog, sid: int, root: Path, config: Path, dest: Path, stop: threading.Event
) -> CopyStats:
    _, _, copy_batch = _batches()
    pr = load_path_rules(config / "paths.toml")
    meter = CONSOLE.meter("copy")
    meter.total, meter.final_total = _copy_total(cat, sid) or None, True
    throttle, every = _test_hooks()
    return copy_source(
        cat, sid, root, Placer(cat, pr), dest, exif=exif_date,
        photo_exts=frozenset(pr.photos.extensions), stop=stop,
        progress=_copy_progress, progress_every=every, throttle=throttle, batch=copy_batch,
    )  # fmt: skip


def _mirror_lag(cat: Catalog) -> list[tuple[str, int, int]]:
    rows = cat.conn.execute("SELECT id, label FROM mirrors ORDER BY label").fetchall()
    return [(label, *behind(cat, mid)) for mid, label in rows]


def _profile_lines(cat: Catalog, sid: int) -> list[str]:
    """Detected copied profiles, as shown by dry-run and on the intake sheet."""
    roots = source_profile_roots(cat, sid)
    if not roots:
        return []
    return ["Profile folders merged into Files/:"] + [
        "  " + ("/".join(r) + "/" if r else "(drive root)") for r in sorted(roots)
    ]


def _record_results(cat: Catalog, sid: int, dest: Path) -> None:
    """Append this drive's results to its intake sheet and refresh _reports/."""
    summary = summarize(cat, sid)
    sheet = dest / "_intake" / f"{summary.label}.txt"
    profiles = _profile_lines(cat, sid)
    n_unknown, n_ext = cat.conn.execute(
        "SELECT COALESCE(SUM(file_count), 0), COUNT(*) FROM unknown_extensions WHERE source_id=?",
        (sid,),
    ).fetchone()
    if sheet.exists():
        block = results_block(summary, dt.date.today())
        extra = [f"   {ln}"[:71] for ln in profiles]
        if n_unknown:
            extra.append(f"   Unknown types skipped: {n_unknown} files, {n_ext} extensions")
        append_results(sheet, "\n".join([block, *extra]))
    for line in profiles:
        typer.echo(line)
    write_reports(cat, dest)
    cat.backup_dated()  # a dated catalog copy after each drive
    for label, files, size in _mirror_lag(cat):
        if files:
            typer.echo(
                f"Secondary {label} is {files} files ({human(size)}) behind: run sync-mirror"
            )
    n_versions = len(find_version_groups(cat))
    if n_versions:
        typer.echo(
            f"{n_versions} sets of files look like versions of one document: see `report versions`"
        )
    verdict = "safe to retire" if summary.retire_safe else "NOT safe to retire yet"
    typer.echo(f"{summary.label}: {verdict}")
    for r in summary.reasons:
        typer.echo(f"  - {r}")


def _finish_copy(stats: CopyStats) -> None:
    typer.echo(
        f"Copied {stats.copied} files ({human(stats.bytes)}); {stats.duplicates} duplicates "
        f"recorded; errors {stats.errors}; changed during copy {stats.changed}"
    )


@app.command(name="hash")
def hash_cmd(
    label: str,
    dest: DestOpt = Path("/dest"),
    sources_root: SourcesOpt = Path("/sources"),
    catalog_mode: ModeOpt = "native",
    work_dir: WorkOpt = None,
) -> None:
    """Hash included files that are not hashed yet."""
    cat = open_catalog(dest, catalog_mode, work_dir)
    try:
        sid, _ = _source(cat, label)
        _live(cat, sid)
        ensure_read_only(sources_root / label)
        throttle, every = _test_hooks()
        worker = HashWorker(cat, sid, sources_root / label, progress=_hash_progress,
                            progress_every=every, throttle=throttle,
                            batch=_batches()[1])  # fmt: skip
        worker.scan_done.set()
        stats = worker.run()
    finally:
        _quiet()
        cat.close()
    typer.echo(f"Hashed {stats.hashed} files ({human(stats.bytes)}); errors {stats.errors}")


@app.command()
def copy(
    label: str,
    dest: DestOpt = Path("/dest"),
    config: ConfigOpt = Path("/config"),
    sources_root: SourcesOpt = Path("/sources"),
    catalog_mode: ModeOpt = "native",
    work_dir: WorkOpt = None,
) -> None:
    """Place and copy new files to Files/, verifying each."""
    cat = open_catalog(dest, catalog_mode, work_dir)
    code = 0
    try:
        sid, _ = _source(cat, label)
        _live(cat, sid)
        ensure_read_only(sources_root / label)
        stats = _copy_phase(cat, sid, sources_root / label, config, dest, threading.Event())
        _finish_copy(stats)
        _record_results(cat, sid, dest)
    except DestinationFull:
        typer.echo("Destination is full. Free some space and re-run.", err=True)
        code = 4
    except KeyboardInterrupt:
        typer.echo("Interrupted; re-run to continue.", err=True)
        code = 130
    finally:
        _quiet()
        cat.close()
    if code:
        raise typer.Exit(code)


@app.command()
def run(
    label: str,
    dest: DestOpt = Path("/dest"),
    config: ConfigOpt = Path("/config"),
    sources_root: SourcesOpt = Path("/sources"),
    os_kind: OsOpt = "auto",
    catalog_mode: ModeOpt = "native",
    work_dir: WorkOpt = None,
) -> None:
    """Scan, hash and copy a source in sequence, with unknown-extension prompts."""
    cat = open_catalog(dest, catalog_mode, work_dir)
    code = 0
    try:
        sid, fs_type = _source(cat, label)
        _live(cat, sid)
        root = sources_root / label
        ensure_read_only(root)
        rules = load_rules(config, os_kind, fs_type)
        throttle, every = _test_hooks()
        scan_batch, hash_batch, _ = _batches()
        hasher = HashWorker(cat, sid, root, progress=_hash_progress, progress_every=every,
                            throttle=throttle, batch=hash_batch)  # fmt: skip
        res = run_scan_session(
            cat, sid, root, rules, config / "file-types.toml", interactive=sys.stdin.isatty(),
            today=dt.date.today(), progress=_progress, progress_every=every,
            throttle=throttle, hasher=hasher, batch_size=scan_batch,
        )  # fmt: skip
        _summarize(res.stats)
        _unknown_summary(res.stats, cat, sid)
        if res.interrupted:
            typer.echo("Interrupted; re-run to continue.", err=True)
            code = 130
        else:
            _finish_copy(_copy_phase(cat, sid, root, config, dest, threading.Event()))
            _record_results(cat, sid, dest)
            held = res.stats.pending - res.released
            if held > 0:
                typer.echo(f"{held} files held for unknown extensions; run `decide`.")
    except DestinationFull:
        typer.echo("Destination is full. Free some space and re-run.", err=True)
        code = 4
    except KeyboardInterrupt:
        typer.echo("Interrupted; re-run to continue.", err=True)
        code = 130
    finally:
        _quiet()
        cat.close()
    if code:
        raise typer.Exit(code)


@app.command(name="prune-excluded")
def prune_excluded(
    yes: Annotated[bool, typer.Option(help="Delete without asking.")] = False,
    dest: DestOpt = Path("/dest"),
    catalog_mode: ModeOpt = "native",
    work_dir: WorkOpt = None,
) -> None:
    """Remove files from the backup that the rules now exclude everywhere they were found."""
    cat = open_catalog(dest, catalog_mode, work_dir)
    try:
        cands = find_candidates(cat)
        if not cands:
            typer.echo("Nothing to remove: every file in the backup is still wanted.")
            return
        listing = dest / "_reports" / "excluded-in-backup.csv"
        write_listing(cands, listing)
        total = sum(c.size for c in cands)
        typer.echo(
            f"{len(cands)} files ({human(total)}) in the backup are now excluded by the rules"
        )
        typer.echo("everywhere they were found. Largest folders:")
        for folder, n, size in top_folders(cands)[:15]:
            typer.echo(f"  {n:7d} {human(size):>10}  {folder}")
        rules: dict[str, int] = {}
        for c in cands:
            rules[c.rule] = rules.get(c.rule, 0) + 1
        typer.echo("Excluded by:")
        for rule, n in sorted(rules.items(), key=lambda r: -r[1])[:10]:
            typer.echo(f"  {n:7d}  {rule}")
        typer.echo(f"Full list: {listing}")
        if not yes:
            if not sys.stdin.isatty():
                typer.echo("Nothing deleted. Run again with --yes to remove them.")
                return
            if not typer.confirm(
                f"Delete these {len(cands)} files from the backup?", default=False
            ):
                typer.echo("Nothing deleted.")
                return
        res = prune(
            cat, dest, cands,
            progress=lambda r: typer.echo(f"[prune] {r.removed} removed, {human(r.bytes)}"),
        )  # fmt: skip
        write_reports(cat, dest)
        cat.backup_dated()
    finally:
        cat.close()
    typer.echo(
        f"Removed {res.removed} files ({human(res.bytes)}) and {res.dirs_removed} empty folders."
    )
    if res.missing:
        typer.echo(f"{res.missing} were already gone from the backup; the catalog is cleaned up.")
    if res.mismatched:
        typer.echo(f"{len(res.mismatched)} files left alone: their content no longer matches:")
        for m in res.mismatched[:20]:
            typer.echo(f"  {m}")
        raise typer.Exit(1)
    typer.echo("A secondary drive keeps its copies until `sync-mirror --prune`.")


@app.command(name="verify")
def verify_cmd(
    dest: DestOpt = Path("/dest"),
    mirror: Annotated[bool, typer.Option("--mirror", help="Verify the secondary drive.")] = False,
    mirror_root: MirrorOpt = Path("/mirror"),
    catalog_mode: ModeOpt = "native",
    work_dir: WorkOpt = None,
) -> None:
    """Re-hash every copied file against the catalog."""
    cat = open_catalog(dest, catalog_mode, work_dir)
    try:
        if mirror:
            try:
                res = verify_mirror(cat, open_mirror(cat, mirror_root), cat.clock())
            except MirrorError as e:
                typer.echo(str(e), err=True)
                raise typer.Exit(2) from e
        else:
            res = verify(cat, dest, cat.clock())
    finally:
        cat.close()
    typer.echo(
        f"Checked {res.checked}; missing {res.missing}; corrupt {res.corrupt}; "
        f"{'extra on secondary' if mirror else 'not in catalog'} {res.orphans}; "
        f"leftover temp files removed {res.tmp_removed}"
    )
    if not res.ok:
        raise typer.Exit(1)


@app.command(name="retry-errors")
def retry_errors(
    label: str,
    dest: DestOpt = Path("/dest"),
    catalog_mode: ModeOpt = "native",
    work_dir: WorkOpt = None,
) -> None:
    """Re-attempt files that failed to read."""
    cat = open_catalog(dest, catalog_mode, work_dir)
    try:
        sid, _ = _source(cat, label)
        with cat.lock:
            cur = cat.conn.execute(
                "UPDATE files SET status='seen', error=NULL, hash=NULL"
                " WHERE source_id=? AND status='error' AND size IS NOT NULL",
                (sid,),
            )
            cat.commit()
    finally:
        cat.close()
    typer.echo(f"{cur.rowcount} files queued; run `run {label}` to process them.")


@report_app.command("errors")
def report_errors(
    dest: DestOpt = Path("/dest"),
    catalog_mode: ModeOpt = "native",
    work_dir: WorkOpt = None,
) -> None:
    """Files that failed to read."""
    cat = open_catalog(dest, catalog_mode, work_dir)
    try:
        rows = cat.conn.execute(
            "SELECT s.label, f.rel_path, f.error FROM files f JOIN sources s ON s.id=f.source_id"
            " WHERE f.status='error' ORDER BY s.label, f.rel_path"
        ).fetchall()
    finally:
        cat.close()
    if not rows:
        typer.echo("No errors.")
    for label, path, err in rows:
        typer.echo(f"{label}: {path}: {err}")


@report_app.command("sources")
def report_sources(
    dest: DestOpt = Path("/dest"),
    catalog_mode: ModeOpt = "native",
    work_dir: WorkOpt = None,
) -> None:
    """Files, bytes and new unique files per source, with the retire-safe flag."""
    cat = open_catalog(dest, catalog_mode, work_dir)
    try:
        rows = summarize_all(cat)
        lag = _mirror_lag(cat)
        last = dict(cat.conn.execute("SELECT label, last_sync_at FROM mirrors"))
    finally:
        cat.close()
    typer.echo(sources_table(rows) if rows else "No sources registered.")
    for label, files, size in lag:
        state = "up to date" if not files else f"{files} files ({human(size)}) behind"
        typer.echo(f"Secondary {label}: {state}; last sync {last.get(label) or 'never'}")


@report_app.command("conflicts")
def report_conflicts(
    dest: DestOpt = Path("/dest"),
    catalog_mode: ModeOpt = "native",
    work_dir: WorkOpt = None,
) -> None:
    """Different files that mapped to the same path; both are kept."""
    cat = open_catalog(dest, catalog_mode, work_dir)
    try:
        rows = conflict_rows(cat)
    finally:
        cat.close()
    if not rows:
        typer.echo("No conflicts.")
    for wanted, chosen, label, _h in rows:
        typer.echo(f"{wanted}  ->  {chosen}  [{label}]")


@report_app.command("versions")
def report_versions(
    dest: DestOpt = Path("/dest"),
    catalog_mode: ModeOpt = "native",
    work_dir: WorkOpt = None,
) -> None:
    """Files with the same name in similar folders but different content: maybe versions."""
    cat = open_catalog(dest, catalog_mode, work_dir)
    try:
        groups = find_version_groups(cat)
    finally:
        cat.close()
    if not groups:
        typer.echo("No likely versions found.")
    for g in groups:
        typer.echo(f"{g.name}: {len(g.members)} different files, newest first")
        for i, m in enumerate(g.members):
            day = dt.datetime.fromtimestamp(m.mtime, dt.UTC).date().isoformat()
            tag = "  <- may be the newest" if i == 0 and has_newer_claim(g) else ""
            typer.echo(f"  {m.dest_path}  {day}  {human(m.size)}  [{', '.join(m.drives)}]{tag}")
        for note in g.notes:
            typer.echo(f"  {note}")


@report_app.command("renamed")
def report_renamed(
    dest: DestOpt = Path("/dest"),
    catalog_mode: ModeOpt = "native",
    work_dir: WorkOpt = None,
) -> None:
    """Paths changed to suit NTFS (forbidden characters, reserved names, length)."""
    cat = open_catalog(dest, catalog_mode, work_dir)
    try:
        rows = renamed_rows(cat)
    finally:
        cat.close()
    if not rows:
        typer.echo("No renamed paths.")
    for original, new, _h in rows:
        typer.echo(f"{original}  ->  {new}")


@app.command(name="where")
def where_cmd(
    path_or_hash: str,
    dest: DestOpt = Path("/dest"),
    catalog_mode: ModeOpt = "native",
    work_dir: WorkOpt = None,
) -> None:
    """Every original location of a file, by path (or part of one) or BLAKE3 hash."""
    cat = open_catalog(dest, catalog_mode, work_dir)
    try:
        rows = where(cat, path_or_hash)
    finally:
        cat.close()
    if not rows:
        typer.echo("Not found.")
        raise typer.Exit(1)
    for label, rel, status, dest_path in rows:
        typer.echo(f"{label}: {rel}  [{status}]  -> {dest_path}")


mirror_app = typer.Typer(help="Secondary drive.")
app.add_typer(mirror_app, name="mirror")


@mirror_app.command("register")
def mirror_register(
    label: str,
    serial: Annotated[str, typer.Option(help="NTFS volume serial, e.g. from `blkid`.")],
    dest: DestOpt = Path("/dest"),
    mirror_root: MirrorOpt = Path("/mirror"),
    catalog_mode: ModeOpt = "native",
    work_dir: WorkOpt = None,
) -> None:
    """Register the secondary drive once, by its volume serial."""
    cat = open_catalog(dest, catalog_mode, work_dir)
    try:
        m = register_mirror(cat, mirror_root, label, serial)
    except MirrorError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(2) from e
    finally:
        cat.close()
    typer.echo(f"Registered secondary {m.label} ({m.serial}).")


@app.command(name="sync-mirror")
def sync_mirror_cmd(
    prune: Annotated[
        bool, typer.Option(help="Remove files the catalog no longer expects.")
    ] = False,
    yes: Annotated[bool, typer.Option(help="Do not ask before pruning.")] = False,
    dest: DestOpt = Path("/dest"),
    mirror_root: MirrorOpt = Path("/mirror"),
    catalog_mode: ModeOpt = "native",
    work_dir: WorkOpt = None,
) -> None:
    """Copy everything new on the primary to the secondary, verifying each file."""
    cat = open_catalog(dest, catalog_mode, work_dir)
    code = 0

    def confirm(extras: list[str]) -> bool:
        typer.echo(f"{len(extras)} files on the secondary are not in the catalog:")
        for e in extras[:20]:
            typer.echo(f"  {e}")
        return yes or typer.confirm("Remove them from the secondary?", default=False)

    try:
        mirror = open_mirror(cat, mirror_root)
        _, every = _test_hooks()
        batch = _batches()[2]
        stats = sync_mirror(
            cat, dest, mirror, progress=lambda s: typer.echo(f"[sync] {s.copied} copied"),
            progress_every=every, batch=batch, prune=confirm if prune else None,
        )  # fmt: skip
        typer.echo(
            f"Synced {stats.copied} files ({human(stats.bytes)}) to {mirror.label}; "
            f"{stats.small_files} sheets/reports updated; pruned {stats.pruned}"
        )
        if stats.extras:
            typer.echo(
                f"{len(stats.extras)} extra files on the secondary were left alone "
                "(see _reports/mirror-extra.csv; remove with --prune)."
            )
        for e in stats.errors:
            typer.echo(f"ERROR: {e}", err=True)
        code = 1 if stats.errors else 0
    except MirrorError as e:
        typer.echo(str(e), err=True)
        code = 2
    except DestinationFull:
        typer.echo("The secondary is full. Free some space and re-run.", err=True)
        code = 4
    except KeyboardInterrupt:
        typer.echo("Interrupted; re-run to continue.", err=True)
        code = 130
    finally:
        cat.close()
    if code:
        raise typer.Exit(code)
