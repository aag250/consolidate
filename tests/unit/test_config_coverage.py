"""Meta-tests: every config entry must be exercised by a generated fixture file."""

import re
from pathlib import Path

import pytest

from drivecon.config import load_file_types, load_path_rules
from drivecon.mockdevices import build_tree
from drivecon.rules import Rules

CONFIG = Path(__file__).parent.parent.parent / "config"
FT = load_file_types(CONFIG / "file-types.toml")
PR = load_path_rules(CONFIG / "paths.toml")


def example_for(pattern: str) -> str:
    """A concrete file path under ``pattern`` that is otherwise an Include type."""
    base = pattern.replace("**/", "a/").replace("*", "x")
    return base + "report.docx" if pattern.endswith("/") else base


@pytest.mark.unit
def test_every_extension_and_pattern_has_a_fixture(tmp_path: Path) -> None:
    files = []
    for cat in FT.categories:
        files += [(f"{cat.name}/file.{e}", cat.decision, e) for e in cat.extensions]
        for ex in cat.pattern_examples:
            files.append((f"{cat.name}/file.{ex}", cat.decision, ex))
        for pat in cat.patterns:  # each pattern needs at least one matching example
            assert any(re.fullmatch(pat, ex) for ex in cat.pattern_examples), pat
    spec = {"files": [{"path": p, "size": 8} for p, _, _ in files]}
    made = build_tree(spec, tmp_path)
    assert len(made) == len(files)
    rules = Rules(FT, PR)
    for path, decision, _ in files:
        assert rules.classify(path, 8).action == decision, path


@pytest.mark.unit
def test_no_extension_is_listed_twice() -> None:
    seen: dict[str, str] = {}
    for cat in FT.categories:
        for e in cat.extensions:
            assert e not in seen, f"{e} in {cat.name} and {seen[e]}"
            seen[e] = cat.name


@pytest.mark.unit
def test_every_exclusion_pattern_positive_and_negative() -> None:
    for exc in PR.exclusions:
        rules = Rules(FT, PR, os_kind="windows" if exc.os == "windows" else "linux")
        for pat in exc.patterns:
            d = rules.classify(example_for(pat), 8)
            assert (d.action, d.rule) == ("exclude", pat), pat
        for neg in exc.non_examples:
            assert rules.classify(neg, 8).action == "include", neg


@pytest.mark.unit
def test_every_exception_rescues_a_pst() -> None:
    rules = Rules(FT, PR)
    for pat in PR.exceptions:
        d = rules.classify(example_for(pat.replace("*.pst", "mail")) + ".pst", 8)
        assert d.rule == f"exception:{pat}", pat


@pytest.mark.unit
def test_every_junk_name_is_dropped() -> None:
    rules = Rules(FT, PR)
    for pat in PR.junk.names:
        name = pat.replace("*", "x")
        assert rules.classify(f"docs/{name}", 8).action == "junk", pat
