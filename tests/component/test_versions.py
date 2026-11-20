import csv
import os
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from drivecon.cli import app
from drivecon.reports import write_reports
from drivecon.versions import find_version_groups, has_newer_claim
from tests.component.test_pipeline import Lab

DAY = 86400


@pytest.fixture
def lab(tmp_path: Path) -> Lab:
    return Lab(tmp_path)


def add(lab: Lab, label: str, files: list[tuple[str, str, int]], **kw: Any) -> None:
    """files: (path, content, age in days before 2020-01-01). Processes the drive."""
    sid, root = lab.add(label, [{"path": p, "content": c} for p, c, _ in files], **kw)
    for p, _, days in files:
        t = 1_577_836_800 - days * DAY
        os.utime(root / p, (t, t))
    lab.process(sid, root)


def names(groups: list[Any]) -> list[tuple[str, int]]:
    return [(g.name, len(g.members)) for g in groups]


@pytest.mark.component
def test_same_path_conflict_is_reported_with_the_newer_file_first(lab: Lab) -> None:
    add(lab, "Dell-2009-C", [("Users/u1/Documents/budget.xlsx", "version one", 3000)])
    add(lab, "USB-backup", [("Backup 2012/budget.xlsx", "version two, longer", 100)])
    groups = find_version_groups(lab.cat)
    assert names(groups) == [("budget.xlsx", 2)]
    g = groups[0]
    assert g.newest.drives == ("USB-backup",) and g.newest.dest_path.startswith("Files/budget (")
    assert has_newer_claim(g) and "may be a newer version" in g.notes[0]
    assert [m.drives for m in g.members] == [("USB-backup",), ("Dell-2009-C",)]


@pytest.mark.component
def test_a_folder_inside_the_other_counts_as_similar(lab: Lab) -> None:
    add(lab, "A", [("Users/u1/Documents/finance/w2.pdf", "old copy", 2000)])
    add(lab, "B", [("Users/u1/Documents/finance/taxes/w2.pdf", "newer, edited", 10)])
    assert names(find_version_groups(lab.cat)) == [("w2.pdf", 2)]


@pytest.mark.component
def test_three_versions_form_one_group_ordered_newest_first(lab: Lab) -> None:
    add(
        lab,
        "A",
        [
            ("Users/u1/Documents/plan.doc", "v1", 900),
            ("Users/u1/Documents/old/plan.doc", "v2 ", 500),
        ],
    )
    add(lab, "B", [("Backup/plan.doc", "v3 final", 5)])
    (g,) = find_version_groups(lab.cat)
    assert [m.size for m in g.members] == [8, 3, 2]


@pytest.mark.component
@pytest.mark.parametrize(
    ("a", "b"),
    [
        (
            "Users/u1/Documents/finance/taxes/2011/w2.pdf",
            "Users/u1/Documents/finance/taxes/2012/w2.pdf",
        ),
        (
            "Users/u1/Documents/projects/alpha/notes.txt",
            "Users/u1/Documents/music/rock/deep/notes.txt",
        ),
    ],
)
def test_different_folders_are_not_versions(lab: Lab, a: str, b: str) -> None:
    add(lab, "A", [(a, "first", 900), (b, "second one", 100)])
    assert find_version_groups(lab.cat) == []


@pytest.mark.component
def test_camera_photos_with_reused_names_are_not_versions(lab: Lab) -> None:
    add(lab, "SD-1", [("DCIM/100CANON/IMG_0001.jpg", "photo one", 900)], interface="sd")
    add(lab, "SD-2", [("DCIM/100CANON/IMG_0001.jpg", "photo two!", 800)], interface="sd")
    assert find_version_groups(lab.cat) == []


@pytest.mark.component
def test_identical_files_are_duplicates_not_versions(lab: Lab) -> None:
    add(lab, "A", [("Users/u1/Documents/x.doc", "same bytes", 900)])
    add(lab, "B", [("Backup/x.doc", "same bytes", 100)])
    assert find_version_groups(lab.cat) == []


@pytest.mark.component
def test_same_size_and_date_but_different_content_is_flagged(lab: Lab) -> None:
    add(lab, "A", [("Users/u1/Documents/x.doc", "AAAA", 900)])
    add(lab, "B", [("Backup/x.doc", "BBBB", 900)])
    (g,) = find_version_groups(lab.cat)
    assert not has_newer_claim(g) and "possible corruption" in g.notes[0]


@pytest.mark.component
def test_both_files_are_always_kept(lab: Lab) -> None:
    add(lab, "A", [("Users/u1/Documents/b.xlsx", "one", 900)])
    add(lab, "B", [("Backup/b.xlsx", "two!", 100)])
    assert len([p for p in (lab.dest / "Files").rglob("*") if p.is_file()]) == 2


@pytest.mark.component
def test_csv_report_and_cli(lab: Lab) -> None:
    add(lab, "A", [("Users/u1/Documents/b.xlsx", "one", 900)])
    add(lab, "B", [("Backup/b.xlsx", "two!", 100)])
    write_reports(lab.cat, lab.dest)
    rows = list(csv.reader((lab.dest / "_reports" / "versions.csv").open()))
    assert rows[0][:3] == ["group", "name", "rank_newest_first"] and len(rows) == 3
    assert rows[1][2] == "1" and "may be a newer version" in rows[1][7] and rows[2][7] == ""
    lab.cat.close()
    res = CliRunner().invoke(app, ["report", "versions", "--dest", str(lab.dest)])
    assert res.exit_code == 0 and "b.xlsx: 2 different files, newest first" in res.output
    assert "<- may be the newest" in res.output
