import os
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from drivecon.cli import app
from drivecon.mockdevices import build_tree

ROOT = Path(__file__).parent.parent.parent


@pytest.fixture
def setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    monkeypatch.delenv("DRIVECON_ALLOW_WRITABLE_SOURCES")  # exercise the real check
    sources, dest, config = tmp_path / "sources", tmp_path / "dest", tmp_path / "config"
    shutil.copytree(ROOT / "config", config)
    build_tree({"seed": 1, "files": [{"path": "docs/a.docx", "size": 5}]}, sources / "M1")
    common = ["--dest", str(dest)]
    r = CliRunner().invoke(
        app, ["intake", "--label", "M1", "--source-dir", str(sources / "M1"), *common]
    )
    assert r.exit_code == 0
    return [*common, "--config", str(config), "--sources-root", str(sources)]


@pytest.mark.component
@pytest.mark.parametrize("cmd", ["scan", "hash", "run", "dry-run", "copy"])
def test_a_writable_source_is_refused(
    setup: list[str], cmd: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dest, config, sources = setup[1], setup[3], setup[5]
    options = {
        "scan": ["--dest", dest, "--config", config, "--sources-root", sources],
        "run": ["--dest", dest, "--config", config, "--sources-root", sources],
        "copy": ["--dest", dest, "--config", config, "--sources-root", sources],
        "hash": ["--dest", dest, "--sources-root", sources],
        "dry-run": ["--config", config, "--sources-root", sources],
    }
    args = [cmd, "M1", *options[cmd]]
    res = CliRunner().invoke(app, args)
    assert res.exit_code == 2 and "mounted read-write" in res.output
    assert not (tmp_path / "dest" / "Files").exists()


@pytest.mark.component
def test_a_read_only_mount_is_accepted(setup: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    real = os.statvfs

    def ro(path: str | os.PathLike[str]) -> os.statvfs_result:
        st = real(path)
        return os.statvfs_result((*st[:8], st.f_flag | os.ST_RDONLY, st.f_namemax))

    monkeypatch.setattr("drivecon.cli.os.statvfs", ro)
    res = CliRunner().invoke(app, ["scan", "M1", *setup])
    assert res.exit_code == 0, res.output


@pytest.mark.component
def test_run_writes_a_dated_catalog_backup(tmp_path: Path) -> None:
    sources, dest, config = tmp_path / "sources", tmp_path / "dest", tmp_path / "config"
    shutil.copytree(ROOT / "config", config)
    build_tree({"seed": 1, "files": [{"path": "docs/a.docx", "size": 5}]}, sources / "M1")
    common = ["--dest", str(dest)]
    r = CliRunner()
    r.invoke(app, ["intake", "--label", "M1", "--source-dir", str(sources / "M1"), *common])
    res = r.invoke(
        app, ["run", "M1", *common, "--config", str(config), "--sources-root", str(sources)]
    )
    assert res.exit_code == 0, res.output
    backups = list((dest / "_catalog" / "backups").glob("catalog-*.sqlite"))
    assert len(backups) == 1
