"""Classify a source path: path exceptions, path exclusions, junk rules, file types."""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from typing import Literal

from drivecon.config import FileTypes, PathRules

Action = Literal["include", "exclude", "junk", "pending", "sniff"]


@dataclass(frozen=True)
class Decision:
    action: Action
    rule: str  # the pattern, extension or rule name that decided
    ext: str = ""


def split_path(path: str) -> list[str]:
    return [seg for seg in path.split("/") if seg]


_EXT_CHARS = re.compile(r"[a-z0-9][a-z0-9_+~-]*")
_EXT_ID = re.compile(r"\d{10,}|[0-9a-f]{16,}")  # timestamps, counters, hashes


def get_ext(name: str) -> str:
    """Lower-case extension without the dot; '' for no extension or dot-files.

    Text after the last dot only counts as an extension when it looks like one. Names
    such as ``song.boy (mode 3 - ch 16)``, ``cache.0-07218068cf...``, ``log.2021-11-18t22-44z``
    or ``data.32_13356250686655436`` have none, so they are sniffed instead of each
    becoming its own unknown-extension prompt.
    """
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem:
        return ""
    ext = ext.lower()
    if not _EXT_CHARS.fullmatch(ext) or _EXT_ID.search(ext):
        return ""
    if ext[0].isdigit() and ("-" in ext or "_" in ext):  # 0-0, 4_1335..., 5-win32-x64
        return ""
    return ext


def _seg_match(pat: str, seg: str, ci: bool) -> bool:
    if ci:
        return fnmatch.fnmatchcase(seg.lower(), pat.lower())
    return fnmatch.fnmatchcase(seg, pat)


def match_segments(pat: list[str], path: list[str], ci: bool, whole: bool) -> bool:
    """Glob-match segments; ``whole`` requires the pattern to consume the full path."""
    if not pat:
        return True if not whole else not path
    head, rest = pat[0], pat[1:]
    if head == "**":
        return any(match_segments(rest, path[i:], ci, whole) for i in range(len(path) + 1))
    if not path or not _seg_match(head, path[0], ci):
        return False
    return match_segments(rest, path[1:], ci, whole)


class Rules:
    def __init__(
        self,
        file_types: FileTypes,
        path_rules: PathRules,
        os_kind: Literal["windows", "linux"] = "windows",
        case_insensitive: bool = True,
    ) -> None:
        self._ci = case_insensitive
        self._os = os_kind
        self._pr = path_rules
        self._ext: dict[str, tuple[str, str]] = {}  # ext -> (decision, rule)
        self._patterns: list[tuple[re.Pattern[str], str, str]] = []
        for cat in file_types.categories:
            for e in cat.extensions:
                self._ext[e] = (cat.decision, e)
            for p in cat.patterns:
                self._patterns.append((re.compile(p), cat.decision, p))
        for e, d in file_types.decided.items():
            self._ext[e] = (d, f"decided:{e}")

    def file_type(self, ext: str) -> tuple[str, str] | None:
        if ext in self._ext:
            return self._ext[ext]
        for rx, decision, pat in self._patterns:
            if rx.fullmatch(ext):
                return decision, pat
        return None

    def add_decision(self, ext: str, decision: str) -> None:
        """Apply an answered prompt for the rest of this run."""
        self._ext[ext.lower()] = (decision, f"decided:{ext.lower()}")

    def excluded_dir(self, segs: list[str]) -> str | None:
        """The exclusion pattern that prunes the directory ``segs``, if any."""
        for exc in self._pr.exclusions:
            if exc.os not in ("any", self._os):
                continue
            for pat in exc.patterns:
                if match_segments(split_path(pat), segs, self._ci, whole=False):
                    return pat
        return None

    def resolve_ext(self, ext: str, rule_prefix: str = "") -> Decision:
        """Decide by extension alone (used for sniffed files)."""
        ftype = self.file_type(ext)
        if ftype is None:
            return Decision("pending", f"unknown:{ext}", ext)
        return Decision(ftype[0], rule_prefix + ftype[1], ext)  # type: ignore[arg-type]

    def classify(self, path: str, size: int) -> Decision:
        segs = split_path(path)
        if not segs:
            raise ValueError("empty path")
        name = segs[-1]
        ext = get_ext(name)
        ftype = self.file_type(ext) if ext else None

        if ftype and ftype[0] == "include":
            for pat in self._pr.exceptions:
                if match_segments(split_path(pat), segs, self._ci, whole=True):
                    return Decision("include", f"exception:{pat}", ext)

        for exc in self._pr.exclusions:
            if exc.os not in ("any", self._os):
                continue
            for pat in exc.patterns:
                if match_segments(split_path(pat), segs, self._ci, whole=False):
                    return Decision("exclude", pat, ext)

        junk = self._pr.junk
        for pat in junk.names:
            if _seg_match(pat, name, self._ci):
                return Decision("junk", f"name:{pat}", ext)
        if junk.zero_byte and size == 0:
            return Decision("junk", "zero-byte", ext)
        if junk.min_image_bytes and ext in junk.image_extensions and size < junk.min_image_bytes:
            return Decision("junk", "image-floor", ext)

        if not ext:
            return Decision("sniff", "no-extension")
        if ftype is None:
            return Decision("pending", f"unknown:{ext}", ext)
        return Decision(ftype[0], ftype[1], ext)  # type: ignore[arg-type]
