import datetime as dt
import os
import shutil
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest

from drivecon import fault
from drivecon.catalog import Catalog
from drivecon.config import load_file_types, load_path_rules
from drivecon.mockdevices import build_tree, load_spec
from drivecon.pending import PendingInfo, PendingTracker
from drivecon.rules import Rules
from drivecon.scan import Scanner, ScanStats
from drivecon.session import SessionResult, run_scan_session

ROOT = Path(__file__).parent.parent.parent
DEVICES = ROOT / "tests" / "fixtures" / "devices"
TODAY = dt.date(2026, 11, 18)
NOW = dt.datetime(2026, 11, 18, 12, 0, 0)


class Env:
    def __init__(self, tmp: Path, spec: str | None = "win7") -> None:
        self.config = tmp / "config"
        shutil.copytree(ROOT / "config", self.config)
        self.root = tmp / "src"
        self.root.mkdir()
        if spec:
            build_tree(load_spec(DEVICES / f"{spec}.yaml"), self.root)
        self.cat = Catalog(tmp / "dest", clock=lambda: NOW)
        self.sid = self.cat.register_source("S1", "uuid-1", fs_type="ntfs")
        self.rules = self.new_rules()

    def new_rules(self) -> Rules:
        return Rules(
            load_file_types(self.config / "file-types.toml"),
            load_path_rules(self.config / "paths.toml"),
        )

    def scan(self, **kw: Any) -> ScanStats:
        return Scanner(self.cat, self.sid, self.root, self.rules, PendingTracker(), **kw).run()

    def files(self) -> dict[str, str]:
        rows = self.cat.conn.execute("SELECT rel_path, status FROM files ORDER BY rel_path")
        return {r[0]: r[1] for r in rows}

    def excluded(self) -> dict[tuple[str, str], tuple[int, int]]:
        rows = self.cat.conn.execute(
            "SELECT rel_path, rule_matched, file_count, total_bytes FROM excluded_dirs"
        )
        return {(r[0], r[1]): (r[2], r[3]) for r in rows}


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


@pytest.mark.component
def test_scan_records_included_and_counts_excluded(env: Env) -> None:
    st = env.scan()
    assert env.files() == {
        "Users/u1/Documents/report.docx": "seen",
        "Users/u1/Documents/data.weird": "pending_decision",
        "Users/u1/Documents/data2.weird": "pending_decision",
        "Users/u1/AppData/Local/Microsoft/Outlook/mail.pst": "seen",  # rescued
        "Users/u1/Pictures/photo.jpg": "seen",
    }
    ex = env.excluded()
    assert ex[("Users/u1/AppData", "Users/*/AppData/")] == (2, 15)  # notes.docx + other.dat
    assert ex[("Program Files", "Program Files/")] == (1, 5)
    assert st.included == 3 and st.pending == 2 and st.excluded_files == 3


@pytest.mark.component
def test_pending_extensions_table_is_refreshed(env: Env) -> None:
    env.scan()
    row = env.cat.conn.execute(
        "SELECT file_count, total_bytes, decision FROM pending_extensions WHERE ext='weird'"
    ).fetchone()
    assert row == (2, 50, None)


@pytest.mark.component
def test_rescan_is_idempotent_and_keeps_progress(env: Env) -> None:
    env.scan()
    before, ex = env.files(), env.excluded()
    env.cat.conn.execute(
        "UPDATE files SET status='copied', hash='h' WHERE rel_path LIKE '%report.docx'"
    )
    env.scan()
    assert env.files() == {**before, "Users/u1/Documents/report.docx": "copied"}
    assert env.excluded() == ex
    # A changed file goes back to 'seen' with its hash cleared.
    (env.root / "Users/u1/Documents/report.docx").write_bytes(b"changed contents")
    env.scan()
    row = env.cat.conn.execute(
        "SELECT status, hash FROM files WHERE rel_path LIKE '%report.docx'"
    ).fetchone()
    assert row == ("seen", None)


@pytest.mark.component
def test_resume_after_crash_between_batches_matches_uninterrupted_run(
    env: Env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    golden = Env(tmp_path / "g")
    golden.scan()
    fault.reset()
    monkeypatch.setenv("CONSOLIDATE_FAULT", "scan.batch_commit:1:raise")
    with pytest.raises(fault.FaultInjected):
        env.scan(batch_size=2)
    env.cat.conn.rollback()
    monkeypatch.delenv("CONSOLIDATE_FAULT")
    env.scan(batch_size=2)
    assert env.files() == golden.files() and env.excluded() == golden.excluded()


@pytest.mark.component
@pytest.mark.skipif(sys.platform == "win32", reason="raw non-UTF-8 byte names are POSIX-only")
def test_non_utf8_name_is_kept_as_raw_bytes(tmp_path: Path) -> None:
    e = Env(tmp_path, spec=None)
    with open(os.path.join(os.fsencode(e.root), b"caf\xe9.txt"), "wb") as f:
        f.write(b"x")
    e.scan()
    raw = e.cat.conn.execute("SELECT rel_path_raw FROM files").fetchone()[0]
    assert raw == b"caf\xe9.txt"


@pytest.mark.component
def test_extensionless_files_are_sniffed(tmp_path: Path) -> None:
    e = Env(tmp_path, spec=None)
    for name in ("notes", "prog", "thing"):
        (e.root / name).write_bytes(b"data")
    mimes = {
        "notes": "text/plain",
        "prog": "application/x-dosexec",
        "thing": "application/x-foo",
    }
    e.scan(sniffer=lambda p: mimes[Path(p).name])
    rows = {
        r[0]: r[1:]
        for r in e.cat.conn.execute("SELECT rel_path, status, rule_matched, ext, mime FROM files")
    }
    assert rows == {
        "notes": ("seen", "sniffed:txt", "txt", "text/plain"),
        "thing": (
            "pending_decision",
            "unknown:mime:application/x-foo",
            "mime:application/x-foo",
            "application/x-foo",
        ),
    }
    assert e.excluded()[("", "sniffed:exe")] == (1, 4)


@pytest.mark.component
def test_stop_event_ends_scan_cleanly(env: Env) -> None:
    import threading

    stop = threading.Event()
    stop.set()
    st = env.scan(stop=stop)
    assert st.files_seen == 0
    assert env.cat.conn.execute("SELECT last_scan_at FROM sources").fetchone()[0] is None


def session(env: Env, answers: dict[str, str] | None, **kw: Any) -> SessionResult:
    def ask(info: PendingInfo) -> str:
        assert answers is not None
        return answers[info.ext]

    return run_scan_session(
        env.cat, env.sid, env.root, env.rules, env.config / "file-types.toml",
        interactive=answers is not None, today=TODAY, ask=ask, **kw,
    )  # fmt: skip


@pytest.mark.component
def test_include_answer_is_written_to_config_and_releases_held_files(env: Env) -> None:
    res = session(env, {"weird": "include"})
    assert res.answered == {"weird": "include"} and res.released == 2
    assert env.files()["Users/u1/Documents/data.weird"] == "seen"
    text = (env.config / "file-types.toml").read_text()
    assert 'ext = "weird"' in text and "2026-11-18" in text and ".weird" in text
    assert "weird" in load_file_types(env.config / "file-types.toml").decided
    row = env.cat.conn.execute("SELECT decision FROM pending_extensions").fetchone()
    assert row == ("include",)


@pytest.mark.component
def test_exclude_answer_drops_rows_into_counts(env: Env) -> None:
    session(env, {"weird": "exclude"})
    assert not any(k.endswith(".weird") for k in env.files())
    assert env.excluded()[("Users/u1/Documents", "decided:weird")] == (2, 50)


@pytest.mark.component
def test_decide_later_keeps_files_held_and_config_untouched(env: Env) -> None:
    before = (env.config / "file-types.toml").read_text()
    res = session(env, {"weird": "later"})
    assert res.deferred == ["weird"] and res.released == 0
    assert env.files()["Users/u1/Documents/data.weird"] == "pending_decision"
    assert (env.config / "file-types.toml").read_text() == before


@pytest.mark.component
def test_non_interactive_run_queues_prompts(env: Env) -> None:
    res = session(env, None)
    assert not res.answered
    assert env.cat.conn.execute("SELECT ext FROM pending_extensions").fetchall() == [("weird",)]


@pytest.mark.component
def test_interrupt_during_prompt_stops_cleanly_and_keeps_held_files(env: Env) -> None:
    def ask(info: PendingInfo) -> str:
        raise KeyboardInterrupt

    res = run_scan_session(
        env.cat, env.sid, env.root, env.rules, env.config / "file-types.toml",
        interactive=True, today=TODAY, ask=ask,
    )  # fmt: skip
    assert res.interrupted
    held = [k for k, v in env.files().items() if v == "pending_decision"]
    assert len(held) <= 2
    assert env.cat.conn.execute("SELECT COUNT(*) FROM files WHERE ext='weird'").fetchone()[
        0
    ] == len(held)
    conn = sqlite3.connect(env.cat.dest_file)
    assert conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == len(env.files())
