import csv
import datetime as dt
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from drivecon.cli import app
from drivecon.copier import copy_source
from drivecon.hashing import HashWorker
from drivecon.mockdevices import build_tree
from drivecon.pending import PendingTracker
from drivecon.placement import Placer
from drivecon.reports import (
    results_block,
    sources_table,
    summarize,
    summarize_all,
    where,
    write_reports,
)
from drivecon.scan import Scanner
from tests.component.test_pipeline import M1, M2, Lab

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="needs POSIX: hostile names, statvfs or raw-byte paths cannot exist on Windows",
)


TODAY = dt.date(2026, 11, 18)


@pytest.fixture
def lab(tmp_path: Path) -> Lab:
    return Lab(tmp_path)


@pytest.mark.component
def test_summary_counts_and_retire_safe_when_everything_is_in_the_backup(lab: Lab) -> None:
    sid, root = lab.add("Dell-2009-C", M1)
    lab.process(sid, root)
    s = summarize(lab.cat, sid)
    assert (s.included, s.excluded, s.new_unique, s.duplicates) == (4, 1, 3, 1)
    assert s.files_seen == 5 and s.new_bytes == 300 + 50 + 5 and s.retire_safe, s.reasons


@pytest.mark.component
def test_second_drive_has_no_new_bytes_for_files_already_held(lab: Lab) -> None:
    s1, r1 = lab.add("Dell-2009-C", M1)
    lab.process(s1, r1)
    s2, r2 = lab.add("USB-backup", M2)
    lab.process(s2, r2)
    usb = summarize(lab.cat, s2)
    assert (usb.new_unique, usb.duplicates) == (1, 1) and usb.retire_safe


@pytest.mark.component
def test_not_safe_when_never_scanned_held_errored_or_unfinished(lab: Lab) -> None:
    sid, root = lab.add("A", [{"path": "d/x.docx", "size": 5}, {"path": "d/y.weird", "size": 5},
                              {"path": "d/z.docx", "size": 6}])  # fmt: skip
    assert "never fully scanned" in summarize(lab.cat, sid).reasons
    from dataclasses import replace

    from drivecon.config import load_file_types
    from drivecon.rules import Rules

    ft = load_file_types(Path(__file__).parent.parent.parent / "config" / "file-types.toml")
    ask = Rules(replace(ft, unknown="ask"), lab.pr)  # held files block retire-safe
    Scanner(lab.cat, sid, root, ask, PendingTracker()).run()
    reasons = " ".join(summarize(lab.cat, sid).reasons)
    assert "unknown-extension" in reasons and "not copied yet" in reasons
    w = HashWorker(lab.cat, sid, root)
    w.scan_done.set()
    (root / "d/z.docx").unlink()
    w.run()
    copy_source(lab.cat, sid, root, Placer(lab.cat, lab.pr), lab.dest)
    assert "failed to read" in " ".join(summarize(lab.cat, sid).reasons)


@pytest.mark.component
def test_not_safe_if_a_blob_was_never_verified(lab: Lab) -> None:
    sid, root = lab.add("A", M1)
    lab.process(sid, root)
    lab.cat.conn.execute("UPDATE blobs SET verified_at=NULL")
    assert not summarize(lab.cat, sid).retire_safe


@pytest.mark.component
def test_results_block_fits_a_printed_sheet(lab: Lab) -> None:
    sid, root = lab.add("A", M1)
    lab.process(sid, root)
    text = results_block(summarize(lab.cat, sid), TODAY)
    assert max(len(line) for line in text.splitlines()) < 72 and "SAFE TO RETIRE: yes" in text
    lab.cat.conn.execute("UPDATE blobs SET verified_at=NULL")
    bad = results_block(summarize(lab.cat, sid), TODAY)
    assert "SAFE TO RETIRE: NO" in bad and max(len(x) for x in bad.splitlines()) < 72


@pytest.mark.component
def test_sources_table_lists_every_drive(lab: Lab) -> None:
    s1, r1 = lab.add("Dell-2009-C", M1)
    lab.process(s1, r1)
    lab.add("Empty-1", [{"path": "a.docx", "size": 1}])  # registered, not scanned
    out = sources_table(summarize_all(lab.cat))
    assert "Dell-2009-C" in out and "yes" in out and "Empty-1" in out and "NO: never fully" in out


@pytest.mark.component
def test_where_finds_every_location_by_hash_or_path(lab: Lab) -> None:
    s1, r1 = lab.add("Dell-2009-C", M1)
    lab.process(s1, r1)
    s2, r2 = lab.add("USB-backup", M2)
    lab.process(s2, r2)
    h = lab.cat.conn.execute(
        "SELECT hash FROM blobs WHERE dest_path='Files/finance/w2.pdf'"
    ).fetchone()[0]
    by_hash = where(lab.cat, h)
    assert len(by_hash) == 3 and {r[3] for r in by_hash} == {"Files/finance/w2.pdf"}
    assert {r[0] for r in by_hash} == {"Dell-2009-C", "USB-backup"}
    assert where(lab.cat, "Desktop/w2.pdf") == by_hash  # a part of a path is enough
    assert where(lab.cat, "no-such-file") == []


@pytest.mark.component
def test_reports_written_to_the_destination(lab: Lab) -> None:
    s1, r1 = lab.add("Dell-2009-C", M1)
    lab.process(s1, r1)
    s2, r2 = lab.add("USB-backup", M2)
    lab.process(s2, r2)
    write_reports(lab.cat, lab.dest)
    rep = lab.dest / "_reports"
    conflicts = list(csv.reader((rep / "conflicts.csv").open()))
    assert conflicts[0] == ["wanted_path", "chosen_path", "drive", "hash"] and len(conflicts) == 2
    assert conflicts[1][0] == "Files/budget.xlsx" and "USB-backup" in conflicts[1][2]
    renamed = list(csv.reader((rep / "renamed.csv").open()))
    assert renamed[1][:2] == [["Users/u1/", "a:b.txt"][1], "Files/a_b.txt"]
    assert list(csv.reader((rep / "errors.csv").open())) == [["drive", "path", "error"]]
    assert not list(rep.glob("*.tmp"))


@pytest.mark.component
def test_cli_run_appends_results_and_reports_retire_state(tmp_path: Path) -> None:
    import shutil

    root = Path(__file__).parent.parent.parent
    dest, config, sources = tmp_path / "dest", tmp_path / "config", tmp_path / "sources"
    shutil.copytree(root / "config", config)
    build_tree({"seed": 1, "files": M1}, sources / "M1")
    r = CliRunner()
    common = ["--dest", str(dest)]
    assert (
        r.invoke(
            app, ["intake", "--label", "M1", "--source-dir", str(sources / "M1"), *common]
        ).exit_code
        == 0
    )
    res = r.invoke(
        app, ["run", "M1", *common, "--config", str(config), "--sources-root", str(sources)]
    )
    assert res.exit_code == 0, res.output
    assert "M1: safe to retire" in res.output
    sheet = (dest / "_intake" / "M1.txt").read_text()
    assert "SAFE TO RETIRE: yes" in sheet and "Run " in sheet
    assert "M1" in r.invoke(app, ["report", "sources", *common]).output
    w = r.invoke(app, ["where", "Desktop/w2.pdf", *common])
    assert w.exit_code == 0 and "finance/w2.pdf" in w.output
    assert r.invoke(app, ["where", "nope", *common]).exit_code == 1
    assert "a:b.txt" in r.invoke(app, ["report", "renamed", *common]).output
    assert "No conflicts." in r.invoke(app, ["report", "conflicts", *common]).output
