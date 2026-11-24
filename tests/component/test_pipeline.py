import datetime as dt
import errno
import os
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest

from drivecon import copier
from drivecon.catalog import Catalog
from drivecon.config import load_file_types, load_path_rules
from drivecon.copier import (
    DestinationFull,
    SourceChanged,
    copy_blob,
    copy_source,
    sweep_tmp,
    tmp_name,
    verify,
)
from drivecon.hashing import HashWorker, hash_file
from drivecon.mockdevices import build_tree
from drivecon.pending import PendingTracker
from drivecon.placement import Placer
from drivecon.rules import Rules
from drivecon.scan import Scanner

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="needs POSIX: hostile names, statvfs or raw-byte paths cannot exist on Windows",
)


ROOT = Path(__file__).parent.parent.parent
NOW = dt.datetime(2026, 11, 18, 12, 0, 0)


class Lab:
    """A destination plus any number of mock sources, driven through the real phases."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.dest = tmp / "dest"
        self.cat = Catalog(self.dest, clock=lambda: NOW)
        self.pr = load_path_rules(ROOT / "config" / "paths.toml")
        self.rules = Rules(load_file_types(ROOT / "config" / "file-types.toml"), self.pr)

    def add(self, label: str, files: list[dict[str, Any]], **fields: object) -> tuple[int, Path]:
        root = self.tmp / "sources" / label
        build_tree({"seed": 7, "files": files}, root)
        sid = self.cat.register_source(label, f"uuid-{label}", fs_type="ntfs", **fields)
        return sid, root

    def process(self, sid: int, root: Path, **kw: Any) -> Any:
        Scanner(self.cat, sid, root, self.rules, PendingTracker()).run()
        w = HashWorker(self.cat, sid, root)
        w.scan_done.set()
        w.run()
        return copy_source(self.cat, sid, root, Placer(self.cat, self.pr), self.dest, **kw)

    def status(self) -> dict[tuple[str, str], str]:
        rows = self.cat.conn.execute(
            "SELECT s.label, f.rel_path, f.status FROM files f JOIN sources s ON s.id=f.source_id"
        )
        return {(r[0], r[1]): r[2] for r in rows}

    def tree(self) -> set[str]:
        return {
            p.relative_to(self.dest).as_posix()
            for p in (self.dest / "Files").rglob("*")
            if p.is_file()
        }


@pytest.fixture
def lab(tmp_path: Path) -> Lab:
    return Lab(tmp_path)


M1: list[dict[str, Any]] = [
    {"path": "Users/u1/Documents/finance/w2.pdf", "key": "w2", "size": 300},
    {"path": "Users/u1/Desktop/w2.pdf", "key": "w2", "size": 300},  # duplicate, worse path
    {"path": "Users/u1/Documents/budget.xlsx", "key": "b1", "size": 50},
    {"path": "Users/u1/Documents/a:b.txt", "content": "colon"},
    {"path": "Windows/x.dll", "size": 9},
]
M2: list[dict[str, Any]] = [
    {"path": "Backup 2012/finance/w2.pdf", "key": "w2", "size": 300},
    {"path": "Backup 2012/budget.xlsx", "key": "b2", "size": 60},  # same path, other content
]


@pytest.mark.component
def test_full_flow_copies_each_unique_file_once(lab: Lab) -> None:
    sid, root = lab.add("Dell-2009-C", M1)
    stats = lab.process(sid, root)
    assert (stats.copied, stats.duplicates) == (3, 1)
    assert lab.tree() == {"Files/finance/w2.pdf", "Files/budget.xlsx", "Files/a_b.txt"}
    st = lab.status()
    assert st[("Dell-2009-C", "Users/u1/Documents/finance/w2.pdf")] == "copied"
    assert st[("Dell-2009-C", "Users/u1/Desktop/w2.pdf")] == "duplicate"
    assert (lab.dest / "Files/finance/w2.pdf").read_bytes() == (
        root / "Users/u1/Documents/finance/w2.pdf"
    ).read_bytes()


@pytest.mark.component
def test_second_drive_records_locations_and_conflicts_keep_both(lab: Lab) -> None:
    s1, r1 = lab.add("Dell-2009-C", M1)
    lab.process(s1, r1)
    s2, r2 = lab.add("USB-backup", M2)
    stats = lab.process(s2, r2)
    assert (stats.copied, stats.duplicates) == (1, 1)  # w2 is location-only now
    assert lab.status()[("USB-backup", "Backup 2012/finance/w2.pdf")] == "duplicate"
    conflicted = [t for t in lab.tree() if "(USB-backup," in t]
    assert len(conflicted) == 1 and conflicted[0].startswith("Files/budget (USB-backup, ")
    where = lab.cat.conn.execute(
        "SELECT COUNT(*) FROM files WHERE hash=(SELECT hash FROM blobs WHERE dest_path=?)",
        ("Files/finance/w2.pdf",),
    ).fetchone()
    assert where == (3,)  # every place it was found stays in the catalog


@pytest.mark.component
def test_rerun_after_completion_does_nothing(lab: Lab) -> None:
    sid, root = lab.add("A", M1)
    lab.process(sid, root)
    before = (lab.status(), lab.tree())
    stats = lab.process(sid, root)
    assert (stats.copied, stats.duplicates) == (0, 0) and (lab.status(), lab.tree()) == before


@pytest.mark.component
def test_copy_sets_oldest_modified_time(lab: Lab) -> None:
    sid, root = lab.add("A", [{"path": "docs/x.docx", "size": 10}])
    os.utime(root / "docs/x.docx", (1_000_000_000, 1_000_000_000))
    lab.process(sid, root)
    assert (lab.dest / "Files/docs/x.docx").stat().st_mtime == 1_000_000_000


@pytest.mark.component
def test_source_is_never_modified(lab: Lab) -> None:
    sid, root = lab.add("A", M1)
    before = {p: (p.read_bytes(), p.stat().st_mtime) for p in root.rglob("*") if p.is_file()}
    lab.process(sid, root)
    after = {p: (p.read_bytes(), p.stat().st_mtime) for p in root.rglob("*") if p.is_file()}
    assert before == after


@pytest.mark.component
def test_file_changed_between_hash_and_copy_is_rehashed_not_copied(lab: Lab) -> None:
    sid, root = lab.add("A", [{"path": "docs/x.docx", "content": "v1"}])
    Scanner(lab.cat, sid, root, lab.rules, PendingTracker()).run()
    w = HashWorker(lab.cat, sid, root)
    w.scan_done.set()
    w.run()
    (root / "docs/x.docx").write_text("v2-changed")
    stats = copy_source(lab.cat, sid, root, Placer(lab.cat, lab.pr), lab.dest)
    assert stats.changed == 1 and stats.copied == 0 and not lab.tree()
    assert lab.cat.conn.execute("SELECT status, hash FROM files").fetchone() == ("seen", None)
    stats = lab.process(sid, root)  # the next run picks up the new content
    assert stats.copied == 1 and (lab.dest / "Files/docs/x.docx").read_text() == "v2-changed"


@pytest.mark.component
def test_read_error_is_logged_and_the_run_continues(lab: Lab) -> None:
    sid, root = lab.add(
        "A", [{"path": "docs/ok.docx", "size": 5}, {"path": "docs/bad.docx", "size": 5}]
    )
    Scanner(lab.cat, sid, root, lab.rules, PendingTracker()).run()
    (root / "docs/bad.docx").unlink()  # vanishes after the scan
    w = HashWorker(lab.cat, sid, root)
    w.scan_done.set()
    assert w.run().errors == 1
    stats = copy_source(lab.cat, sid, root, Placer(lab.cat, lab.pr), lab.dest)
    assert stats.copied == 1
    st = lab.status()
    assert st[("A", "docs/bad.docx")] == "error" and st[("A", "docs/ok.docx")] == "copied"


@pytest.mark.component
def test_good_copy_left_by_a_crash_before_the_commit_is_adopted(lab: Lab) -> None:
    sid, root = lab.add("A", [{"path": "docs/x.docx", "size": 40}])
    lab.process(sid, root)
    # Simulate a crash after the rename but before the catalog commit.
    lab.cat.conn.execute("UPDATE blobs SET copied_at=NULL")
    lab.cat.conn.execute("UPDATE files SET status='hashed'")
    lab.cat.conn.commit()
    (lab.dest / "Files/docs/.dc-leftover.tmp").write_bytes(b"junk")
    stats = copy_source(lab.cat, sid, root, Placer(lab.cat, lab.pr), lab.dest)
    assert stats.copied == 1
    assert lab.cat.conn.execute("SELECT copied_at IS NOT NULL FROM blobs").fetchone() == (1,)
    assert not list((lab.dest / "Files").rglob(".dc-*"))


@pytest.mark.component
def test_different_file_already_at_destination_is_an_error_not_an_overwrite(lab: Lab) -> None:
    sid, root = lab.add("A", [{"path": "docs/x.docx", "content": "mine"}])
    (lab.dest / "Files/docs").mkdir(parents=True)
    (lab.dest / "Files/docs/x.docx").write_text("someone else's")
    lab.process(sid, root)
    assert (lab.dest / "Files/docs/x.docx").read_text() == "someone else's"
    assert lab.status()[("A", "docs/x.docx")] == "error"


@pytest.mark.component
def test_destination_full_stops_cleanly_and_resumes(
    lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    sid, root = lab.add(
        "A", [{"path": f"docs/f{i}.docx", "key": str(i), "size": 20} for i in range(4)]
    )
    Scanner(lab.cat, sid, root, lab.rules, PendingTracker()).run()
    w = HashWorker(lab.cat, sid, root)
    w.scan_done.set()
    w.run()
    real = hash_file
    calls = {"n": 0}

    def full_after_two(path: str | Path) -> tuple[str, int]:
        if str(path).endswith(".tmp"):
            calls["n"] += 1
            if calls["n"] == 3:
                raise OSError(errno.ENOSPC, "No space left on device")
        return real(path)

    monkeypatch.setattr("drivecon.copier.hash_file", full_after_two)
    with pytest.raises(DestinationFull):
        copy_source(lab.cat, sid, root, Placer(lab.cat, lab.pr), lab.dest)
    assert len(lab.tree()) == 2 and not list((lab.dest / "Files").rglob(".dc-*"))
    monkeypatch.undo()
    stats = copy_source(lab.cat, sid, root, Placer(lab.cat, lab.pr), lab.dest)
    assert stats.copied == 2 and len(lab.tree()) == 4  # committed work was kept


@pytest.mark.component
def test_destination_full_while_staging_keeps_the_files_already_staged(
    lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    sid, root = lab.add(
        "A", [{"path": f"docs/f{i}.docx", "key": str(i), "size": 20} for i in range(4)]
    )
    Scanner(lab.cat, sid, root, lab.rules, PendingTracker()).run()
    w = HashWorker(lab.cat, sid, root)
    w.scan_done.set()
    w.run()
    real = copier._write_tmp
    calls = {"n": 0}

    def full_on_third(src: Path, final: Path, expected: str) -> tuple[Path, int]:
        calls["n"] += 1
        if calls["n"] == 3:
            raise DestinationFull(str(final))
        return real(src, final, expected)

    monkeypatch.setattr(copier, "_write_tmp", full_on_third)
    with pytest.raises(DestinationFull):  # all four fall in one batch
        copy_source(lab.cat, sid, root, Placer(lab.cat, lab.pr), lab.dest)
    assert len(lab.tree()) == 2 and not list((lab.dest / "Files").rglob(".dc-*"))
    monkeypatch.undo()
    stats = copy_source(lab.cat, sid, root, Placer(lab.cat, lab.pr), lab.dest)
    assert stats.copied == 2 and len(lab.tree()) == 4


@pytest.mark.component
def test_copy_blob_never_leaves_a_partial_file_under_the_final_name(tmp_path: Path) -> None:
    src = tmp_path / "s.bin"
    src.write_bytes(b"x" * 1000)
    digest = hash_file(src)[0]
    final = tmp_path / "out" / "f.bin"
    with pytest.raises(SourceChanged):
        copy_blob(src, final, "0" * 64, 1.0)
    assert not final.exists() and not list(final.parent.glob(".dc-*"))
    copy_blob(src, final, digest, 1.0)
    assert final.read_bytes() == src.read_bytes() and not list(final.parent.glob(".dc-*"))


@pytest.mark.component
def test_sweep_tmp_removes_only_temp_files(tmp_path: Path) -> None:
    (tmp_path / "Files/a").mkdir(parents=True)
    (tmp_path / "Files/a" / tmp_name("ab" * 32)).write_text("x")
    (tmp_path / "Files/a/keep.txt").write_text("y")
    assert sweep_tmp(tmp_path) == 1 and (tmp_path / "Files/a/keep.txt").exists()


@pytest.mark.component
def test_verify_ok_then_catches_corruption_missing_and_orphans(
    lab: Lab, capsys: pytest.CaptureFixture[str]
) -> None:
    sid, root = lab.add("A", M1)
    lab.process(sid, root)
    assert verify(lab.cat, lab.dest, NOW).ok
    (lab.dest / "Files/finance/w2.pdf").write_bytes(b"corrupted")
    (lab.dest / "Files/budget.xlsx").unlink()
    (lab.dest / "Files/stray.txt").write_text("not in catalog")
    res = verify(lab.cat, lab.dest, NOW)
    out = capsys.readouterr().out
    assert (res.corrupt, res.missing, res.orphans) == (1, 1, 1) and not res.ok
    assert "CORRUPT  Files/finance/w2.pdf" in out and "ORPHAN   Files/stray.txt" in out


@pytest.mark.component
def test_camera_photo_is_filed_by_exif_date(lab: Lab) -> None:
    from PIL import Image

    sid, root = lab.add("SD-1", [{"path": "notes.txt", "content": "x"}], interface="sd")
    img = root / "DCIM/100CANON/IMG_0001.jpg"
    img.parent.mkdir(parents=True)
    im = Image.new("RGB", (4, 4), "red")
    exif = Image.Exif()
    exif[0x8769] = {36867: "2015:06:01 10:30:00"}
    im.save(img, exif=exif)
    from drivecon.photos import exif_date

    assert exif_date(img) == "2015-06-01T10:30:00"
    lab.process(sid, root, exif=exif_date, photo_exts=frozenset(lab.pr.photos.extensions))
    assert "Files/Photos/2015/06/IMG_0001.jpg" in lab.tree()


@pytest.mark.component
def test_hash_worker_runs_alongside_the_scan(lab: Lab) -> None:
    from drivecon.session import run_scan_session

    sid, root = lab.add(
        "A", [{"path": f"d/f{i:02}.docx", "key": str(i), "size": 10} for i in range(30)]
    )
    w = HashWorker(lab.cat, sid, root, batch=5)
    run_scan_session(
        lab.cat, sid, root, lab.rules, ROOT / "config" / "file-types.toml",
        interactive=False, today=NOW.date(), hasher=w, batch_size=5,
    )  # fmt: skip
    assert w.stats.hashed == 30
    assert lab.cat.conn.execute("SELECT COUNT(*) FROM files WHERE status='hashed'").fetchone() == (
        30,
    )
    shutil.rmtree(root)  # nothing else needed from the source for the check above


@pytest.mark.component
def test_a_profile_copied_into_any_folder_merges_with_the_users_profile(lab: Lab) -> None:
    s1, r1 = lab.add("PC", [
        {"path": "Users/kel/Documents/taxes/w2.pdf", "content": "w2"},
        {"path": "Users/kel/Desktop/list.txt", "content": "list"},
    ])  # fmt: skip
    lab.process(s1, r1)
    s2, r2 = lab.add("SSD", [
        {"path": "KG 10.23.21 Backup/Documents/taxes/w2.pdf", "content": "w2"},
        {"path": "KG 10.23.21 Backup/Documents/taxes/1099.pdf", "content": "1099"},
        {"path": "KG 10.23.21 Backup/Desktop/new.txt", "content": "new"},
        {"path": "KG 10.23.21 Backup/Downloads/form.pdf", "content": "form"},
        {"path": "KG 10.23.21 Backup/Favorites/desktop.ini", "content": "junk"},
        {"path": "Projects/site/Documents/spec.txt", "content": "spec"},
        {"path": "Projects/site/Pictures/logo.txt", "content": "logo"},
    ])  # fmt: skip
    stats = lab.process(s2, r2)
    assert (stats.copied, stats.duplicates) == (5, 1)
    tree = lab.tree()
    assert {"Files/taxes/1099.pdf", "Files/Desktop/new.txt", "Files/Downloads/form.pdf"} <= set(
        tree
    )
    assert "Files/Projects/site/Documents/spec.txt" in tree  # two profile folders: left as is
    assert not [t for t in tree if "KG 10.23.21 Backup" in t]


def _rules_excluding(lab: Lab, pattern: str) -> Rules:
    cfg = lab.tmp / "cfg-prune"
    cfg.mkdir(exist_ok=True)
    text = (ROOT / "config" / "paths.toml").read_text()
    text += f'\n[[exclusion]]\nscope = "test"\nos = "any"\npatterns = ["{pattern}"]\n'
    (cfg / "paths.toml").write_text(text)
    return Rules(
        load_file_types(ROOT / "config" / "file-types.toml"), load_path_rules(cfg / "paths.toml")
    )


def _backed_up_then_excluded(lab: Lab) -> Path:
    sid, root = lab.add("A", [
        {"path": "Users/u/Documents/keep.docx", "content": "keep"},
        {"path": "Tools/sample/a.docx", "content": "a"},
        {"path": "Tools/sample/sub/b.docx", "content": "b"},
        {"path": "Tools/sample/shared.docx", "content": "keep"},  # same content as keep.docx
    ])  # fmt: skip
    lab.process(sid, root)
    assert {"Files/Tools/sample/a.docx", "Files/Tools/sample/sub/b.docx"} <= set(lab.tree())
    lab.rules = _rules_excluding(lab, "Tools/")
    assert Scanner(lab.cat, sid, root, lab.rules, PendingTracker()).run().retired == 3
    lab.cat.commit()
    return root


@pytest.mark.component
def test_prune_removes_only_files_excluded_everywhere_and_empty_folders(lab: Lab) -> None:
    from drivecon.prune import find_candidates, prune

    _backed_up_then_excluded(lab)
    cands = find_candidates(lab.cat)
    assert sorted(c.dest_path for c in cands) == [
        "Files/Tools/sample/a.docx",
        "Files/Tools/sample/sub/b.docx",
    ]
    res = prune(lab.cat, lab.dest, cands)
    assert (res.removed, res.missing, res.mismatched) == (2, 0, [])
    # The content also kept as keep.docx stays, at the path it was first placed at.
    (left,) = lab.tree()
    assert (lab.dest / left).read_bytes() == b"keep"
    assert not (lab.dest / "Files" / "Tools" / "sample" / "sub").exists()  # emptied folder removed
    assert lab.cat.conn.execute("SELECT COUNT(*) FROM blobs").fetchone() == (1,)
    assert verify(lab.cat, lab.dest, NOW).ok
    assert find_candidates(lab.cat) == []


@pytest.mark.component
def test_prune_leaves_a_file_whose_content_no_longer_matches(lab: Lab) -> None:
    from drivecon.prune import find_candidates, prune

    _backed_up_then_excluded(lab)
    (lab.dest / "Files/Tools/sample/a.docx").write_bytes(b"edited by hand")
    res = prune(lab.cat, lab.dest, find_candidates(lab.cat))
    assert res.removed == 1 and res.mismatched == ["Files/Tools/sample/a.docx"]
    assert (lab.dest / "Files/Tools/sample/a.docx").read_bytes() == b"edited by hand"
