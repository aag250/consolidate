import datetime as dt
import shutil
from pathlib import Path

import pytest

from drivecon.config import load_file_types, record_decision
from drivecon.rules import Rules, get_ext

CONFIG = Path(__file__).parent.parent.parent / "config"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("path", "action", "rule"),
    [
        ("Users/u1/Documents/budget.XLSX", "include", "xlsx"),
        ("Users/u1/Documents/return.tax2011", "include", r"tax\d{4}"),
        ("Users/u1/AppData/Roaming/x/notes.docx", "exclude", "Users/*/AppData/"),
        (
            "Users/u1/AppData/Local/Microsoft/Outlook/a.pst",
            "include",
            "exception:Users/*/AppData/Local/Microsoft/Outlook/*.pst",
        ),
        ("Users/u1/AppData/Local/Microsoft/Outlook/a.docx", "exclude", "Users/*/AppData/"),
        ("Windows/System32/x.docx", "exclude", "Windows/"),
        ("Users/u1/Pictures/Thumbs.db", "junk", "name:Thumbs.db"),
        ("Users/u1/Documents/~$report.docx", "junk", "name:~$*"),
        ("Users/u1/Documents/app.exe", "exclude", "exe"),
        ("Users/u1/Documents/data.weird", "pending", "unknown:weird"),
        ("Users/u1/Documents/README", "sniff", "no-extension"),
        ("proj/node_modules/pkg/readme.txt", "exclude", "**/node_modules/"),
        ("Dev/notes.txt", "include", "txt"),  # Linux rules must not hit a Windows drive
        # A profile copied into a backup folder, not under Users/
        ("bak_2026/joe/AppData/Local/Google/x/data.docx", "exclude", "**/AppData/Local/"),
        ("bak_2026/joe/AppData/Roaming/x/notes.docx", "exclude", "**/AppData/Roaming/"),
        (
            "bak_2026/joe/AppData/Local/Microsoft/Outlook/a.pst",
            "include",
            "exception:**/AppData/Local/Microsoft/Outlook/*.pst",
        ),
        ("bak_2026/joe/Documents/AppData/notes.docx", "include", "docx"),
        ("bak_2026/joe/.vscode/extensions/ms-python/x.docx", "exclude", "**/.vscode/extensions/"),
        ("Users/u1/Music/song.boy (mode 3 - ch 16)", "sniff", "no-extension"),
    ],
)
def test_classify(rules: Rules, path: str, action: str, rule: str) -> None:
    d = rules.classify(path, size=100)
    assert (d.action, d.rule) == (action, rule)


@pytest.mark.unit
def test_zero_byte_is_junk(rules: Rules) -> None:
    assert rules.classify("a/b.docx", 0).rule == "zero-byte"


@pytest.mark.unit
def test_case_insensitive_exclusion(rules: Rules) -> None:
    assert rules.classify("WINDOWS/x.docx", 5).action == "exclude"


@pytest.mark.unit
def test_get_ext() -> None:
    assert (get_ext("a.TAR.gz"), get_ext(".bashrc"), get_ext("noext"), get_ext("x.")) == (
        "gz",
        "",
        "",
        "",
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("name", "ext"),
    [
        ("model.3mf", "3mf"),
        ("archive.7z", "7z"),
        ("backup.043", "043"),
        ("app.log.1", "1"),
        ("Jump.automaticDestinations-ms", "automaticdestinations-ms"),
        ("vimrc.un~", "un~"),
        ("song.boy (mode 3 - ch 16)", ""),
        ("Notes 3.5 (copy)", ""),
        ("index.0-07218068cfa519613b646f716c6dc5b8db19e3779fe69bbb567d349ccd549385", ""),
        ("blob.567f5df81ea0c9bdcfb7221f0ea091893150f8c16e3012e4f0314ba3d43f1632", ""),
        ("events.2021-11-18t22-44-52z-00000", ""),
        ("cache.32_13356250686655436", ""),
        ("cache.4_13359585971024130", ""),
        ("data.0000000001", ""),
        ("recovery.jsonlz4-20260203002554", ""),
        ("ms-toolsai.jupyter-2023.7.1002162226-win32-x64", ""),
        ("x.0-0", ""),
    ],
)
def test_get_ext_ignores_suffixes_that_are_not_extensions(name: str, ext: str) -> None:
    assert get_ext(name) == ext


@pytest.mark.unit
def test_record_decision_keeps_comments_and_applies(tmp_path: Path) -> None:
    cfg = tmp_path / "file-types.toml"
    shutil.copy(CONFIG / "file-types.toml", cfg)
    before = cfg.read_text()
    earlier = load_file_types(cfg).decided  # answers already recorded in the shipped config
    record_decision(cfg, "WEIRD", "include", "a/b.weird", dt.date(2026, 11, 18))
    after = cfg.read_text()
    assert after.startswith(before.rstrip("\n"))
    assert "# Answers to unknown-extension prompts" in after
    assert load_file_types(cfg).decided == {**earlier, "weird": "include"}


@pytest.mark.unit
def test_record_decision_rejects_decide_later(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        record_decision(tmp_path / "x.toml", "a", "later", "p", dt.date(2026, 1, 1))
