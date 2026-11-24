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


def use_ask_mode(config_dir: Path) -> None:
    """Switch a copied config to unknown = "ask", for tests of the held-and-prompt flow."""
    p = config_dir / "file-types.toml"
    text = p.read_text()
    assert 'unknown = "exclude"' in text
    p.write_text(text.replace('unknown = "exclude"', 'unknown = "ask"', 1))
