"""System tests against real filesystems on loop devices (root required)."""

import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.system.imagemock import Lab, require_loop_devices, sh

ROOT = Path(__file__).parent.parent.parent
pytestmark = [pytest.mark.system, pytest.mark.image]


@pytest.fixture
def lab(tmp_path: Path) -> Iterator[Lab]:
    require_loop_devices()
    lab = Lab(tmp_path)
    yield lab
    lab.close()


class Env:
    def __init__(self, tmp: Path, dest: Path) -> None:
        self.tmp, self.dest = tmp, dest
        self.srv = tmp / "srv"
        self.config = tmp / "config"
        shutil.copytree(ROOT / "config", self.config)
        wrapper = tmp / "drivecon-wrapper"
        wrapper.write_text(
            '#!/usr/bin/env bash\nargs=("${@//\\/intake\\//' + str(self.srv) + '/intake/}")\n'
            f'exec {sys.executable} -m drivecon "${{args[@]}}"\n'
        )
        wrapper.chmod(0o755)
        self.env = {
            **os.environ,
            "DRIVECON_SRV": str(self.srv),
            "DRIVECON_CMD": str(wrapper),
            "DRIVECON_DEST": str(dest),
            "DRIVECON_CONFIG": str(self.config),
            "DRIVECON_SOURCES": str(self.srv / "sources"),
        }

    def cli(self, *args: str, **extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "drivecon", *args],
            env={**self.env, **extra}, stdin=subprocess.DEVNULL, capture_output=True,
            text=True, timeout=300,
        )  # fmt: skip

    def intake(self, device: str, label: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(ROOT / "scripts" / "intake.sh"), device, label],
            env=self.env, capture_output=True, text=True, timeout=120,
        )  # fmt: skip


def populate(mount: Path, files: dict[str, bytes]) -> None:
    for rel, data in files.items():
        p = mount / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)


COMMON = {
    "docs/report.docx": b"report body " * 50,
    "finance/taxes/w2.pdf": b"%PDF w2 " * 80,
    "photos/trip/a.jpg": b"\xff\xd8\xff not really a jpeg " * 10,
}
LINUX_ONLY = {  # legal on ext4, hostile on NTFS
    "docs/a:b.txt": b"colon",
    "docs/what?.doc": b"question",
    "docs/CON.txt": b"reserved",
    "docs/trailing.": b"dot",
}


@pytest.fixture
def ntfs_dest(lab: Lab, tmp_path: Path) -> Path:
    _, dev = lab.image("ntfs", 64, "DEST")
    return lab.mount(dev, "ntfs", tmp_path / "dest")


@pytest.mark.parametrize("fs", ["ntfs", "exfat", "vfat", "ext4"])
def test_real_intake_and_copy_from_each_filesystem(
    lab: Lab, tmp_path: Path, ntfs_dest: Path, fs: str
) -> None:
    disk, dev = lab.image(fs, 64, "SRC", partitioned=True)
    staging = lab.mount(dev, fs, tmp_path / "stage")
    files = {**COMMON, **(LINUX_ONLY if fs == "ext4" else {})}
    populate(staging, files)
    lab.umount(staging)

    e = Env(tmp_path, ntfs_dest)
    r = e.intake(disk, "Src-1")
    lab.track_umount(e.srv / "sources" / "Src-1")
    assert r.returncode == 0, r.stdout + r.stderr
    info = json.loads((e.srv / "intake" / "Src-1.json").read_text())
    assert info["blkid"]["TYPE"] == {"vfat": "vfat", "ntfs": "ntfs"}.get(fs, fs)
    sheet = (ntfs_dest / "_intake" / "Src-1.txt").read_text()
    assert "SMART        not available" in sheet and "Src-1" in sheet

    mount = e.srv / "sources" / "Src-1"
    with pytest.raises(OSError):  # sources are read-only twice over
        (mount / "docs" / "new.txt").write_text("x")

    res = e.cli("run", "Src-1")
    assert res.returncode == 0, res.stdout + res.stderr
    dest_files = {
        p.relative_to(ntfs_dest).as_posix() for p in (ntfs_dest / "Files").rglob("*") if p.is_file()
    }
    assert {
        "Files/docs/report.docx",
        "Files/finance/taxes/w2.pdf",
        "Files/photos/trip/a.jpg",
    } <= dest_files
    if fs == "ext4":
        assert {
            "Files/docs/a_b.txt",
            "Files/docs/what_.doc",
            "Files/docs/_CON.txt",
            "Files/docs/trailing",
        } <= dest_files
        renamed = e.cli("report", "types")  # command exists; names are logged in the catalog
        assert renamed.returncode == 0
    assert e.cli("verify").returncode == 0
    for name in (ntfs_dest / "Files" / "docs").iterdir():  # no name NTFS would treat specially
        assert not re.search(r'[\\:*?"<>|]', name.name)


def test_read_errors_from_a_failing_device_are_logged_and_retried(
    lab: Lab, tmp_path: Path, ntfs_dest: Path
) -> None:
    disk = sh("losetup", "-f", "--show", _blank(tmp_path / "ext4.img", 64)).stdout.strip()
    lab._undo.append(lambda: sh("losetup", "-d", disk, check=False))
    sectors = int(sh("blockdev", "--getsz", disk).stdout)
    dm = f"mock{os.getpid()}"
    sh("dmsetup", "create", dm, "--table", f"0 {sectors} linear {disk} 0")
    lab._undo.append(lambda: sh("dmsetup", "remove", "-f", dm, check=False))
    sh("dmsetup", "mknodes")  # no udev in containers to create the node
    dev = f"/dev/mapper/{dm}"
    sh("mkfs.ext4", "-q", "-b", "4096", "-L", "FLAKY", dev)
    stage = lab.mount(dev, "ext4", tmp_path / "stage")
    populate(stage, {**COMMON, "big/scan.pdf": os.urandom(2 << 20)})
    sh("sync")
    ext = sh("filefrag", "-v", str(stage / "big/scan.pdf")).stdout
    m = re.search(r"^\s*0:\s+0\.\.\s*\d+:\s+(\d+)\.\.\s*(\d+):", ext, re.M)
    assert m, ext
    start, end = int(m[1]) * 8, (int(m[2]) + 1) * 8
    lab.umount(stage)

    def table(bad: bool) -> str:
        if not bad:
            return f"0 {sectors} linear {disk} 0"
        return (
            f"0 {start} linear {disk} 0\n{start} {end - start} error\n"
            f"{end} {sectors - end} linear {disk} {end}"
        )

    def load(bad: bool) -> None:
        sh("dmsetup", "suspend", dm)
        subprocess.run(["dmsetup", "load", dm], input=table(bad), text=True, check=True)
        sh("dmsetup", "resume", dm)
        sh("blockdev", "--flushbufs", dev)

    load(bad=True)
    e = Env(tmp_path, ntfs_dest)
    r = e.intake(dev, "Flaky-1")
    lab.track_umount(e.srv / "sources" / "Flaky-1")
    assert r.returncode == 0, r.stdout + r.stderr
    res = e.cli("run", "Flaky-1")
    assert res.returncode == 0, res.stdout + res.stderr  # a read error never stops a run
    assert (ntfs_dest / "Files/docs/report.docx").exists()
    assert not (ntfs_dest / "Files/big/scan.pdf").exists()
    assert "big/scan.pdf" in e.cli("report", "errors").stdout

    load(bad=False)  # the device recovers
    assert e.cli("retry-errors", "Flaky-1").returncode == 0
    assert e.cli("run", "Flaky-1").returncode == 0
    assert (ntfs_dest / "Files/big/scan.pdf").stat().st_size == 2 << 20
    assert "No errors." in e.cli("report", "errors").stdout
    assert e.cli("verify").returncode == 0


def _blank(path: Path, mb: int) -> Path:
    sh("truncate", "-s", f"{mb}M", path)
    return path


def test_destination_fills_up_mid_copy_then_resumes(lab: Lab, tmp_path: Path) -> None:
    _, dev = lab.image("ntfs", 12, "TINY")
    dest = lab.mount(dev, "ntfs", tmp_path / "dest")
    filler = dest / "filler.bin"
    filler.write_bytes(os.urandom(6 << 20))
    src = tmp_path / "srv" / "sources" / "Big-1"
    for i in range(6):
        (src / "docs").mkdir(parents=True, exist_ok=True)
        (src / "docs" / f"f{i}.docx").write_bytes(os.urandom(1 << 20))
    e = Env(tmp_path, dest)
    assert e.cli("intake", "--label", "Big-1", "--source-dir", str(src)).returncode == 0
    first = e.cli("run", "Big-1")
    assert first.returncode == 4, first.stdout + first.stderr
    assert "full" in first.stderr.lower()
    assert not list((dest / "Files").rglob(".dc-*"))  # no partial file left behind
    done = len([p for p in (dest / "Files").rglob("*.docx")])
    assert 0 < done < 6
    filler.unlink()  # free space
    second = e.cli("run", "Big-1")
    assert second.returncode == 0, second.stdout + second.stderr
    assert len(list((dest / "Files").rglob("*.docx"))) == 6
    assert e.cli("verify").returncode == 0
