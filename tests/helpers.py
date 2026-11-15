"""Shared test helpers: fake clock, fake sleep, quick orchestrator factory."""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXAMPLES = os.path.join(ROOT, "examples", "research_pipeline")
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from oas import Orchestrator, Store  # noqa: E402


class FakeClock:
    def __init__(self, t: float = 1_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class FakeSleep:
    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def manifest(id: str = "svc", **kw):
    d = {"id": id, "version": "1.0.0", "purpose": "test service"}
    d.update(kw)
    return d


def make_orch(path: str = ":memory:"):
    clock = FakeClock()
    sleep = FakeSleep()
    store = Store(path, clock=clock)
    orch = Orchestrator(store, sleep=sleep)
    return orch, store, clock, sleep
