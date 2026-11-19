from pathlib import Path

import pytest

from drivecon.mockdevices import build_tree, load_spec, tree_digest

DEVICES = Path(__file__).parent.parent / "fixtures" / "devices"


@pytest.mark.unit
def test_build_tree_is_deterministic(tmp_path: Path) -> None:
    spec = load_spec(DEVICES / "xp_era.yaml")
    a, b = tmp_path / "a", tmp_path / "b"
    build_tree(spec, a)
    build_tree(spec, b)
    assert tree_digest(a) == tree_digest(b)


@pytest.mark.unit
def test_backup_copy_has_same_bytes_as_original(tmp_path: Path) -> None:
    xp, usb = tmp_path / "xp", tmp_path / "usb"
    build_tree(load_spec(DEVICES / "xp_era.yaml"), xp)
    build_tree(load_spec(DEVICES / "backup_usb.yaml"), usb)
    orig = xp / "Documents and Settings/user1/My Documents/finance/taxes/2011/w2.pdf"
    copy = usb / "Backup 2012/finance/taxes/2011/w2.pdf"
    assert orig.read_bytes() == copy.read_bytes()
