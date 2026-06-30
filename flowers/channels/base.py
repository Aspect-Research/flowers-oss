"""The channel contract + the channel-agnostic answer parser.

An outbound *event* is a plain dict the operator emits: ``{"run_id","tenant_id","kind","text", ...}``
where ``kind`` is one of EVENT_KINDS. A channel renders it however it likes (SSE line, SMS, etc.).
Inbound, a channel hands the control plane a goal (and later, answers) via the intake/answer entry
points; there is no inbound wrapper type.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

EVENT_KINDS = frozenset(
    {"plan_announce", "progress", "approval", "clarify", "notify", "done", "escalated", "connect"}
)


class Channel(ABC):
    """A transport. The operator calls ``emit`` with an outbound event dict."""

    @abstractmethod
    def emit(self, event: dict) -> None: ...


# --------------------------------------------------------------------------- answer parsing

_YES = {"yes", "y", "yeah", "yep", "yup", "ok", "okay", "sure", "approve", "approved",
        "confirm", "confirmed", "go", "do it", "please do", "send it"}
_NO = {"no", "n", "nope", "nah", "deny", "denied", "cancel", "stop", "dont", "don't",
       "do not", "decline", "declined", "abort"}


def parse_answer(text: str) -> dict:
    """Map a free-text owner reply to ``{"decision": "yes"|"no"|"other", "text": text}``. Fail-safe:
    anything not clearly affirmative/negative is ``other`` (treated by the operator as not-yes)."""
    t = (text or "").strip().lower().rstrip(".!")
    if t in _YES or any(t.startswith(w + " ") for w in _YES):
        return {"decision": "yes", "text": text}
    if t in _NO or any(t.startswith(w + " ") for w in _NO):
        return {"decision": "no", "text": text}
    return {"decision": "other", "text": text}
