"""Sanitized names must be creatable and listable on the filesystem the tests run on.

On the Windows CI runner this is a real NTFS volume, which is the point."""

from pathlib import Path

import pytest

from drivecon.paths import limit_length, sanitize_segment

HOSTILE = [
    "a:b.txt",
    "what?.doc",
    "star*.txt",
    'quote".txt',
    "lt<gt>.txt",
    "pipe|.txt",
    "back\\slash.txt",
    "CON.txt",
    "con",
    "PRN.doc",
    "AUX",
    "NUL.txt",
    "COM1.txt",
    "LPT9",
    "trailing.",
    "trailing ",
    "dots...",
    "ctrl\x01char.txt",
    "caf\u00e9.txt",
    "\u65e5\u672c\u8a9e.txt",
    "a" * 300 + ".txt",
]


@pytest.mark.component
@pytest.mark.parametrize("name", HOSTILE)
def test_sanitized_name_can_be_created_listed_and_read_back(tmp_path: Path, name: str) -> None:
    safe = sanitize_segment(name)
    path = tmp_path / safe
    path.write_text("payload")
    assert [p.name for p in tmp_path.iterdir()] == [safe]
    assert path.read_text() == "payload"


@pytest.mark.component
def test_a_shortened_deep_path_can_be_created(tmp_path: Path) -> None:
    segs, fits = limit_length(
        ["Files", *[f"folder{i:02}" + "x" * 40 for i in range(8)], "report.docx"]
    )
    target = tmp_path.joinpath(*segs)
    target.parent.mkdir(parents=True)
    target.write_text("deep")
    assert fits and target.read_text() == "deep"
