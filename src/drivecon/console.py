"""Terminal output shared by the scan, hash and copy workers and the prompts.

Workers report through one Console so that:

- progress lines never bury an open question: while a prompt waits, status updates are
  kept, not printed, and the latest one per phase is printed once the answer is given;
- an open question is repeated every ``remind_every`` seconds, and says so plainly once
  the background work has finished and only the answer is holding the run up;
- a heartbeat prints at least every ``heartbeat`` seconds with the file each phase is on,
  so a long file never looks like a hang;
- each line carries a rough percentage and time left where a total is known.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

# A progress line at least this often, even when the per-file count is not reached
# (one large file, or a long excluded tree).
PROGRESS_SECS = float(os.environ.get("CONSOLIDATE_PROGRESS_SECS", "10"))


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1000 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1000
    return f"{n:.1f} TB"  # pragma: no cover


def duration(seconds: float) -> str:
    s = int(max(seconds, 0))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"


@dataclass
class Meter:
    """Rough progress of one phase: done and total in bytes, timed from the first update."""

    started: float | None = None
    base: int = 0  # done at the first update: earlier runs' work, not counted in the rate
    done: int = 0
    total: int | None = None  # None while unknown
    final_total: bool = False  # False while the total can still grow (hashing during a scan)

    def update(self, done: int, now: float, total: int | None = None) -> None:
        if self.started is None:
            self.started, self.base = now, done
        self.done = done
        if total is not None:
            self.total = total

    def summary(self, now: float) -> str:
        """'~42%, ~12m left' when a total is known; '' otherwise."""
        if not self.total or self.started is None:
            return ""
        frac = min(self.done / self.total, 0.99 if self.done < self.total else 1.0)
        pct = f"~{frac * 100:.0f}%"
        if not self.final_total:
            return f"{pct} of {human(self.total)} found so far"
        elapsed = now - self.started
        rate = (self.done - self.base) / elapsed if elapsed > 0 else 0.0
        if rate <= 0 or elapsed < 5 or frac >= 1.0:
            return pct
        left = (self.total - self.done) / rate
        return f"{pct}, ~{duration(left)} left"


@dataclass
class _Prompt:
    lines: list[str]
    prompt: str
    opened: float
    reminded: float


@dataclass
class Console:
    out: Callable[[str], None] = field(default=lambda s: print(s, flush=True))
    clock: Callable[[], float] = time.monotonic
    heartbeat: float = field(
        default_factory=lambda: float(os.environ.get("CONSOLIDATE_HEARTBEAT", "30"))
    )
    remind_every: float = field(
        default_factory=lambda: float(os.environ.get("CONSOLIDATE_REMIND_EVERY", "60"))
    )

    def __post_init__(self) -> None:
        self._lock = threading.RLock()
        self._latest: dict[str, str] = {}  # phase -> last status line
        self._held: dict[str, str] = {}  # status lines kept back while a prompt is open
        self._current: dict[str, tuple[str, int, float]] = {}  # phase -> (path, size, since)
        self._finished: set[str] = set()
        self._expected: set[str] = set()  # phases that must finish before "only the answer is left"
        self._prompt: _Prompt | None = None
        self._last_out = self.clock()
        self.meters: dict[str, Meter] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def reset(self) -> None:
        """Forget everything from an earlier command (tests run several in one process)."""
        self.stop()
        self.__post_init__()

    def is_finished(self, phase: str) -> bool:
        return phase in self._finished

    # --- reporting from workers ---------------------------------------------------------

    def meter(self, phase: str) -> Meter:
        with self._lock:
            return self.meters.setdefault(phase, Meter())

    def status(self, phase: str, text: str) -> None:
        """A progress line for ``phase``; held back while a question is open."""
        with self._lock:
            m = self.meters.get(phase)
            extra = m.summary(self.clock()) if m else ""
            line = f"[{phase}] {text}" + (f" ({extra})" if extra else "")
            self._latest[phase] = line
            if self._prompt is not None:
                self._held[phase] = line
            else:
                self._emit(line)

    def working_on(self, phase: str, path: str, size: int = 0) -> None:
        """Cheap: remember the file a phase is on, for the heartbeat."""
        self._current[phase] = (path, size, self.clock())

    def expect(self, *phases: str) -> None:
        with self._lock:
            self._expected.update(phases)

    def finished(self, phase: str) -> None:
        with self._lock:
            self._finished.add(phase)
            self._current.pop(phase, None)

    def resumed(self, phase: str) -> None:
        with self._lock:
            self._finished.discard(phase)

    def say(self, text: str) -> None:
        """A message that is not progress (results, warnings); never held back."""
        with self._lock:
            self._emit(text)

    def _emit(self, text: str) -> None:
        self.out(text)
        self._last_out = self.clock()

    # --- questions ----------------------------------------------------------------------

    def ask(self, lines: list[str], prompt: str, read: Callable[[str], str] | None = None) -> str:
        """Show ``lines`` and read one answer; progress waits until it is given."""
        read = read or _read_line
        with self._lock:
            for ln in lines:
                self._emit(ln)
            now = self.clock()
            self._prompt = _Prompt(lines, prompt, now, now)
        try:
            return read(prompt)
        finally:
            with self._lock:
                self._prompt = None
                held, self._held = self._held, {}
                for phase in sorted(held):
                    self._emit(held[phase])

    def _only_answer_left(self) -> bool:
        return bool(self._expected) and self._expected <= self._finished

    # --- background ticker --------------------------------------------------------------

    def tick(self) -> None:
        """Heartbeat and reminders; called about once a second by the ticker thread."""
        now = self.clock()
        with self._lock:
            p = self._prompt
            if p is not None:
                if now - p.reminded >= self.remind_every:
                    p.reminded = now
                    waited = duration(now - p.opened)
                    if self._only_answer_left():
                        done = " and ".join(_PHASE_NAMES.get(x, x) for x in sorted(self._expected))
                        why = f"{done} finished; the run is waiting only for this answer"
                    else:
                        why = "work continues in the background"
                    self._emit(f"\n(Still waiting for your answer after {waited}: {why}.)")
                    for ln in p.lines:
                        self._emit(ln)
                    sys.stdout.write(f"{p.prompt}: ")
                    sys.stdout.flush()
                return
            if now - self._last_out < self.heartbeat:
                return
            parts = []
            for phase in sorted(set(self._latest) | set(self._current)):
                if phase in self._finished:
                    continue
                cur = self._current.get(phase)
                if cur:
                    path, size, since = cur
                    on = f"on {path}" + (f" ({human(size)})" if size else "")
                    if now - since >= self.heartbeat:
                        on += f" for {duration(now - since)}"
                    parts.append(f"{phase} {on}")
                elif phase in self._latest:
                    parts.append(self._latest[phase].split("] ", 1)[-1])
            if parts:
                self._emit("[working] " + "; ".join(parts))

    def start(self) -> None:
        if self._thread is not None:
            return

        def loop() -> None:
            while not self._stop.wait(1.0):
                self.tick()

        self._stop.clear()
        self._thread = threading.Thread(target=loop, name="console", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(2.0)
            self._thread = None


_PHASE_NAMES = {"hash": "hashing", "scan": "scanning", "copy": "copying"}


def _read_line(prompt: str) -> str:
    sys.stdout.write(f"{prompt}: ")
    sys.stdout.flush()
    line = sys.stdin.readline()
    if not line:
        raise EOFError
    return line


CONSOLE = Console()
