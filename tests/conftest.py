from pathlib import Path

import pytest

from drivecon.config import load_file_types, load_path_rules
from drivecon.rules import Rules

CONFIG = Path(__file__).parent.parent / "config"


@pytest.fixture
def rules() -> Rules:
    return Rules(
        load_file_types(CONFIG / "file-types.toml"),
        load_path_rules(CONFIG / "paths.toml"),
        os_kind="windows",
    )
