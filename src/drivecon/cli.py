import datetime as dt
import json
import os
import sys
import tempfile
import threading
from pathlib import Path
from typing import Annotated, Literal, cast

import typer

from drivecon.catalog import Catalog
from drivecon.config import load_file_types, load_path_rules
from drivecon.copier import CopyStats, DestinationFull, copy_source, verify
from drivecon.hashing import HashStats, HashWorker
from drivecon.intake import append_results, human, run_intake, synthetic_device
from drivecon.pending import PendingInfo, PendingTracker, release_decided
from drivecon.photos import exif_date
from drivecon.placement import Placer
from drivecon.reports import (
    conflict_rows,
    renamed_rows,
    results_block,
    sources_table,
    summarize,
    summarize_all,
    where,
    write_reports,
)
from drivecon.rules import Rules
from drivecon.scan import Scanner, ScanStats
from drivecon.session import apply_answer, ask_extension, run_scan_session

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
        Path | None, typer.Option(hidden=True, help="Test only: register a directory mock.")
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


def _progress(stats: ScanStats) -> None:
    typer.echo(
        f"[scan] {stats.files_seen} files, {stats.included} included, {stats.pending} held",
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
        rules = load_rules(config, os_kind, fs_type)
        throttle, every = _test_hooks()
        interactive = sys.stdin.isatty()
        res = run_scan_session(
            cat, sid, sources_root / label, rules, config / "file-types.toml",
            interactive=interactive, today=dt.date.today(), progress=_progress,
            progress_every=every, throttle=throttle,
        )  # fmt: skip
    finally:
        cat.close()
    _summarize(res.stats)
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


def _hash_progress(stats: HashStats) -> None:
    typer.echo(f"[hash] {stats.hashed} files, {human(stats.bytes)}")


def _copy_progress(stats: CopyStats) -> None:
    typer.echo(f"[copy] {stats.copied} copied, {stats.duplicates} duplicates")


def _copy_phase(
    cat: Catalog, sid: int, root: Path, config: Path, dest: Path, stop: threading.Event
) -> CopyStats:
    _, _, copy_batch = _batches()
    pr = load_path_rules(config / "paths.toml")
    throttle, every = _test_hooks()
    return copy_source(
        cat, sid, root, Placer(cat, pr), dest, exif=exif_date,
        photo_exts=frozenset(pr.photos.extensions), stop=stop,
        progress=_copy_progress, progress_every=every, throttle=throttle, batch=copy_batch,
    )  # fmt: skip


def _record_results(cat: Catalog, sid: int, dest: Path) -> None:
    """Append this drive's results to its intake sheet and refresh _reports/."""
    summary = summarize(cat, sid)
    sheet = dest / "_intake" / f"{summary.label}.txt"
    if sheet.exists():
        append_results(sheet, results_block(summary, dt.date.today()))
    write_reports(cat, dest)
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
        throttle, every = _test_hooks()
        worker = HashWorker(cat, sid, sources_root / label, progress=_hash_progress,
                            progress_every=every, throttle=throttle,
                            batch=_batches()[1])  # fmt: skip
        worker.scan_done.set()
        stats = worker.run()
    finally:
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
        rules = load_rules(config, os_kind, fs_type)
        root = sources_root / label
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
        cat.close()
    if code:
        raise typer.Exit(code)


@app.command(name="verify")
def verify_cmd(
    dest: DestOpt = Path("/dest"),
    catalog_mode: ModeOpt = "native",
    work_dir: WorkOpt = None,
) -> None:
    """Re-hash every copied file against the catalog."""
    cat = open_catalog(dest, catalog_mode, work_dir)
    try:
        res = verify(cat, dest, cat.clock())
    finally:
        cat.close()
    typer.echo(
        f"Checked {res.checked}; missing {res.missing}; corrupt {res.corrupt}; "
        f"not in catalog {res.orphans}; leftover temp files removed {res.tmp_removed}"
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
    finally:
        cat.close()
    typer.echo(sources_table(rows) if rows else "No sources registered.")


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
