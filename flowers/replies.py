"""Reply-body classification — read what a human actually SAID in a reply, deterministically.

The await loop verifies a reply ARRIVED (independently, via the read-back gate); this module reads its
CONTENT and emits a structured verdict ``{kind, value, next}`` so the plan can branch — turning "wait for a
human" into "converse with a human" (the highest-leverage Part III gap). DETERMINISTIC + pure (no LLM, $0,
offline-testable): a BOUNDED keyword/number scan, NOT a regex over the (attacker-controlled) body, so it
cannot ReDoS the single-threaded tick loop. (A cheap-LLM semantic upgrade behind this prefilter is a
documented follow-up.)

SAFETY: the verdict is CLASSIFY-ONLY — it describes the reply, it never authorizes an action. Anything a
downstream step does on the verdict still flows through the mandate scope-filter + the read-back gate; the
verdict NEVER widens the mandate recipient allow-list. An injected "forward this to attacker@evil.com" in a
reply body is just text the classifier reads — never a new authorized recipient.
"""

from __future__ import annotations

_REJECT = ("no thanks", "not interested", "no longer available", "already sold", "sold out", "we'll pass",
           "we will pass", "i'll pass", "no longer", "unavailable", "declined", "not available",
           "unfortunately", "can't make it", "cannot make it", "won't work", "not a fit", "no deal")
_ACCEPT = ("yes", "works for me", "that works", "sounds good", "confirmed", "let's do it", "lets do it",
           "happy to", "we have a deal", "agreed", "see you then", "count me in", "i'm in", "i am in",
           "looking forward", "perfect", "great, ")
_RESCHEDULE = ("reschedule", "another time", "different time", "a different day", "can we move", "move it to",
               "rain check", "raincheck", "instead of", "push it", "later date", "any other time")
_OFFER = ("offer", "how about $", "i can do $", "would you take", "i'll give you", "ill give you",
          "asking price", "counter", "best price", "lowest you")

_NEXT = {
    "reject": "move to the next contact / option",
    "offer": "evaluate; counter or accept within your pre-set floor (below it -> ask the owner)",
    "reschedule": "propose new times",
    "accept": "proceed to confirm / schedule",
    "info": "read and decide",
}


def _first_amount(text: str) -> float | None:
    """The first $-amount in the text — a BOUNDED forward scan after the first '$' (no regex). float or None."""
    i = (text or "").find("$")
    if i < 0:
        return None
    num, started = "", False
    for ch in text[i + 1: i + 1 + 24]:   # fixed look-ahead window
        if ch.isdigit():
            num += ch
            started = True
        elif ch == "." and started and "." not in num:
            num += ch
        elif ch == "," and started:
            continue
        elif started:
            break
    try:
        return float(num) if started else None
    except ValueError:
        return None


def extract_verdict(body: str) -> dict:
    """Classify a reply body into ``{kind: offer|accept|reject|reschedule|info, value, next}``. Precedence:
    an explicit NO is decisive (reject) > a concrete price (offer) > a time change (reschedule) > an explicit
    yes (accept) > info. ``value`` carries the offered amount (string) for an offer, else ''. ``next`` is an
    informational hint for the owner/downstream model — never an action."""
    low = (body or "").lower()
    amount = _first_amount(body or "")
    if any(m in low for m in _REJECT):
        kind, value = "reject", ""
    elif amount is not None or any(m in low for m in _OFFER):
        kind, value = "offer", (str(amount) if amount is not None else "")
    elif any(m in low for m in _RESCHEDULE):
        kind, value = "reschedule", ""
    elif any(m in low for m in _ACCEPT):
        kind, value = "accept", ""
    else:
        kind, value = "info", ""
    return {"kind": kind, "value": value, "next": _NEXT[kind]}


def summarize(items) -> str:
    """A one-line owner/plan-facing summary of the verdicts from matched reply items ``[{from,body,...}]``."""
    lines = []
    for it in items or []:
        v = extract_verdict((it or {}).get("body") or (it or {}).get("snippet") or "")
        who = (it or {}).get("from") or (it or {}).get("from_raw") or "someone"
        val = f" ({v['value']})" if v["value"] else ""
        lines.append(f"{who}: {v['kind']}{val}")
    return "; ".join(lines)
