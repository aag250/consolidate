"""Run a scan in a worker thread while the main thread asks about unknown extensions."""

from __future__ import annotations

import datetime as dt
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import typer

from drivecon.catalog import Catalog
from drivecon.config import record_decision
from drivecon.hashing import HashWorker
from drivecon.pending import PendingInfo, PendingTracker, release_decided
from drivecon.rules import Rules
from drivecon.scan import Scanner, ScanStats, Sniffer, sniff_mime

JOIN_TIMEOUT = 30.0


def human(n: int) -> str:
    from drivecon.intake import human as h

    return h(n)


def ask_extension(info: PendingInfo) -> str:
    """Prompt for one unknown extension. Returns include, exclude or later."""
    typer.echo(
        f"\nUnknown extension '.{info.ext}': {info.count} files, {human(info.total_bytes)} so far"
    )
    for ex in info.examples:
        typer.echo(f"    e.g. {ex}")
    while True:
        answer = typer.prompt("[i]nclude, [e]xclude, [d]ecide later").strip().lower()[:1]
        if answer in ("i", "e", "d"):
            break
        typer.echo("Please answer i, e or d.")
    return {"i": "include", "e": "exclude", "d": "later"}[answer]


@dataclass
class SessionResult:
    stats: ScanStats
    answered: dict[str, str] = field(default_factory=dict)
    deferred: list[str] = field(default_factory=list)
    interrupted: bool = False
    released: int = 0


def apply_answer(
    ext: str, decision: str, example: str, rules: Rules, config_path: Path, today: dt.date
) -> None:
    record_decision(config_path, ext, decision, example, today)
    rules.add_decision(ext, decision)


def run_scan_session(
    cat: Catalog,
    source_id: int,
    root: Path,
    rules: Rules,
    config_path: Path,
    *,
    interactive: bool,
    today: dt.date,
    ask: Callable[[PendingInfo], str] = ask_extension,
    sniffer: Sniffer = sniff_mime,
    progress: Callable[[ScanStats], None] | None = None,
    progress_every: int = 1000,
    throttle: float = 0.0,
    batch_size: int = 1000,
    hasher: HashWorker | None = None,
) -> SessionResult:
    tracker = PendingTracker()
    stop = threading.Event()
    scanner = Scanner(
        cat, source_id, root, rules, tracker, sniffer=sniffer, batch_size=batch_size,
        stop=stop, progress=progress, progress_every=progress_every, throttle=throttle,
    )  # fmt: skip
    errors: list[BaseException] = []

    def work() -> None:
        try:
            scanner.run()
        except BaseException as e:  # reported on the main thread after the join
            errors.append(e)

    def hash_work() -> None:
        try:
            assert hasher is not None
            hasher.run()
        except BaseException as e:
            errors.append(e)

    thread = threading.Thread(target=work, name="scan", daemon=True)
    hash_thread = threading.Thread(target=hash_work, name="hash", daemon=True)
    result = SessionResult(scanner.stats)
    if hasher:
        hasher.stop = stop
        hash_thread.start()
    thread.start()
    try:
        while True:
            info = tracker.next_unasked() if interactive else None
            if info is not None:
                decision = ask(tracker.snapshot(info.ext))
                if decision == "later":
                    result.deferred.append(info.ext)
                else:
                    example = tracker.snapshot(info.ext).examples[0]
                    apply_answer(info.ext, decision, example, rules, config_path, today)
                    result.answered[info.ext] = decision
            elif not thread.is_alive():
                break
            else:
                thread.join(0.1)
    except (KeyboardInterrupt, typer.Abort):
        result.interrupted = True
        stop.set()
    thread.join(JOIN_TIMEOUT)
    if thread.is_alive():
        raise RuntimeError("scan worker did not stop in time")
    if errors:
        raise errors[0]
    with cat.lock:
        result.released = release_decided(cat, rules, cat.clock())
        cat.commit()
    if hasher:
        hasher.scan_done.set()  # released files are now 'seen'; hash the stragglers, then stop
        hash_thread.join(JOIN_TIMEOUT if stop.is_set() else None)
        if hash_thread.is_alive():
            raise RuntimeError("hash worker did not stop in time")
        if errors:
            raise errors[0]
    return result
