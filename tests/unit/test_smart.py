import json
from pathlib import Path
from typing import Any

import pytest

from drivecon.smart import assess

FIX = Path(__file__).parent.parent / "fixtures" / "smart"


def load(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIX / f"{name}.json").read_text())
    return data


@pytest.mark.unit
@pytest.mark.parametrize(
    ("fixture", "status", "warnings"),
    [
        ("healthy", "passed", []),
        ("failed", "failed", ["Overall SMART result is FAILED"]),
        ("reallocated", "passed", ["reallocated sectors: 8"]),
        ("pending", "passed", ["pending sectors: 2"]),
        ("offline_uncorrectable", "passed", ["offline-uncorrectable sectors: 1"]),
        ("usb_bridge_no_smart", "unavailable", []),
        ("not_available", "unavailable", []),
    ],
)
def test_assess(fixture: str, status: str, warnings: list[str]) -> None:
    r = assess(load(fixture))
    assert (r.status, r.warnings) == (status, warnings)


@pytest.mark.unit
def test_missing_json_is_unavailable_not_a_problem() -> None:
    r = assess(None)
    assert r.status == "unavailable" and not r.has_problem


@pytest.mark.unit
def test_usb_bridge_note_is_kept() -> None:
    assert "USB bridge" in assess(load("usb_bridge_no_smart")).note
