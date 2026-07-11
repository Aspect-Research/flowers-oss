"""The channel contract + the channel-agnostic answer parser.

An outbound *event* is a plain dict the operator emits: ``{"run_id","kind","text", ...}``
where ``kind`` is one of EVENT_KINDS. A channel renders it however it likes (SSE line, SMS, etc.).
Inbound, a channel hands the control plane a goal (and later, answers) via the intake/answer entry
points; there is no inbound wrapper type.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod

EVENT_KINDS = frozenset(
    {"plan_announce", "progress", "approval", "clarify", "notify", "done", "escalated", "connect"}
)


class Channel(ABC):
    """A transport. The operator calls ``emit`` with an outbound event dict."""

    @abstractmethod
    def emit(self, event: dict) -> None: ...


# --------------------------------------------------------------------------- answer parsing

# Affirmatives — matched as the WHOLE reply OR a "<phrase> ..." prefix (so "yes please" / "do it now" are
# still yes; a "yes, but change X" keeps its comma so it falls to "other" and revises). Beyond the terse
# tokens we include the natural approval phrases a person texts back to a draft preview ("sounds good",
# "looks great", "ship it", "lgtm", "perfect") — otherwise a plain approval would needlessly route into a
# second-touch revise (+ a model call) instead of just sending.
_YES = {"yes", "y", "yeah", "yep", "yup", "ok", "okay", "sure", "approve", "approved",
        "confirm", "confirmed", "go", "do it", "please do", "send it",
        "sounds good", "looks good", "looks great", "perfect", "great",
        "ship it", "lgtm", "go for it", "all good"}

# Decline words that prefix-match ("stop everything", "cancel it", "decline the request") — unambiguous
# whatever follows. The "no"-FAMILY is handled separately in parse_answer: a "no ..."-prefixed reply is
# usually EDIT GUIDANCE ("no, mention the deadline instead"), not a stop, so it must NOT prefix-match here.
_NO = {"deny", "denied", "cancel", "stop", "dont", "don't", "do not", "decline", "declined", "abort"}
# Bare "no"-family declines — a plain stop only when the reply IS one of these (or "no thanks").
_NO_FAMILY = {"no", "n", "nope", "nah"}
_NO_THANKS = {"no thanks", "no thank you", "no thank u", "nope thanks", "nah thanks"}

_THUMBS_UP = "\U0001F44D"   # 👍 — skin-tone + variation-selector modifiers are stripped before matching
_EMOJI_MODIFIERS = {ord(c): None for c in "\U0001F3FB\U0001F3FC\U0001F3FD\U0001F3FE\U0001F3FF\uFE0F"}
# The EXACT no-change idioms that read as approval of the draft as-is. A "no ..."-prefixed reply may flip
# to yes ONLY when, after stripping an optional trailing send-imperative, it IS one of these — nothing
# more. A generic no+send tail is NOT enough: "no, make it shorter and send it" requests an EDIT, and
# flipping it would send the OLD draft — unapproved words must never go out ("edits never send the old
# one"; every other no-prefixed reply falls to "other" -> the preview revises).
_NO_CHANGE_IDIOMS = {
    "no change", "no changes", "no changes needed", "no need to change it",
    "no need to change anything", "no edits", "no tweaks", "nothing to change",
    "no, as is", "no as is",
}
_SEND_TAIL_RE = re.compile(r"[\s,;—–-]*\bsend( it)?$")


def _no_change_approval(t: str) -> bool:
    """True iff the reply is EXACTLY a no-change idiom, optionally followed by a send-imperative tail
    ("no need to change it, send" / "no changes — send it" / bare "no changes needed"). Exact-match by
    design: any extra words mean the owner asked for something, so the preview must revise, not send."""
    stripped = _SEND_TAIL_RE.sub("", t).strip().strip(",;—–-").strip()
    return stripped in _NO_CHANGE_IDIOMS


def parse_answer(text: str) -> dict:
    """Map a free-text owner reply to ``{"decision": "yes"|"no"|"other", "text": text}``. Fail-safe:
    anything not clearly affirmative/negative is ``other`` (treated by the operator as not-yes — on a draft
    preview that means "revise"). The ONE shared parser, so the owner's approval vocabulary is identical on
    every surface (web + SMS; per-action approval + autonomy card + draft preview)."""
    t = (text or "").strip().lower().rstrip(".!")
    # A thumbs-up (any skin tone / with-or-without variation selector, one or more) is a plain yes.
    tn = t.translate(_EMOJI_MODIFIERS)
    if tn and set(tn) == {_THUMBS_UP}:
        return {"decision": "yes", "text": text}
    if t in _YES or any(t.startswith(w + " ") for w in _YES):
        return {"decision": "yes", "text": text}
    # An exact no-change idiom (± a send tail) is APPROVAL, not a decline ("no need to change it, send" /
    # "no changes needed"). Any OTHER "no ..."-prefixed reply ("no, make it shorter and send it") falls
    # through to "other" -> the preview REVISES, so the old draft never rides a mixed edit+send reply out.
    if _no_change_approval(t):
        return {"decision": "yes", "text": text}
    # A decline: a bare "no"-family reply ("no"/"nope"/"no thanks"), OR an unambiguous decline word
    # (stop/cancel/decline/...) as the whole reply or its prefix. A "no, do X instead" is neither -> "other".
    if t in _NO_FAMILY or t in _NO_THANKS or t in _NO or any(t.startswith(w + " ") for w in _NO):
        return {"decision": "no", "text": text}
    return {"decision": "other", "text": text}
