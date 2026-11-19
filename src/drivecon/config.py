"""Load the rule configs and write unknown-extension answers back, keeping comments."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import tomlkit


@dataclass(frozen=True)
class Category:
    name: str
    decision: str  # "include" | "exclude"
    extensions: tuple[str, ...]
    patterns: tuple[str, ...] = ()
    pattern_examples: tuple[str, ...] = ()
    notes: str = ""


@dataclass(frozen=True)
class FileTypes:
    categories: tuple[Category, ...]
    decided: dict[str, str] = field(default_factory=dict)  # ext -> include|exclude


@dataclass(frozen=True)
class Exclusion:
    scope: str
    os: str  # "windows" | "linux" | "any"
    patterns: tuple[str, ...]
    non_examples: tuple[str, ...] = ()
    notes: str = ""


@dataclass(frozen=True)
class Junk:
    names: tuple[str, ...]
    zero_byte: bool
    min_image_bytes: int
    image_extensions: tuple[str, ...]


@dataclass(frozen=True)
class PhotoRules:
    extensions: tuple[str, ...] = ()
    camera_folders: tuple[str, ...] = ()
    camera_folder_regex: str = r"$^"


@dataclass(frozen=True)
class PathRules:
    exclusions: tuple[Exclusion, ...]
    exceptions: tuple[str, ...]
    junk: Junk
    strip_prefixes: tuple[str, ...] = ()
    photos: PhotoRules = PhotoRules()


def load_file_types(path: Path) -> FileTypes:
    doc = tomlkit.parse(path.read_text())
    cats = tuple(
        Category(
            name=str(c["name"]),
            decision=str(c["decision"]),
            extensions=tuple(str(e).lower() for e in c.get("extensions", [])),
            patterns=tuple(str(p) for p in c.get("patterns", [])),
            pattern_examples=tuple(str(p) for p in c.get("pattern_examples", [])),
            notes=str(c.get("notes", "")),
        )
        for c in doc.get("category", [])
    )
    decided = {
        str(d["ext"]).lower(): str(d["decision"])
        for d in doc.get("decided", [])
        if d["decision"] in ("include", "exclude")
    }
    return FileTypes(cats, decided)


def load_path_rules(path: Path) -> PathRules:
    doc: Any = tomlkit.parse(path.read_text())
    exclusions = tuple(
        Exclusion(
            scope=str(e["scope"]),
            os=str(e["os"]),
            patterns=tuple(str(p) for p in e["patterns"]),
            non_examples=tuple(str(p) for p in e.get("non_examples", [])),
            notes=str(e.get("notes", "")),
        )
        for e in doc.get("exclusion", [])
    )
    exceptions = tuple(str(e["pattern"]) for e in doc.get("exception", []))
    j = doc["junk"]
    junk = Junk(
        names=tuple(str(n) for n in j["names"]),
        zero_byte=bool(j["zero_byte"]),
        min_image_bytes=int(j["min_image_bytes"]),
        image_extensions=tuple(str(e).lower() for e in j["image_extensions"]),
    )
    merge = doc.get("merge", {})
    ph = doc.get("photos", {})
    photos = PhotoRules(
        extensions=tuple(str(e).lower() for e in ph.get("extensions", [])),
        camera_folders=tuple(str(f) for f in ph.get("camera_folders", [])),
        camera_folder_regex=str(ph.get("camera_folder_regex", r"$^")),
    )
    prefixes = tuple(str(p) for p in merge.get("strip_prefixes", []))
    return PathRules(exclusions, exceptions, junk, prefixes, photos)


def record_decision(path: Path, ext: str, decision: str, example: str, today: dt.date) -> None:
    """Append an answered prompt to ``[[decided]]``, preserving comments and order."""
    if decision not in ("include", "exclude"):
        raise ValueError(f"decision must be include or exclude, not {decision!r}")
    doc = tomlkit.parse(path.read_text())
    entry = tomlkit.table()
    entry.add("ext", ext.lower())
    entry.add("decision", decision)
    entry.add("date", today.isoformat())
    entry.add("example", example)
    if "decided" not in doc:
        doc.add("decided", tomlkit.aot())
    doc["decided"].append(entry)
    path.write_text(tomlkit.dumps(doc))
