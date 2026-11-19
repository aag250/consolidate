import datetime as dt
import json
from pathlib import Path

import pytest

from drivecon.catalog import Catalog
from drivecon.config import load_path_rules
from drivecon.placement import Placer

ROOT = Path(__file__).parent.parent.parent
NOW = dt.datetime(2026, 11, 18, 12, 0, 0)
DAY = dt.datetime(2011, 3, 4, 12, tzinfo=dt.UTC).timestamp()


class World:
    def __init__(self, tmp: Path) -> None:
        self.cat = Catalog(tmp, clock=lambda: NOW)
        self.placer = Placer(self.cat, load_path_rules(ROOT / "config" / "paths.toml"))
        self.n = 0

    def source(self, label: str, interface: str = "usb", strip: list[str] | None = None) -> int:
        return self.cat.register_source(
            label, f"uuid-{label}", interface=interface, strip_prefixes=json.dumps(strip or [])
        )

    def file(self, sid: int, rel: str, h: str, mtime: float = DAY, size: int = 10) -> None:
        self.n += 1
        ext = rel.rpartition(".")[2].lower()
        self.cat.conn.execute(
            "INSERT INTO files (source_id, rel_path_raw, rel_path, size, mtime, ext, hash, status)"
            " VALUES (?,?,?,?,?,?,?,'hashed')",
            (sid, rel.encode(), rel, size, mtime, ext, h),
        )

    def place(self, h: str, exif: str | None = None):  # type: ignore[no-untyped-def]
        return self.placer.place(h, exif)


@pytest.fixture
def w(tmp_path: Path) -> World:
    return World(tmp_path)


@pytest.mark.component
def test_same_file_on_two_drives_lands_once_at_the_merged_path(w: World) -> None:
    a, b = w.source("Dell-2009-C"), w.source("USB-backup")
    w.file(a, "Users/joe/Documents/finance/taxes/2011/w2.pdf", "h1")
    w.file(b, "Backup 2012/finance/taxes/2011/w2.pdf", "h1")
    p = w.place("h1")
    assert p.dest_path == "Files/finance/taxes/2011/w2.pdf" and not p.conflict and not p.renamed
    logical = w.cat.conn.execute("SELECT DISTINCT logical_path FROM files").fetchall()
    assert logical == [("finance/taxes/2011/w2.pdf",)]  # both locations stay in the catalog


@pytest.mark.component
def test_placement_is_idempotent(w: World) -> None:
    a = w.source("A")
    w.file(a, "docs/x.doc", "h1")
    first = w.place("h1")
    assert w.place("h1") == first
    assert w.cat.conn.execute("SELECT COUNT(*) FROM blobs").fetchone()[0] == 1


@pytest.mark.component
def test_different_files_at_one_path_both_stay_the_later_gets_source_and_date(w: World) -> None:
    a, b = w.source("Dell-2009-C"), w.source("USB-backup")
    w.file(a, "Users/joe/Documents/budget.xlsx", "h1")
    w.file(b, "Backup 2012/BUDGET.XLSX", "h2", mtime=DAY)  # case-insensitive clash
    first, second = w.place("h1"), w.place("h2")
    assert first.dest_path == "Files/budget.xlsx" and not first.conflict
    assert second.conflict and second.dest_path == "Files/BUDGET (USB-backup, 2011-03-04).XLSX"
    row = w.cat.conn.execute("SELECT dest_path, hash, chosen_name FROM conflicts").fetchone()
    assert row == ("Files/BUDGET.XLSX", "h2", "Files/BUDGET (USB-backup, 2011-03-04).XLSX")


@pytest.mark.component
def test_ntfs_hostile_names_are_sanitized_and_logged_with_original(w: World) -> None:
    a = w.source("Linux-box", strip=["home/ann/"])
    w.file(a, "home/ann/stuff/a:b.txt", "h1")
    w.file(a, "home/ann/stuff/CON.txt", "h2")
    p1, p2 = w.place("h1"), w.place("h2")
    assert (p1.dest_path, p1.renamed) == ("Files/stuff/a_b.txt", True)
    assert p2.dest_path == "Files/stuff/_CON.txt"
    orig = w.cat.conn.execute("SELECT original_name FROM blobs WHERE hash='h1'").fetchone()
    assert orig == ("stuff/a:b.txt",)


@pytest.mark.component
def test_per_drive_strip_prefix(w: World) -> None:
    a = w.source("Old-PC", strip=["OldPC/"])
    w.file(a, "OldPC/projects/plan.doc", "h1")
    assert w.place("h1").dest_path == "Files/projects/plan.doc"


@pytest.mark.component
def test_long_paths_are_shortened_under_240(w: World) -> None:
    a = w.source("A")
    deep = "/".join([f"folder_name_number_{i:02d}_" + "x" * 30 for i in range(8)])
    w.file(a, f"{deep}/report.docx", "h1")
    p = w.place("h1")
    assert (
        len(p.dest_path) + 3 < 240 and p.renamed and p.fits and p.dest_path.endswith("/report.docx")
    )


@pytest.mark.component
def test_camera_dump_images_go_to_photos_by_exif_date_else_mtime(w: World) -> None:
    sd = w.source("SD-1", interface="sd")
    w.file(sd, "DCIM/100CANON/IMG_0001.JPG", "h1")
    w.file(sd, "IMG_0002.jpg", "h2")  # root of an SD card
    w.file(sd, "DCIM/100CANON/IMG_0003.jpg", "h3", mtime=DAY)
    w.file(sd, "Vacation/IMG_0004.jpg", "h4")  # named folder stays put
    assert (
        w.place("h1", exif="2015-06-01T10:00:00").dest_path == "Files/Photos/2015/06/IMG_0001.JPG"
    )
    assert (
        w.place("h2", exif="2014-01-15T00:00:00").dest_path == "Files/Photos/2014/01/IMG_0002.jpg"
    )
    assert w.place("h3").dest_path == "Files/Photos/2011/03/IMG_0003.jpg"
    assert w.place("h4").dest_path == "Files/Vacation/IMG_0004.jpg"


@pytest.mark.component
def test_same_camera_file_names_from_different_cards_conflict_not_overwrite(w: World) -> None:
    c1, c2 = w.source("SD-1", "sd"), w.source("SD-2", "sd")
    w.file(c1, "DCIM/100CANON/IMG_0001.jpg", "h1")
    w.file(c2, "DCIM/100CANON/IMG_0001.jpg", "h2")
    a, b = w.place("h1", "2015-06-01T00:00:00"), w.place("h2", "2015-06-02T00:00:00")
    assert a.dest_path == "Files/Photos/2015/06/IMG_0001.jpg"
    assert b.conflict and b.dest_path.startswith("Files/Photos/2015/06/IMG_0001 (SD-2, ")


@pytest.mark.component
def test_oldest_mtime_across_locations_is_recorded(w: World) -> None:
    a, b = w.source("A"), w.source("B")
    w.file(a, "docs/x.doc", "h1", mtime=500.0)
    w.file(b, "docs/x.doc", "h1", mtime=100.0)
    w.place("h1")
    assert w.cat.conn.execute("SELECT oldest_mtime FROM blobs").fetchone() == (100.0,)
