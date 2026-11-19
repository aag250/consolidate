import datetime as dt
import hashlib
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from drivecon.catalog import Catalog
from drivecon.cli import app
from drivecon.mirror import (
    MARKER,
    MirrorError,
    behind,
    open_mirror,
    register_mirror,
    sync_mirror,
    verify_mirror,
)
from drivecon.reports import write_reports
from tests.component.test_pipeline import M1, M2, Lab

NOW = dt.datetime(2026, 11, 18, 12, 0, 0)


@pytest.fixture
def lab(tmp_path: Path) -> Lab:
    return Lab(tmp_path)


def tree(root: Path, sub: str = "Files") -> dict[str, str]:
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in (root / sub).rglob("*")
        if p.is_file()
    }


def primed(lab: Lab) -> tuple[Path, int]:
    """Primary with both drives copied, a registered (empty) secondary."""
    s1, r1 = lab.add("Dell-2009-C", M1)
    lab.process(s1, r1)
    (lab.dest / "_intake").mkdir(exist_ok=True)
    (lab.dest / "_intake" / "Dell-2009-C.txt").write_text("sheet")
    mroot = lab.tmp / "mirror"
    mroot.mkdir()
    return mroot, register_mirror(lab.cat, mroot, "Sec-A", "AAAA-1111").id


@pytest.mark.component
def test_sync_copies_files_sheets_reports_and_a_fresh_catalog(lab: Lab) -> None:
    mroot, mid = primed(lab)
    write_reports(lab.cat, lab.dest)
    m = open_mirror(lab.cat, mroot)
    stats = sync_mirror(lab.cat, lab.dest, m)
    assert stats.copied == 3 and not stats.errors and stats.small_files >= 3
    assert tree(mroot) == tree(lab.dest)
    assert (mroot / "_intake/Dell-2009-C.txt").read_text() == "sheet"
    assert (mroot / "_reports/renamed.csv").exists()
    copy = sqlite3.connect(mroot / "_catalog" / "catalog.sqlite")
    assert copy.execute("SELECT COUNT(*) FROM blobs").fetchone() == (3,)
    assert copy.execute("SELECT COUNT(*) FROM mirror_blobs").fetchone() == (3,)
    assert behind(lab.cat, mid) == (0, 0)


@pytest.mark.component
def test_second_sync_only_copies_what_is_new_and_keeps_mtimes(lab: Lab) -> None:
    mroot, mid = primed(lab)
    m = open_mirror(lab.cat, mroot)
    sync_mirror(lab.cat, lab.dest, m)
    assert sync_mirror(lab.cat, lab.dest, m).copied == 0
    s2, r2 = lab.add("USB-backup", M2)
    lab.process(s2, r2)
    assert behind(lab.cat, mid)[0] == 1  # only the conflicting budget file is new
    stats = sync_mirror(lab.cat, lab.dest, m)
    assert stats.copied == 1 and tree(mroot) == tree(lab.dest)
    p = "Files/finance/w2.pdf"
    assert (mroot / p).stat().st_mtime == (lab.dest / p).stat().st_mtime


@pytest.mark.component
def test_never_deletes_extras_which_are_reported_and_pruned_only_on_request(lab: Lab) -> None:
    mroot, _ = primed(lab)
    m = open_mirror(lab.cat, mroot)
    sync_mirror(lab.cat, lab.dest, m)
    (mroot / "Files/leftover.txt").write_text("should not be here")
    stats = sync_mirror(lab.cat, lab.dest, m)
    assert stats.extras == ["Files/leftover.txt"] and (mroot / "Files/leftover.txt").exists()
    assert "Files/leftover.txt" in (lab.dest / "_reports/mirror-extra.csv").read_text()
    declined = sync_mirror(lab.cat, lab.dest, m, prune=lambda extras: False)
    assert declined.pruned == 0 and (mroot / "Files/leftover.txt").exists()
    done = sync_mirror(lab.cat, lab.dest, m, prune=lambda extras: True)
    assert done.pruned == 1 and not (mroot / "Files/leftover.txt").exists()
    assert tree(mroot) == tree(lab.dest)


@pytest.mark.component
def test_refuses_unregistered_or_unplugged_secondaries(lab: Lab, tmp_path: Path) -> None:
    primed(lab)
    blank = tmp_path / "blank"
    blank.mkdir()
    with pytest.raises(MirrorError, match="not a registered secondary"):
        open_mirror(lab.cat, blank)
    with pytest.raises(MirrorError):
        open_mirror(lab.cat, tmp_path / "unplugged")
    (blank / MARKER).write_text('{"serial": "ZZZZ-9999"}')
    with pytest.raises(MirrorError, match="not registered in this catalog"):
        open_mirror(lab.cat, blank)
    with pytest.raises(MirrorError, match="another mirror"):
        register_mirror(lab.cat, blank, "x", "AAAA-1111")


@pytest.mark.component
def test_verify_mirror_catches_corruption_missing_and_extras(
    lab: Lab, capsys: pytest.CaptureFixture[str]
) -> None:
    mroot, _ = primed(lab)
    m = open_mirror(lab.cat, mroot)
    sync_mirror(lab.cat, lab.dest, m)
    assert verify_mirror(lab.cat, m, NOW).ok
    (mroot / "Files/finance/w2.pdf").write_bytes(b"bit rot")
    (mroot / "Files/budget.xlsx").unlink()
    (mroot / "Files/stray.txt").write_text("x")
    res = verify_mirror(lab.cat, m, NOW)
    out = capsys.readouterr().out
    assert (res.corrupt, res.missing, res.orphans) == (1, 1, 1) and not res.ok
    assert "CORRUPT  Files/finance/w2.pdf" in out and "EXTRA    Files/stray.txt" in out


@pytest.mark.component
def test_corrupt_primary_file_is_never_copied_to_the_secondary(lab: Lab) -> None:
    mroot, _ = primed(lab)
    (lab.dest / "Files/finance/w2.pdf").write_bytes(b"primary rot")
    stats = sync_mirror(lab.cat, lab.dest, open_mirror(lab.cat, mroot))
    assert any("finance/w2.pdf" in e for e in stats.errors)
    assert not (mroot / "Files/finance/w2.pdf").exists()
    assert not list((mroot / "Files").rglob(".dc-*"))


@pytest.mark.component
def test_existing_identical_file_is_adopted_after_an_interrupted_sync(lab: Lab) -> None:
    mroot, mid = primed(lab)
    (mroot / "Files/finance").mkdir(parents=True)
    (mroot / "Files/finance/w2.pdf").write_bytes((lab.dest / "Files/finance/w2.pdf").read_bytes())
    stats = sync_mirror(lab.cat, lab.dest, open_mirror(lab.cat, mroot))
    assert stats.skipped_existing == 1 and stats.copied == 2 and behind(lab.cat, mid) == (0, 0)


@pytest.mark.component
def test_promote_secondary_then_sync_a_fresh_drive_from_it(lab: Lab, tmp_path: Path) -> None:
    mroot, _ = primed(lab)
    sync_mirror(lab.cat, lab.dest, open_mirror(lab.cat, mroot))
    lab.cat.close()
    # The primary dies. The secondary becomes the new primary...
    new_primary = Catalog(mroot, clock=lambda: NOW)
    assert new_primary.conn.execute("SELECT COUNT(*) FROM blobs").fetchone() == (3,)
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    m = register_mirror(new_primary, fresh, "Sec-B", "BBBB-2222")
    assert behind(new_primary, m.id) == (3, 355)  # nothing recorded for the new drive yet
    sync_mirror(new_primary, mroot, m)
    assert tree(fresh) == tree(mroot) and len(tree(fresh)) == 3
    assert verify_mirror(new_primary, m, NOW).ok
    new_primary.close()


@pytest.mark.component
def test_cli_register_sync_verify_and_report(tmp_path: Path, lab: Lab) -> None:
    s1, r1 = lab.add("Dell-2009-C", M1)
    lab.process(s1, r1)
    lab.cat.close()
    mroot = tmp_path / "mirror"
    mroot.mkdir()
    r = CliRunner()
    c = ["--dest", str(lab.dest), "--mirror-root", str(mroot)]
    assert r.invoke(app, ["sync-mirror", *c]).exit_code == 2  # not registered yet
    assert (
        r.invoke(app, ["mirror", "register", "Sec-A", "--serial", "AAAA-1111", *c]).exit_code == 0
    )
    assert "3 files" in r.invoke(app, ["report", "sources", "--dest", str(lab.dest)]).output
    res = r.invoke(app, ["sync-mirror", *c])
    assert res.exit_code == 0 and "Synced 3 files" in res.output, res.output
    assert "up to date" in r.invoke(app, ["report", "sources", "--dest", str(lab.dest)]).output
    assert r.invoke(app, ["verify", "--mirror", *c]).exit_code == 0
    (mroot / "Files/finance/w2.pdf").write_bytes(b"x")
    assert r.invoke(app, ["verify", "--mirror", *c]).exit_code == 1
    (mroot / "Files/extra.txt").write_text("e")
    pruned = r.invoke(app, ["sync-mirror", "--prune", *c], input="n\n")
    assert "not in the catalog" in pruned.output and (mroot / "Files/extra.txt").exists()
    yes = r.invoke(app, ["sync-mirror", "--prune", "--yes", *c])
    assert yes.exit_code == 0 and not (mroot / "Files/extra.txt").exists()
