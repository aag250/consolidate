"""Run the real backup-drive.sh against stub host tools (no root, no real devices)."""

import os
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent.parent

# name, type, fstype, uuid
DISK = [
    ("/dev/sdd1", "part", "vfat", "44B8-2DF7"),
    ("/dev/sdd2", "part", "ntfs", "2C5E3AD35E3A9614"),
    ("/dev/sdd3", "part", "swap", "aaaa-swap"),
    ("/dev/sdd4", "part", "", ""),
]


def stub(bin_dir: Path, name: str, body: str) -> None:
    p = bin_dir / name
    p.write_text("#!/usr/bin/env bash\n" + body)
    p.chmod(p.stat().st_mode | stat.S_IEXEC)


class Host:
    def __init__(self, tmp: Path, disk: list[tuple[str, str, str, str]] = DISK) -> None:
        self.tmp, self.log = tmp, tmp / "log"
        b = tmp / "bin"
        b.mkdir()
        (tmp / "parts").write_text("".join(f"{n} {t}\n" for n, t, _, _ in disk))
        (tmp / "blkid").write_text("".join(f"{n} {f or '-'} {u or '-'}\n" for n, _, f, u in disk))
        (tmp / "mounts").write_text("")
        (tmp / "known").write_text("")
        log = f'echo "$(basename "$0")|$(IFS="|"; echo "$*")" >> {self.log}'
        stub(
            b,
            "lsblk",
            f"""
dev=${{@: -1}}
case "$*" in
  "-dn "*) [ "$dev" = /dev/nope ] && exit 32; echo x ;;
  "-dno TYPE "*) case $dev in *[0-9]) echo part ;; *) echo disk ;; esac ;;
  "-no PKNAME "*) echo sdd ;;
  "-dno MODEL "*) echo "INTEL SSDSC2BW180H6  " ;;
  "-dno SERIAL "*) echo CVTR511601SY180BGN ;;
  "-lnpo NAME,TYPE "*) cat {tmp}/parts ;;
esac
""",
        )
        stub(
            b,
            "blkid",
            f"""
dev=${{@: -1}} field=$5
line=$(grep "^$dev " {tmp}/blkid) || exit 2
set -- $line
v=$2; [ "$field" = UUID ] && v=$3
[ "$v" = - ] && exit 2
echo "$v"
""",
        )
        stub(b, "mountpoint", f"[ ! -f {tmp}/nodest ]")
        stub(
            b,
            "findmnt",
            f"""
case "$1" in
  -no) echo /dev/sdb1 ;;
  -rno) grep "^${{@: -1}} " {tmp}/mounts | cut -d" " -f2- ;;
  -n) grep -q " $2 " {tmp}/mounts ;;
esac
""",
        )
        stub(b, "umount", log)
        stub(b, "mount", log)
        stub(b, "intake", log)
        stub(b, "cmd", log)
        stub(
            b,
            "query",
            f'[ "$1" = report ] && {{ echo "No unknown extensions were skipped."; exit 0; }}; '
            f'grep "^${{@: -1}} " {tmp}/known | cut -d" " -f2',
        )
        self.env = {
            **os.environ,
            "PATH": f"{b}:{os.environ['PATH']}",
            "DRIVECON_SRV": str(tmp / "srv"),
            "DRIVECON_DEST_DIR": str(tmp / "dest"),
            "DRIVECON_CMD": str(b / "cmd"),
            "DRIVECON_QUERY_CMD": str(b / "query"),
            "DRIVECON_INTAKE": str(b / "intake"),
            "DRIVECON_ALLOW_NONROOT": "1",
            "DRIVECON_NO_INHIBIT": "1",
            "DRIVECON_UMOUNT_RETRY_SECS": "0",
        }

    def run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(ROOT / "scripts" / "backup-drive.sh"), *args],
            env=self.env, text=True, capture_output=True,
        )  # fmt: skip

    def calls(self, tool: str) -> list[list[str]]:
        if not self.log.exists():
            return []
        lines = self.log.read_text().splitlines()
        return [ln.split("|")[1:] for ln in lines if ln.split("|")[0] == tool]


@pytest.mark.system
def test_every_partition_with_a_filesystem_is_registered_and_run(tmp_path: Path) -> None:
    h = Host(tmp_path)
    r = h.run("/dev/sdd", "--strip-prefix", "KG 10.23.21 Backup/Documents/")
    assert r.returncode == 0, r.stderr
    base = "INTEL-SSDSC2BW180H6-0BGN"
    assert h.calls("intake") == [
        ["/dev/sdd", f"{base}-p1", "--partition", "/dev/sdd1",
         "--strip-prefix", "KG 10.23.21 Backup/Documents/"],
        ["/dev/sdd", f"{base}-p2", "--partition", "/dev/sdd2",
         "--strip-prefix", "KG 10.23.21 Backup/Documents/"],
    ]  # fmt: skip
    assert h.calls("cmd") == [
        ["run", f"{base}-p1"],
        ["run", f"{base}-p2"],
        ["report", "sources"],
    ]
    assert "skipped: /dev/sdd3 (swap)" in r.stdout
    assert "skipped: /dev/sdd4 (no filesystem)" in r.stdout
    assert "can be unplugged" in r.stdout


@pytest.mark.system
def test_a_registered_partition_keeps_its_label(tmp_path: Path) -> None:
    h = Host(tmp_path)
    (tmp_path / "known").write_text("44B8-2DF7 intel-ssd-2\n")
    r = h.run("/dev/sdd", "--label", "intel-ssd")
    assert r.returncode == 0, r.stderr
    assert [c[1] for c in h.calls("intake")] == ["intel-ssd-2", "intel-ssd-p2"]


@pytest.mark.system
def test_other_mounts_are_released_but_a_read_only_source_mount_is_kept(tmp_path: Path) -> None:
    h = Host(tmp_path)
    srv = tmp_path / "srv" / "sources"
    (tmp_path / "mounts").write_text(
        "/dev/sdd2 /run/media/u/New\\x20Volume rw,relatime\n"
        "/dev/sdd2 /src/sources/oops ro,relatime\n"
        f"/dev/sdd1 {srv}/d-p1 ro,relatime\n"
    )
    r = h.run("/dev/sdd", "--label", "d")
    assert r.returncode == 0, r.stderr
    unmounted = [c[0] for c in h.calls("umount")]
    assert unmounted[:2] == ["/run/media/u/New Volume", "/src/sources/oops"]
    assert f"{srv}/d-p1" not in unmounted[:2]  # kept for intake, released only at the end


@pytest.mark.system
def test_refuses_when_the_destination_is_not_mounted(tmp_path: Path) -> None:
    h = Host(tmp_path)
    (tmp_path / "nodest").touch()
    r = h.run("/dev/sdd")
    assert r.returncode == 2 and "not mounted" in r.stderr
    assert h.calls("intake") == [] and h.calls("cmd") == []


@pytest.mark.system
def test_refuses_a_partition_or_a_missing_disk(tmp_path: Path) -> None:
    h = Host(tmp_path)
    r = h.run("/dev/sdd2")
    assert r.returncode == 2 and "give the whole disk (/dev/sdd)" in r.stderr
    assert h.run("/dev/nope").returncode == 2
    assert h.calls("intake") == []


@pytest.mark.system
def test_dry_run_mounts_read_only_and_registers_nothing(tmp_path: Path) -> None:
    h = Host(tmp_path)
    r = h.run("/dev/sdd", "--label", "d", "--dry-run")
    assert r.returncode == 0, r.stderr
    assert h.calls("intake") == []
    assert h.calls("cmd") == [["dry-run", "d-p1", "--os", "windows"],
                              ["dry-run", "d-p2", "--os", "windows"]]  # fmt: skip
    assert all(c[:2] == ["-o", "ro"] for c in h.calls("mount"))


@pytest.mark.system
def test_a_failed_intake_stops_before_copying(tmp_path: Path) -> None:
    h = Host(tmp_path)
    stub(tmp_path / "bin", "intake", "exit 1")
    r = h.run("/dev/sdd", "--label", "d")
    assert r.returncode == 1 and "PROBLEM: d-p1 (intake exit 1)" in r.stderr
    assert ["run", "d-p1"] not in h.calls("cmd")


@pytest.mark.system
def test_a_bare_disk_filesystem_is_one_source_without_suffix(tmp_path: Path) -> None:
    h = Host(tmp_path, disk=[])
    (tmp_path / "blkid").write_text("/dev/sdd exfat 1234-ABCD\n")
    r = h.run("/dev/sdd", "--label", "stick")
    assert r.returncode == 0, r.stderr
    assert h.calls("intake") == [["/dev/sdd", "stick", "--partition", "/dev/sdd"]]


@pytest.mark.system
def test_rejects_a_label_that_would_look_like_an_option(tmp_path: Path) -> None:
    h = Host(tmp_path)
    r = h.run("/dev/sdd", "--label", "-intel-ssd")
    assert r.returncode == 2 and "must start with a letter or digit" in r.stderr
    assert h.calls("intake") == []


@pytest.mark.system
def test_error_message_does_not_include_the_exit_code(tmp_path: Path) -> None:
    r = Host(tmp_path).run("/dev/nope")
    assert r.returncode == 2 and r.stderr.strip() == "backup-drive: /dev/nope: no such disk"


@pytest.mark.system
def test_the_end_lists_unknown_types_skipped_on_each_partition(tmp_path: Path) -> None:
    h = Host(tmp_path)
    stub(tmp_path / "bin", "query",
         'case "$1" in report) [ "$3" = d-p2 ] && echo "d-p2  .xyz  3 files  1 KB" '
         '|| echo "No unknown extensions were skipped." ;; *) exit 0 ;; esac')  # fmt: skip
    r = h.run("/dev/sdd", "--label", "d")
    assert r.returncode == 0, r.stderr
    assert "Unknown file types skipped on d-p2:" in r.stdout and ".xyz" in r.stdout
    assert "skipped on d-p1" not in r.stdout


@pytest.mark.system
def test_a_busy_mount_is_reported_the_other_partitions_still_run_and_mounts_are_released(
    tmp_path: Path,
) -> None:
    h = Host(tmp_path)
    srv = tmp_path / "srv" / "sources"
    (tmp_path / "mounts").write_text(
        f"/dev/sdd1 {srv}/d-p1 ro,relatime\n/dev/sdd2 /run/media/u/BUSY rw,relatime\n"
    )
    busy = '[ "$1" = /run/media/u/BUSY ] && exit 32; exit 0'
    stub(tmp_path / "bin", "umount", f'echo "umount|$1" >> {tmp_path}/log; {busy}')
    stub(tmp_path / "bin", "fuser", 'echo "anthony  4242 ..c.. dolphin"')
    r = h.run("/dev/sdd", "--label", "d")
    assert r.returncode == 1
    assert "/run/media/u/BUSY is in use" in r.stderr and "dolphin" in r.stderr
    assert "PROBLEM: d-p2 (/dev/sdd2 is in use elsewhere; nothing copied from it)" in r.stderr
    assert [c[1] for c in h.calls("intake")] == ["d-p1"]  # p1 still done; p2 never registered
    assert [f"{srv}/d-p1"] == [c[0] for c in h.calls("umount") if c[0].startswith(str(srv))]
