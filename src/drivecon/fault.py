"""Named crash points for tests.

Set ``CONSOLIDATE_FAULT=<checkpoint>:<n>:<kill|sigint|raise>`` to fire an action the
nth time the named checkpoint is reached. Without the variable this is a no-op.
"""

from __future__ import annotations

import os
import signal

_hits: dict[str, int] = {}


class FaultInjected(RuntimeError):
    """Raised by the ``raise`` fault action."""


def reset() -> None:
    _hits.clear()


def checkpoint(name: str) -> None:
    spec = os.environ.get("CONSOLIDATE_FAULT")
    if not spec:
        return
    cp, n, action = spec.split(":")
    if cp != name:
        return
    _hits[name] = _hits.get(name, 0) + 1
    if _hits[name] != int(n):
        return
    if action == "kill":
        os.kill(os.getpid(), signal.SIGKILL)
    elif action == "sigint":
        os.kill(os.getpid(), signal.SIGINT)
    elif action == "raise":
        raise FaultInjected(name)
    else:
        raise ValueError(f"unknown fault action {action!r}")
