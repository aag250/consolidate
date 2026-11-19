"""Interpret ``smartctl --json`` output."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Status = Literal["passed", "failed", "unavailable"]

# ATA attribute ids that signal a failing disk when their raw value is above zero.
WATCHED = {5: "reallocated", 197: "pending", 198: "offline_uncorrectable"}


@dataclass
class SmartReport:
    status: Status
    counts: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    note: str = ""

    @property
    def has_problem(self) -> bool:
        return bool(self.warnings)


def assess(data: dict[str, Any] | None) -> SmartReport:
    """Warn if the overall result is FAILED or reallocated, pending or
    offline-uncorrectable sectors are above zero. Anything without SMART data is
    reported as unavailable (never as a problem)."""
    if not data:
        return SmartReport("unavailable", note="SMART not available")
    overall = data.get("smart_status")
    table = data.get("ata_smart_attributes", {}).get("table", [])
    if overall is None and not table:
        msgs = data.get("smartctl", {}).get("messages", [])
        note = msgs[0].get("string", "") if msgs else ""
        return SmartReport("unavailable", note=note or "SMART not available")

    counts = {name: 0 for name in WATCHED.values()}
    for attr in table:
        name = WATCHED.get(attr.get("id", -1))
        if name:
            counts[name] = int(attr.get("raw", {}).get("value", 0))
    warnings: list[str] = []
    status: Status = "passed"
    if overall is not None and not overall.get("passed", True):
        status = "failed"
        warnings.append("Overall SMART result is FAILED")
    for name, n in counts.items():
        if n > 0:
            warnings.append(f"{name.replace('_', '-')} sectors: {n}")
    return SmartReport(status, counts, warnings)
