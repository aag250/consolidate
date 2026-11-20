import os
from pathlib import Path

import pytest

from drivecon.config import load_file_types, load_path_rules
from drivecon.rules import Rules

# Tests use plain temp directories as sources; tests of the read-only check unset this.
os.environ.setdefault("DRIVECON_ALLOW_WRITABLE_SOURCES", "1")

CONFIG = Path(__file__).parent.parent / "config"


@pytest.fixture
def rules() -> Rules:
    return Rules(
        load_file_types(CONFIG / "file-types.toml"),
        load_path_rules(CONFIG / "paths.toml"),
        os_kind="windows",
    )
