import datetime as dt
import json
from pathlib import Path
from typing import Any

import pytest
from syrupy.assertion import SnapshotAssertion
from typer.testing import CliRunner

from drivecon.catalog import Catalog
from drivecon.cli import app
from drivecon.intake import append_results, format_sheet, parse_device_info, run_intake
from drivecon.smart import assess

FIX = Path(__file__).parent.parent / "fixtures"
TODAY = dt.date(2026, 11, 18)


def device(smart: str = "healthy") -> dict[str, Any]:
    d: dict[str, Any] = json.loads((FIX / "device_healthy_usb.json").read_text())
    d["smart"] = json.loads((FIX / "smart" / f"{smart}.json").read_text())
    return d


@pytest.mark.component
@pytest.mark.parametrize("smart", ["healthy", "failed", "reallocated", "usb_bridge_no_smart"])
def test_sheet_snapshot_and_width(smart: str, snapshot: SnapshotAssertion) -> None:
    d = device(smart)
    sheet = format_sheet("Dell-2009-C", parse_device_info(d), assess(d["smart"]), TODAY)
    assert max(len(line) for line in sheet.splitlines()) < 72
    assert sheet == snapshot


@pytest.mark.component
def test_intake_writes_sheet_json_and_registers(tmp_path: Path) -> None:
    cat = Catalog(tmp_path)
    r = run_intake("Dell-2009-C", device(), tmp_path, cat, TODAY)
    assert (tmp_path / "_intake" / "Dell-2009-C.txt").read_text().startswith("=")
    assert json.loads((tmp_path / "_intake" / "Dell-2009-C.json").read_text())["device"]
    row = cat.conn.execute("SELECT fs_uuid, smart_status FROM sources").fetchone()
    assert row == ("1A2B3C4D5E6F7788", "passed") and not r.already_known
    cat.close()


@pytest.mark.component
def test_rescan_recognizes_drive_and_keeps_appended_results(tmp_path: Path) -> None:
    cat = Catalog(tmp_path)
    first = run_intake("Dell-2009-C", device(), tmp_path, cat, TODAY)
    append_results(first.sheet_path, "Files seen: 10")
    again = run_intake("Other-Name", device(), tmp_path, cat, TODAY)
    assert again.already_known and again.label == "Dell-2009-C"
    assert again.source_id == first.source_id
    assert "Files seen: 10" in first.sheet_path.read_text()
    assert not (tmp_path / "_intake" / "Other-Name.txt").exists()
    cat.close()


def invoke(tmp_path: Path, smart: str, *extra: str, stdin: str | None = None):  # type: ignore[no-untyped-def]
    info = tmp_path / "info.json"
    info.write_text(json.dumps(device(smart)))
    return CliRunner().invoke(
        app,
        ["intake", "--label", "L1", "--device-info", str(info), "--dest", str(tmp_path), *extra],
        input=stdin,
    )


@pytest.mark.component
def test_cli_healthy_needs_no_confirmation(tmp_path: Path) -> None:
    res = invoke(tmp_path, "healthy")
    assert res.exit_code == 0 and "Intake sheet" in res.output


@pytest.mark.component
def test_cli_failed_smart_declined_exits_3(tmp_path: Path) -> None:
    res = invoke(tmp_path, "failed", stdin="n\n")
    assert res.exit_code == 3 and "FAILED" in res.output
    assert (tmp_path / "_intake" / "L1.txt").exists()  # the sheet is still written


@pytest.mark.component
def test_cli_failed_smart_continues_with_yes(tmp_path: Path) -> None:
    assert invoke(tmp_path, "failed", "--yes").exit_code == 0


@pytest.mark.component
def test_cli_source_dir_mock(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    res = CliRunner().invoke(
        app,
        ["intake", "--label", "M1", "--source-dir", str(src), "--dest", str(tmp_path / "d")],
    )
    assert res.exit_code == 0
    assert "not available" in (tmp_path / "d" / "_intake" / "M1.txt").read_text()
