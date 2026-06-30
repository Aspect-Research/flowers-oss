"""Per-user persistent memory — a small, self-curated markdown note the operator carries ACROSS runs.

flowers is long-lived: it should get to know YOU. This is the cross-session memory that makes that real —
ONE markdown note for the local user. Before a run, the note is injected into
the planner + executor prompts ("WHAT YOU KNOW ABOUT THIS USER"), so plans and actions are informed by
prior context — standing preferences, important facts, corrections/redirections the user gave, who-is-who.
The agent updates it WHEN APPROPRIATE via the `remember` tool: the executor emits a ``remember`` event,
the operator appends it here (this module) and persists it through the Store.

It is deliberately SMALL and SELF-CURATING: notes are deduped (case-insensitive) and, past a soft
character cap, the OLDEST notes are dropped — so the memory stays a tight, current picture of the user
rather than an ever-growing log. No model call lives here; this is plain, deterministic, testable text
code (the same discipline as the trust gate / planner parser).
"""

from __future__ import annotations

MEMORY_CHAR_CAP = 6000          # soft cap on the whole note; oldest bullets are dropped once we exceed it
MAX_NOTE_LEN = 600              # one remembered note can't blow up the file
_HEADER = "# What flowers knows about this user"
_BULLET = "- "


def _clean(note: str) -> str:
    """Collapse a note to a single trimmed line (a bullet is one line) and bound its length."""
    one_line = " ".join(str(note or "").split())
    return one_line[:MAX_NOTE_LEN].strip()


def existing_notes(md: str) -> list[str]:
    """The bullet notes already in the memory, in order (oldest first)."""
    out: list[str] = []
    for line in (md or "").splitlines():
        s = line.strip()
        if s.startswith(_BULLET):
            note = s[len(_BULLET):].strip()
            if note:
                out.append(note)
    return out


def render(notes: list[str]) -> str:
    """Render the canonical markdown note from a list of bullets (empty string when there are none)."""
    if not notes:
        return ""
    return _HEADER + "\n" + "\n".join(_BULLET + n for n in notes) + "\n"


def append_note(md: str, note: str) -> str:
    """Return the memory with ``note`` appended — deduped (case-insensitive) and capped (drop oldest)."""
    clean = _clean(note)
    notes = existing_notes(md)
    if not clean:
        return render(notes)
    if any(clean.lower() == n.lower() for n in notes):
        return render(notes)                       # already known — no churn
    notes.append(clean)
    # Enforce the soft cap by dropping the OLDEST notes until we fit (keep at least the newest one).
    while len(render(notes)) > MEMORY_CHAR_CAP and len(notes) > 1:
        notes.pop(0)
    return render(notes)


def append_notes(md: str, notes) -> str:
    """Append several notes in order, returning the updated memory."""
    out = md or ""
    for n in notes or []:
        out = append_note(out, n)
    return out


def format_for_prompt(md: str) -> str:
    """The block injected into planner/executor prompts — empty string when there's nothing to say."""
    notes = existing_notes(md)
    if not notes:
        return ""
    body = "\n".join(_BULLET + n for n in notes)
    return ("\n\nWHAT YOU KNOW ABOUT THIS USER (remembered from past sessions — use it; do not re-ask for "
            "what you already know here):\n" + body)
