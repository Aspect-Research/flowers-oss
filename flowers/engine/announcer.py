"""The announcer — tell the owner the plan before executing (announce + proceed).

Per the locked decision (Q2): we announce the plan and proceed with read-only discovery immediately;
the first side-effecting action still parks for approval (the trust gate enforces that). This module
just renders the plan into a human-facing announcement; the operator pushes it to the channel.
"""

from __future__ import annotations

from flowers.types import Plan, StepKind


def announce_plan(plan: Plan, *, mandate: dict | None = None) -> str:
    lines = ["Here's my plan:"]
    for s in plan.steps:
        bullet = f"  {s.index + 1}. {s.text}"
        notes = []
        if s.depends_on:
            notes.append("after " + ", ".join(str(d + 1) for d in s.depends_on))
        if s.kind is StepKind.AWAIT_REPLIES:
            w = s.params.get("window_seconds")
            k = s.params.get("min_replies")
            notes.append(f"wait for {k or 1} reply(ies)" + (f" up to {w}s" if w else ""))
        elif s.kind is StepKind.MONITOR:
            notes.append("monitor/notify")
        if notes:
            bullet += "  (" + "; ".join(notes) + ")"
        lines.append(bullet)
    if mandate:
        types = ", ".join(mandate.get("action_types") or []) or "(none)"
        scope = ", ".join(mandate.get("recipient_scope") or []) or "(none in scope)"
        lines.append(f"Autonomy I'll ask you to grant once: {types} → only to {scope} "
                     "(no money, nothing irreversible without asking).")
    return "\n".join(lines)
