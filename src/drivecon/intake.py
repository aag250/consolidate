"""Intake: identify a drive, assess SMART, write the printable intake sheet."""

from __future__ import annotations

import datetime as dt
import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from drivecon.catalog import Catalog
from drivecon.smart import SmartReport, assess

WIDTH = 70


@dataclass
class DeviceInfo:
    model: str = ""
    serial: str = ""
    size_bytes: int = 0
    used_bytes: int = 0
    interface: str = ""
    fs_type: str = ""
    fs_uuid: str = ""
    volume_label: str = ""
    partitions: tuple[str, ...] = ()


def parse_device_info(data: dict[str, Any]) -> DeviceInfo:
    """Combine the host's lsblk, blkid and smartctl JSON into one description."""
    disks = data.get("lsblk", {}).get("blockdevices", [])
    disk = disks[0] if disks else {}
    blk = data.get("blkid", {})
    smart = data.get("smart") or {}
    parts = tuple(
        f"{c.get('name', '?')} {c.get('fstype') or '-'} {human(int(c.get('size') or 0))}"
        for c in disk.get("children", [])
    )
    iface = disk.get("tran") or ""
    stype = smart.get("device", {}).get("type", "")
    if iface and stype and stype != "scsi":
        iface = f"{iface} ({stype})"
    return DeviceInfo(
        model=(disk.get("model") or smart.get("model_name") or "").strip(),
        serial=(disk.get("serial") or smart.get("serial_number") or "").strip(),
        size_bytes=int(disk.get("size") or 0),
        used_bytes=int(data.get("used_bytes") or 0),
        interface=iface,
        fs_type=blk.get("TYPE", ""),
        fs_uuid=blk.get("UUID", ""),
        volume_label=blk.get("LABEL", ""),
        partitions=parts,
    )


def human(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1000 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1000
    raise AssertionError("unreachable")


def _row(key: str, value: str) -> str:
    line = f" {key:<13}{value}"
    return line if len(line) <= WIDTH else line[: WIDTH - 1] + "…"


def format_sheet(label: str, info: DeviceInfo, smart: SmartReport, today: dt.date) -> str:
    bar = "=" * WIDTH
    rule = "-" * WIDTH
    lines = [bar, _row("DRIVE", label), bar, _row("Intake date", today.isoformat())]
    lines += [
        _row("Model", info.model or "unknown"),
        _row("Serial", info.serial or "unknown"),
        _row("Capacity", human(info.size_bytes) if info.size_bytes else "unknown"),
        _row("Used", human(info.used_bytes) if info.used_bytes else "unknown"),
        _row("Interface", info.interface or "unknown"),
        _row("Filesystem", info.fs_type or "unknown"),
        _row("Volume label", info.volume_label or "-"),
        _row("UUID", info.fs_uuid or "unknown"),
    ]
    for i, p in enumerate(info.partitions):
        lines.append(_row("Partitions" if i == 0 else "", p))
    lines.append(rule)
    if smart.status == "unavailable":
        lines.append(_row("SMART", "not available"))
        if smart.note:
            lines.append(_row("", smart.note))
    else:
        lines.append(_row("SMART", smart.status.upper()))
        c = smart.counts
        lines.append(
            _row(
                "",
                f"realloc {c['reallocated']}  pending {c['pending']}"
                f"  offline {c['offline_uncorrectable']}",
            )
        )
        for w in smart.warnings:
            lines.append(_row("** WARNING", w))
    lines += [rule, " RESULTS (appended after each run)", rule]
    return "\n".join(lines) + "\n"


@dataclass
class IntakeResult:
    label: str
    source_id: int
    smart: SmartReport
    sheet_path: Path
    already_known: bool


def run_intake(
    label: str,
    device_data: dict[str, Any],
    dest: Path,
    catalog: Catalog,
    today: dt.date,
) -> IntakeResult:
    """Register the source, write ``_intake/<label>.txt`` and the raw JSON beside it."""
    info = parse_device_info(device_data)
    smart = assess(device_data.get("smart"))
    known = bool(
        info.fs_uuid
        and catalog.conn.execute(
            "SELECT 1 FROM sources WHERE fs_uuid=?", (info.fs_uuid,)
        ).fetchone()
    )
    source_id = catalog.register_source(
        label,
        info.fs_uuid or None,
        fs_type=info.fs_type,
        volume_label=info.volume_label,
        device_model=info.model,
        serial=info.serial,
        interface=info.interface,
        size_bytes=info.size_bytes,
        used_bytes=info.used_bytes,
        smart_status=smart.status,
        smart_json=json.dumps(device_data.get("smart")),
    )
    row = catalog.conn.execute("SELECT label FROM sources WHERE id=?", (source_id,)).fetchone()
    label = row[0]  # a rescanned drive keeps the label it was first given
    out = dest / "_intake"
    out.mkdir(parents=True, exist_ok=True)
    sheet = out / f"{label}.txt"
    if not sheet.exists():  # never clobber appended results on a rescan
        sheet.write_text(format_sheet(label, info, smart, today))
    (out / f"{label}.json").write_text(json.dumps(device_data, indent=1))
    return IntakeResult(label, source_id, smart, sheet, known)


def append_results(sheet: Path, text: str) -> None:
    with sheet.open("a") as f:
        f.write(text.rstrip("\n") + "\n")


def synthetic_device(label: str, source_dir: Path) -> dict[str, Any]:
    """Test-only device description for a directory mock."""
    usage = shutil.disk_usage(source_dir)
    return {
        "lsblk": {
            "blockdevices": [
                {
                    "name": label,
                    "model": "MOCK",
                    "serial": label,
                    "size": usage.total,
                    "tran": "dir",
                }
            ]
        },
        "blkid": {"UUID": f"mock-{label}", "TYPE": "dir", "LABEL": label},
        "used_bytes": usage.used,
        "smart": None,
    }
