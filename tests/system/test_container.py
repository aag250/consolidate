"""Build the runtime image and run one small scenario inside it."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from drivecon.mockdevices import build_tree

ROOT = Path(__file__).parent.parent.parent
IMAGE = "drivecon-smoke"

FILES = [
    {"path": "Users/u1/Documents/finance/w2.pdf", "key": "w2", "size": 300},
    {"path": "Users/u1/Desktop/w2.pdf", "key": "w2", "size": 300},
    {"path": "Users/u1/Documents/a:b.txt", "content": "hostile name"},
    {"path": "Users/u1/Documents/README", "content": "plain text, no extension"},  # libmagic
    {"path": "Windows/skip.dll", "size": 9},
]


def docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, check=check, timeout=600
    )


@pytest.fixture(scope="module")
def image() -> str:
    if not shutil.which("docker") or docker("info", check=False).returncode != 0:
        pytest.skip("docker is not available")
    docker("build", "--target", "runtime", "-t", IMAGE, str(ROOT))
    return IMAGE


@pytest.mark.container
def test_runtime_image_runs_a_drive_end_to_end(image: str, tmp_path: Path) -> None:
    sources, dest, config = tmp_path / "sources", tmp_path / "dest", tmp_path / "config"
    build_tree({"seed": 5, "files": FILES}, sources / "M1")
    dest.mkdir()
    shutil.copytree(ROOT / "config", config)
    user = f"{os.getuid()}:{os.getgid()}"

    def run(*args: str) -> subprocess.CompletedProcess[str]:
        return docker(
            "run", "--rm", "--user", user,
            "-v", f"{sources}:/sources:ro", "-v", f"{dest}:/dest", "-v", f"{config}:/config",
            "-e", "DRIVECON_DEST=/dest", "-e", "DRIVECON_CONFIG=/config",
            "-e", "DRIVECON_SOURCES=/sources", "-w", "/tmp", image, *args,
            check=False,
        )  # fmt: skip

    assert run("intake", "--label", "M1", "--source-dir", "/sources/M1").returncode == 0
    res = run("run", "M1")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "M1: safe to retire" in res.stdout
    files = {p.relative_to(dest).as_posix() for p in (dest / "Files").rglob("*") if p.is_file()}
    assert files == {"Files/finance/w2.pdf", "Files/a_b.txt", "Files/README"}  # README via libmagic
    assert run("verify").returncode == 0
    assert "M1" in run("report", "sources").stdout
    assert "finance/w2.pdf" in run("where", "Desktop/w2.pdf").stdout
    # Sources are read-only inside the container.
    ro = docker(
        "run", "--rm", "--entrypoint", "sh", "-v", f"{sources}:/sources:ro", image,
        "-c", "touch /sources/M1/new.txt", check=False,
    )  # fmt: skip
    assert ro.returncode != 0
    assert not (sources / "M1" / "new.txt").exists()
    # Runs as a non-root user by default.
    who = docker("run", "--rm", "--entrypoint", "id", image, "-u")
    assert who.stdout.strip() != "0"
