import datetime as dt
import json
import sys
from pathlib import Path
from typing import Annotated, Literal, cast

import typer

from drivecon.catalog import Catalog
from drivecon.intake import run_intake, synthetic_device

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
