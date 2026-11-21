"""Run the real intake.sh against stub host tools (no root, no real devices)."""

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent.parent
FIX = ROOT / "tests" / "fixtures"


def stub(bin_dir: Path, name: str, body: str) -> None:
    p = bin_dir / name
    p.write_text("#!/usr/bin/env bash\n" + body)
    p.chmod(p.stat().st_mode | stat.S_IEXEC)


def setup(tmp_path: Path, mount_opts: str = "ro,relatime") -> tuple[Path, dict[str, str]]:
    b = tmp_path / "bin"
    b.mkdir()
    lsblk = json.loads((FIX / "device_healthy_usb.json").read_text())["lsblk"]
    (tmp_path / "lsblk.json").write_text(json.dumps(lsblk))
    stub(b, "lsblk", f"cat {tmp_path}/lsblk.json")
    stub(
        b,
        "blkid",
        'case "${@: -1}" in /dev/sdb1)'
        ' printf "UUID=1A2B3C4D5E6F7788\\nTYPE=ntfs\\nLABEL=OS\\n";; *) exit 2;; esac',
    )
    stub(b, "smartctl", f"cat {FIX}/smart/healthy.json")
    stub(
        b,
        "findmnt",
        f'[ "$1" = -n ] && [ "$2" != -o ] && [ ! -f {tmp_path}/mounted ] && exit 1; '
        f"echo {mount_opts}",
    )
    stub(b, "mount", f'echo "$@" > {tmp_path}/mount.args; touch {tmp_path}/mounted')
    stub(b, "df", "printf 'Used\\n123456\\n'")
    stub(b, "fakecmd", f"printf '%s\\n' \"$@\" > {tmp_path}/container.args")
    env = {
        **os.environ,
        "PATH": f"{b}:{os.environ['PATH']}",
        "DRIVECON_SRV": str(tmp_path / "srv"),
        "DRIVECON_CMD": str(b / "fakecmd"),
        "DRIVECON_ALLOW_NONROOT": "1",
    }
    return b, env


def run(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(ROOT / "scripts" / "intake.sh"), *args], env=env, text=True, capture_output=True
    )


@pytest.mark.system
def test_intake_sh_collects_json_mounts_ro_and_calls_container(tmp_path: Path) -> None:
    _, env = setup(tmp_path)
    r = run(env, "/dev/sdb", "Dell-2009-C")
    assert r.returncode == 0, r.stderr
    assert (tmp_path / "mount.args").read_text().split()[:2] == ["-o", "ro"]
    doc = json.loads((tmp_path / "srv" / "intake" / "Dell-2009-C.json").read_text())
    assert doc["blkid"]["UUID"] == "1A2B3C4D5E6F7788" and doc["used_bytes"] == 123456
    assert doc["partition"] == "/dev/sdb1" and doc["smart"]["smart_status"]["passed"]
    args = (tmp_path / "container.args").read_text().split()
    assert args == ["intake", "--label", "Dell-2009-C", "--device-info", "/intake/Dell-2009-C.json"]


@pytest.mark.system
def test_intake_sh_refuses_a_writable_mount(tmp_path: Path) -> None:
    _, env = setup(tmp_path, mount_opts="rw,relatime")
    r = run(env, "/dev/sdb", "L1")
    assert r.returncode == 1 and "not mounted read-only" in r.stderr
    assert not (tmp_path / "container.args").exists()


@pytest.mark.system
def test_intake_sh_rejects_bad_label_and_non_root(tmp_path: Path) -> None:
    _, env = setup(tmp_path)
    assert run(env, "/dev/sdb", "bad/label").returncode == 2
    env.pop("DRIVECON_ALLOW_NONROOT")
    if os.getuid() != 0:
        assert run(env, "/dev/sdb", "L1").returncode == 1


@pytest.mark.system
def test_intake_sh_uses_given_smart_json(tmp_path: Path) -> None:
    _, env = setup(tmp_path)
    r = run(env, "/dev/sdb", "L1", "--smart-json", str(FIX / "smart" / "failed.json"))
    assert r.returncode == 0, r.stderr
    doc = json.loads((tmp_path / "srv" / "intake" / "L1.json").read_text())
    assert doc["smart"]["smart_status"]["passed"] is False


@pytest.mark.system
def test_intake_sh_passes_strip_prefixes_through(tmp_path: Path) -> None:
    _, env = setup(tmp_path)
    r = run(env, "/dev/sdb", "L1", "--strip-prefix", "Backup 2012/joe/", "--strip-prefix", "old/")
    assert r.returncode == 0, r.stderr
    args = (tmp_path / "container.args").read_text().splitlines()
    assert args[-4:] == ["--strip-prefix", "Backup 2012/joe/", "--strip-prefix", "old/"]
