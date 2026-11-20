"""Clocks. Every time-based decision of the controller and of the test executor reads one of these, so tests can
cover the 60-minute idle threshold with a controlled clock instead of waiting."""

from __future__ import annotations

import threading
import time
from typing import Protocol


class Clock(Protocol):
    def now(self) -> float: ...

    def sleep(self, seconds: float) -> None: ...


class SystemClock:
    """Wall clock of the controller host (epoch seconds)."""

    def now(self) -> float:
        return time.time()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


class ManualClock:
    """Controlled clock for tests: time moves only when `advance` (or `sleep`) is called. Thread-safe."""

    def __init__(self, start: float = 1_800_000_000.0) -> None:
        self._now = start
        self._lock = threading.Lock()

    def now(self) -> float:
        with self._lock:
            return self._now

    def advance(self, seconds: float) -> float:
        if seconds < 0:
            raise ValueError("time does not go backwards")
        with self._lock:
            self._now += seconds
            return self._now

    def sleep(self, seconds: float) -> None:
        self.advance(max(seconds, 0.0))
