"""Generate synthetic source trees from declarative specs (no real data)."""

from __future__ import annotations

import hashlib
import random
from pathlib import Path
from typing import Any

import yaml


def _content(seed: int, path: str, size: int) -> bytes:
    """Deterministic pseudo-random bytes derived from seed, path and size."""
    rng = random.Random(f"{seed}:{path}")
    return rng.randbytes(size)


def build_tree(spec: dict[str, Any], dest: Path) -> list[Path]:
    """Create the files described by ``spec`` under ``dest``; return created paths.

    A file entry is ``{path, size?, key?, same_as?, content?}``. ``key`` seeds the
    content (default: the path), so entries with the same key and size hold identical
    bytes. ``same_as`` reuses the content of another entry's path.
    """
    seed = int(spec.get("seed", 0))
    made: list[Path] = []
    by_path: dict[str, bytes] = {}
    for entry in spec.get("files", []):
        rel = entry["path"]
        if "content" in entry:
            data = str(entry["content"]).encode()
        elif "same_as" in entry:
            data = by_path[entry["same_as"]]
        else:
            data = _content(seed, str(entry.get("key", rel)), int(entry.get("size", 64)))
        by_path[rel] = data
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        made.append(target)
    return made


def load_spec(path: Path) -> dict[str, Any]:
    loaded = yaml.safe_load(path.read_text())
    if not isinstance(loaded, dict):
        raise ValueError(f"{path}: spec must be a mapping")
    return loaded


def tree_digest(root: Path) -> str:
    """Stable digest of every file path and content under ``root``."""
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        if p.is_file():
            h.update(str(p.relative_to(root)).encode())
            h.update(p.read_bytes())
    return h.hexdigest()
