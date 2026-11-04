"""Timer precision for real-time audio.

Windows wakes sleeping programs on a 15.6 ms tick by default, which makes 20 ms audio frames
leave unevenly. While a conversation runs, wewln asks for a 1 ms tick (timeBeginPeriod), as
media players do; it is released afterwards. Other systems need nothing.
"""

from __future__ import annotations

import contextlib
import ctypes
import sys
from collections.abc import Iterator


@contextlib.contextmanager
def fine_timer() -> Iterator[None]:
    if sys.platform != "win32":
        yield
        return
    try:  # pragma: no cover - Windows only
        winmm = ctypes.WinDLL("winmm")
        winmm.timeBeginPeriod(1)
    except (OSError, AttributeError):  # pragma: no cover
        yield
        return
    try:  # pragma: no cover
        yield
    finally:  # pragma: no cover
        winmm.timeEndPeriod(1)
