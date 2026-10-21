"""Per-tool-call time limits (the optional timeout_s every tool accepts).

The limit lives in a thread-local set by the tool wrapper in the worker thread that runs the tool. Long waits in
the radio / capture / watch / polling code call check() or sleep(), so a call that runs out of time stops at the next
such point with ToolTimeout; the wrapper also stops waiting a little after the limit whatever happens.
"""
from __future__ import annotations

import threading
import time

_TL = threading.local()


class ToolTimeout(TimeoutError):
    pass


def start(seconds: float | None) -> None:
    _TL.end = time.time() + float(seconds) if seconds else None
    _TL.limit = float(seconds) if seconds else None


def clear() -> None:
    _TL.end = _TL.limit = None


def remaining() -> float | None:
    end = getattr(_TL, "end", None)
    return None if end is None else end - time.time()


def check() -> None:
    end = getattr(_TL, "end", None)
    if end is not None and time.time() >= end:
        raise ToolTimeout(f"timed out after {getattr(_TL, 'limit', 0) or 0:g} s")


def sleep(seconds: float) -> None:
    """time.sleep that wakes up (and raises ToolTimeout) when the call's time limit runs out."""
    check()
    rem = remaining()
    if rem is not None and rem < seconds:
        time.sleep(max(0.0, rem))
        check()
    time.sleep(seconds)


def wait_slice(seconds: float) -> float:
    """How long a blocking wait may last before it should come back and check() (at most `seconds`)."""
    rem = remaining()
    return seconds if rem is None else max(0.0, min(seconds, rem))
