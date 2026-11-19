import typer

app = typer.Typer(help="Consolidate old drives into one deduplicated NTFS backup.")


@app.callback()
def main() -> None:
    """drivecon command line."""
