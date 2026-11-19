import datetime as dt
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Annotated, Literal, cast

import typer

from drivecon.catalog import Catalog
from drivecon.config import load_file_types, load_path_rules
from drivecon.intake import human, run_intake, synthetic_device
from drivecon.pending import PendingInfo, PendingTracker, release_decided
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
        result = run_intake(label, data, dest, cat, dt.date.today())
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
