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
from drivecon.console import CONSOLE
from drivecon.hashing import HashWorker
from drivecon.pending import PendingInfo, PendingTracker, release_decided
from drivecon.rules import Rules
from drivecon.scan import Scanner, ScanStats, Sniffer, sniff_mime

JOIN_TIMEOUT = 30.0


def human(n: int) -> str:
    from drivecon.intake import human as h

    return h(n)


PROMPT = "[i]nclude, [e]xclude, [d]ecide later"


def ask_extension(info: PendingInfo) -> str:
    """Prompt for one unknown extension. Returns include, exclude or later.

    Progress lines are held back while the question is open, and the question is
    repeated if it waits (see drivecon.console), so it never scrolls out of sight.
    """
    lines = [
        f"\nUnknown extension '.{info.ext}': {info.count} files, {human(info.total_bytes)} so far"
    ]
    lines += [f"    e.g. {ex}" for ex in info.examples]
    while True:
        try:
            answer = CONSOLE.ask(lines, PROMPT).strip().lower()[:1]
        except EOFError:
            raise typer.Abort() from None
        if answer in ("i", "e", "d"):
            break
        lines = ["Please answer i, e or d."]
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
        finally:
            CONSOLE.finished("scan")

    def hash_work() -> None:
        assert hasher is not None
        hasher.run()

    scan_w = _Worker("scan", work, errors)
    hash_w = _Worker("hash", hash_work, errors)
    result = SessionResult(scanner.stats)
    CONSOLE.expect("scan", *(["hash"] if hasher else []))
    if hasher:
        hasher.stop = stop
        hash_w.start()
    scan_w.start()
    # The caller closes the catalog once this returns or raises, so no path out of here
    # may leave a worker running: closing the connection under a statement in flight
    # crashes the interpreter (SIGSEGV in _sqlite3), it does not raise.
    try:
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
                elif scan_w.done:
                    break  # only after a last look for prompts the scan raised at its end
                else:
                    scan_w.wait(0.1)
        except (KeyboardInterrupt, typer.Abort):
            result.interrupted = True
            stop.set()
        scan_w.finish()
        if errors:
            raise errors[0]
        with cat.lock:
            result.released = release_decided(cat, rules, cat.clock())
            cat.commit()
        if hasher:
            hasher.scan_done.set()  # released files are now 'seen'; hash the stragglers
            try:
                while not hash_w.wait(0.1):
                    pass
            except KeyboardInterrupt:
                result.interrupted = True
                stop.set()
            hash_w.finish()
            if errors:
                raise errors[0]
    except BaseException:
        stop.set()
        scan_w.finish(quiet=True)
        hash_w.finish(quiet=True)
        raise
    return result


class _Worker:
    """A daemon thread whose exit is signalled by an Event set in its own finally.

    Thread.join() and is_alive() are deliberately not used to wait. On Python 3.12 a
    KeyboardInterrupt landing inside join() marks the thread stopped although it is
    still running; is_alive() then says False and later joins return at once, so the
    caller would close the catalog under a live worker. An interrupted Event.wait()
    leaves nothing behind.
    """

    def __init__(self, name: str, target: Callable[[], None], errors: list[BaseException]):
        self.name = name
        self.exited = threading.Event()
        self.started = False

        def body() -> None:
            try:
                target()
            except BaseException as e:  # reported on the main thread after it waits
                errors.append(e)
            finally:
                self.exited.set()

        self.thread = threading.Thread(target=body, name=name, daemon=True)

    def start(self) -> None:
        self.started = True
        self.thread.start()

    @property
    def done(self) -> bool:
        return not self.started or self.exited.is_set()

    def wait(self, timeout: float) -> bool:
        """True once the worker has exited (or if it was never started)."""
        return not self.started or self.exited.wait(timeout)

    def finish(self, *, quiet: bool = False) -> None:
        """Wait for a worker that has been told to stop, or has run out of work."""
        if not self.wait(JOIN_TIMEOUT) and not quiet:
            raise RuntimeError(f"{self.name} worker did not stop in time")
