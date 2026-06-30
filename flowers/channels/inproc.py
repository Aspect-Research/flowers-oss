"""InProcChannel — an in-memory channel for tests and embedding. Collects emitted events."""

from __future__ import annotations

from collections.abc import Callable

from flowers.channels.base import Channel


class InProcChannel(Channel):
    def __init__(self, on_event: Callable[[dict], None] | None = None):
        self.events: list[dict] = []
        self._on_event = on_event

    def emit(self, event: dict) -> None:
        self.events.append(event)
        if self._on_event:
            self._on_event(event)

    def of_kind(self, kind: str) -> list[dict]:
        return [e for e in self.events if e.get("kind") == kind]

    def for_run(self, run_id: str) -> list[dict]:
        return [e for e in self.events if e.get("run_id") == run_id]
