"""Interrupt the real `sync-mirror` at each crash point and resume."""

import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from drivecon.mockdevices import build_tree
from tests.system.test_crash import M1, M2, Case

SERIAL = "AAAA-1111"


def files_of(root: Path) -> dict[str, str]:
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in (root / "Files").rglob("*")
        if p.is_file()
    }


@pytest.fixture(scope="module")
def shared(tmp_path_factory: pytest.TempPathFactory) -> Path:
    d = tmp_path_factory.mktemp("shared")
    shutil.copytree(Path(__file__).parent.parent.parent / "config", d / "config")
    build_tree({"seed": 11, "files": M1}, d / "sources" / "M1")
    build_tree({"seed": 11, "files": M2}, d / "sources" / "M2")
    return d


@pytest.fixture
def primed(shared: Path, tmp_path: Path) -> tuple[Case, Path]:
    c = Case(tmp_path, shared)
    c.finish("M1")
    c.finish("M2")
    mirror = tmp_path / "mirror"
    mirror.mkdir()
    c.env["DRIVECON_MIRROR"] = str(mirror)
    res = c.cli("mirror", "register", "Sec-A", "--serial", SERIAL)
    assert res.returncode == 0, res.stderr
    return c, mirror


def finish_sync(c: Case, fault: str | None = None) -> int:
    first = c.cli("sync-mirror", fault=fault).returncode
    rc = first
    for _ in range(5):
        if rc == 0:
            break
        rc = c.cli("sync-mirror").returncode
    assert rc == 0
    return first


def assert_in_step(c: Case, mirror: Path) -> None:
    assert files_of(mirror) == files_of(c.dest)
    assert not list(mirror.rglob(".dc-*"))
    res = c.cli("verify", "--mirror")
    assert res.returncode == 0, res.stdout + res.stderr


@pytest.mark.system
def test_clean_sync_matches_primary_and_carries_a_catalog(primed: tuple[Case, Path]) -> None:
    c, mirror = primed
    assert finish_sync(c) == 0
    assert_in_step(c, mirror)
    assert (mirror / "_catalog" / "catalog.sqlite").exists()
    assert (mirror / "_intake" / "M1.txt").exists() and (mirror / "_reports").is_dir()
    assert "up to date" in c.cli("report", "sources").stdout


@pytest.mark.system
@pytest.mark.parametrize("action", ["kill", "sigint"])
@pytest.mark.parametrize(
    "fault",
    [
        "sync.after_temp_write:3",
        "sync.after_fsync:3",
        "sync.after_rename:3",
        "catalog.after_commit:2",
        "sync.before_catalog_copy:1",
    ],
)
def test_interrupted_sync_resumes_to_the_same_result(
    primed: tuple[Case, Path], fault: str, action: str
) -> None:
    c, mirror = primed
    assert finish_sync(c, fault=f"{fault}:{action}") != 0, "fault never fired"
    assert_in_step(c, mirror)


@pytest.mark.system
def test_secondary_unplugged_mid_sync_then_replugged(
    primed: tuple[Case, Path], tmp_path: Path
) -> None:
    c, mirror = primed
    assert c.cli("sync-mirror", fault="sync.after_rename:3:kill").returncode != 0
    unplugged = tmp_path / "unplugged"
    mirror.rename(unplugged)
    mirror.mkdir()  # an empty mount point, as when the drive is gone
    res = c.cli("sync-mirror")
    assert res.returncode == 2 and "not a registered secondary" in res.stderr
    shutil.rmtree(mirror)
    unplugged.rename(mirror)
    assert c.cli("sync-mirror").returncode == 0
    assert_in_step(c, mirror)


@pytest.mark.system
def test_corruption_on_the_secondary_is_caught_by_verify(primed: tuple[Case, Path]) -> None:
    c, mirror = primed
    finish_sync(c)
    victim = next((mirror / "Files").rglob("w2.pdf"))
    victim.write_bytes(b"silent corruption")
    res = c.cli("verify", "--mirror")
    assert res.returncode == 1 and "CORRUPT" in res.stdout


@pytest.mark.system
def test_secondary_is_promoted_and_a_replacement_synced_from_it(
    primed: tuple[Case, Path], tmp_path: Path
) -> None:
    c, mirror = primed
    finish_sync(c)
    golden = files_of(c.dest)
    shutil.rmtree(c.dest)  # the primary drive dies
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    env = {**c.env, "DRIVECON_DEST": str(mirror), "DRIVECON_MIRROR": str(fresh)}

    def run(*a: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "drivecon", *a], env=env, capture_output=True, text=True,
            stdin=subprocess.DEVNULL,
        )  # fmt: skip

    assert run("mirror", "register", "Sec-B", "--serial", "BBBB-2222").returncode == 0
    assert run("sync-mirror").returncode == 0
    assert files_of(fresh) == golden
    assert run("verify", "--mirror").returncode == 0
