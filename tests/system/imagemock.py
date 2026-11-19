"""Small real disk images on loop devices (needs root), for system tests."""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

MKFS = {
    "ntfs": ["mkfs.ntfs", "-F", "-Q", "-L"],
    "exfat": ["mkfs.exfat", "-L"],
    "vfat": ["mkfs.vfat", "-n"],
    "ext4": ["mkfs.ext4", "-q", "-b", "4096", "-L"],
}
MOUNT = {
    "ntfs": ["mount", "-t", "ntfs-3g"],
    "exfat": ["mount.exfat-fuse"],
    "vfat": ["mount"],
    "ext4": ["mount"],
}


def sh(*args: str | Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    res = subprocess.run([str(a) for a in args], capture_output=True, text=True)
    if check and res.returncode != 0:
        raise RuntimeError(f"{' '.join(map(str, args))} failed ({res.returncode}): {res.stderr}")
    return res


def require_loop_devices() -> None:
    if os.geteuid() != 0:
        pytest.skip("image mocks need root: run with sudo or in a privileged container")
    for tool in ("losetup", "mkfs.ext4", "mkfs.ntfs", "mkfs.exfat", "mkfs.vfat", "sfdisk"):
        if not shutil.which(tool):
            pytest.skip(f"image mocks need {tool}")
    probe = sh("losetup", "-f", check=False)
    if probe.returncode != 0:
        pytest.skip("no loop devices available: " + probe.stderr.strip())


class Lab:
    """Creates images, loop devices, mounts and dm devices; tears all down in reverse."""

    def __init__(self, base: Path) -> None:
        self.base = base
        self._undo: list[Callable[[], object]] = []
        self.n = 0

    def close(self) -> None:
        while self._undo:
            with contextlib.suppress(Exception):
                self._undo.pop()()
        sh("sync", check=False)

    def image(
        self, fs: str, size_mb: int = 64, label: str = "MOCK", partitioned: bool = False
    ) -> tuple[str, str]:
        """Make a formatted image. Returns (disk device, device holding the filesystem)."""
        self.n += 1
        img = self.base / f"{fs}-{self.n}.img"
        sh("truncate", "-s", f"{size_mb}M", img)
        if partitioned:
            subprocess.run(
                ["sfdisk", "--quiet", str(img)],
                input="start=2048, type=7\n",
                text=True,
                check=True,
                capture_output=True,
            )
        disk = sh("losetup", "-f", "--show", *(["-P"] if partitioned else []), img).stdout.strip()
        self._undo.append(lambda: sh("losetup", "-d", disk, check=False))
        target = disk
        if partitioned:
            target = disk + "p1"
            node = Path(f"/sys/block/{Path(disk).name}/{Path(target).name}/dev")
            if not Path(target).exists():
                major, minor = node.read_text().strip().split(":")
                sh("mknod", target, "b", major, minor)
        sh(*MKFS[fs], label, target)
        return disk, target

    def mount(self, device: str, fs: str, mountpoint: Path, ro: bool = False) -> Path:
        mountpoint.mkdir(parents=True, exist_ok=True)
        opts = ["-o", "ro"] if ro else []
        sh(*MOUNT[fs], *opts, device, mountpoint)
        self._undo.append(lambda: sh("umount", mountpoint, check=False))
        return mountpoint

    def track_umount(self, mountpoint: Path) -> None:
        """Unmount ``mountpoint`` on teardown, before the devices under it are detached."""
        self._undo.append(lambda: sh("umount", mountpoint, check=False))

    def umount(self, mountpoint: Path) -> None:
        sh("umount", mountpoint)
