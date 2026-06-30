"""Deterministic capability policy — the no-LLM tiering for every world-touching action.

Ported from an earlier prototype (renaming its ``confirm`` tier to ``ask`` to match flowers'
auto/ask/never vocabulary). Every tool call the broker receives is classified into one tier BEFORE
it is forwarded:

  * ``auto``  — read-only / safe -> forward immediately.
  * ``ask``   — side-effecting but reversible (send/create/post) -> owner authorizes once.
  * ``never`` — irreversible / high-stakes (delete/pay/transfer) -> always confirmed; the money floor.

Default-conservative: an unrecognized verb defaults to ``ask`` (fail toward asking); any
irreversible/spend verb forces ``never``. An owner override may RAISE strictness, but it can NEVER
loosen a never-tier or money action to ``auto`` — the money/irreversible floor is un-bypassable. No
model is ever in this decision; owner overrides are advisory WITHIN these bounds.
"""

from __future__ import annotations

import re

AUTO = "auto"
ASK = "ask"
NEVER = "never"
REFUSE = "refuse"   # a categorical NON-capability (money/payment): never approvable / overridable / executed

_TIERS = (AUTO, ASK, NEVER)   # valid tiers (REFUSE is a verdict, NOT a tier — not a usable override value)

_READ_VERBS = frozenset({
    "get", "list", "fetch", "read", "search", "find", "retrieve", "view", "describe",
    "count", "check", "lookup", "query", "status", "info", "show",
})
# Bulk data-egress verbs are NOT auto-safe: an export/download/sync can read out a whole
# mailbox/repo the model then exfiltrates. Side-effecting (ask + read-back), even though they "read."
_EGRESS_VERBS = frozenset({
    "export", "download", "sync", "dump", "backup", "scrape", "crawl", "bulk", "extract",
})
_NEVER_VERBS = frozenset({
    "delete", "remove", "destroy", "drop", "purge", "erase", "wipe",
    "pay", "charge", "purchase", "buy", "refund", "transfer", "wire", "payout", "checkout",
    "deactivate", "revoke", "terminate", "uninstall", "unsubscribe",
})
_ASK_VERBS = frozenset({
    "send", "create", "update", "post", "write", "add", "insert", "modify", "edit", "set",
    "move", "archive", "reply", "forward", "share", "invite", "assign", "close", "merge",
    "publish", "upload", "schedule", "trigger", "execute", "approve", "comment", "label",
    "mark", "star", "react", "draft", "enable", "disable", "rename",
})
# PRECISE money detection. Money is now a categorical hard-REFUSE (not the old ask-owner floor), so a
# broad substring stem would CATEGORICALLY BLOCK legitimate consumer actions — 'pay' inside payload/copay,
# 'wire' inside wireless/rewire, 'charge' inside recharge/discharge, plus read-only invoice/billing views.
# Detect money as WORD-BOUNDARY spend verbs (not naked substrings), payment TOOLKITS, and a few explicit
# financial PHRASES (so 'wire_transfer'/'charge_card' count but 'transfer_file'/'charge_battery' don't).
_MONEY_VERB_RE = re.compile(
    r"(?<![a-z])(?:pay(?:ment|ments|out|now)?|purchase|checkout|check[\s_-]?out|refund|"
    r"buy(?:[\s_-]?now)?|remit)(?![a-z])", re.IGNORECASE)
_MONEY_PHRASES = (
    frozenset({"wire", "transfer"}), frozenset({"bank", "transfer"}), frozenset({"transfer", "funds"}),
    frozenset({"transfer", "money"}), frozenset({"transfer", "balance"}), frozenset({"send", "money"}),
    frozenset({"charge", "card"}), frozenset({"charge", "payment"}),
)
_MONEY_TOOLKITS = frozenset({"stripe", "paypal", "plaid", "wise", "venmo", "square", "cashapp",
                             "braintree", "adyen", "dwolla", "coinbase"})

# Explicit (toolkit, ACTION) overrides where the verb heuristic would mis-classify.
_POLICY_TABLE: dict[tuple[str, str], str] = {
    ("gmail", "GMAIL_DELETE_MESSAGE"): NEVER,
    ("gmail", "GMAIL_DELETE_DRAFT"): ASK,
    # trash = move to Trash (REVERSIBLE, restorable ~30d) -> ASK, explicitly distinct from the permanent
    # GMAIL_DELETE_MESSAGE NEVER floor above. add-label = reversible organize -> ASK. Pinned (not left to
    # the verb heuristic) so 'trash' can never drift toward a delete/NEVER reading.
    ("gmail", "GMAIL_TRASH_MESSAGE"): ASK,
    ("gmail", "GMAIL_ADD_LABEL"): ASK,
    ("gmail", "GMAIL_SEND_EMAIL"): ASK,
}


def _norm_action(action: str) -> str:
    return (action or "").strip().upper()


def _normalize_overrides(overrides: dict | None) -> dict:
    """Normalize override keys so case/whitespace can't silently drop an override: an exact key
    'browser:submit' is matched as 'browser:SUBMIT', a toolkit key 'Gmail' as 'gmail'. (A dropped
    override would only ever fall back to the stricter natural tier, but a hardening override being
    silently ignored is a surprising, real operability bug — finding H.)"""
    norm: dict[str, str] = {}
    for k, v in (overrides or {}).items():
        ks = str(k)
        if ":" in ks:
            t, _, a = ks.partition(":")
            norm[f"{_norm_toolkit(t)}:{_norm_action(a)}"] = v
        else:
            norm[_norm_toolkit(ks)] = v
    return norm


def _norm_toolkit(toolkit: str) -> str:
    return (toolkit or "").strip().lower()


def _action_tokens(action: str) -> set[str]:
    return set(_norm_action(action).replace("-", "_").lower().split("_"))


def is_money_action(toolkit: str, action: str) -> bool:
    """True iff (toolkit, action) genuinely moves money — the categorical hard-REFUSE. PRECISE (not broad
    substrings): a payments TOOLKIT, a WORD-BOUNDARY spend verb (pay/payment/payout/paynow/purchase/
    checkout/refund/buy[ now]/remit — NOT 'payload'/'copay'/'wireless'/'recharge'), or an explicit
    financial PHRASE (wire_transfer / charge_card — NOT 'transfer_file'/'charge_battery'). Read-only
    'invoice'/'billing' views are NOT money (they move nothing). Pure, no import cycle."""
    if _norm_toolkit(toolkit) in _MONEY_TOOLKITS:
        return True
    if _MONEY_VERB_RE.search(re.sub(r"[_\-]+", " ", str(action or ""))):
        return True
    toks = _action_tokens(action)
    return any(phrase <= toks for phrase in _MONEY_PHRASES)


# --- illegal / disallowed content (E1): a SECOND categorical hard-REFUSE, ORed into is_refused alongside
# money. Detection is deterministic + conservative (PRECISION over recall, to avoid blocking legitimate
# research/topics): an unambiguous ACCESS marker (a .onion hidden-service target, CSAM), OR an access/
# procure/produce VERB paired with an illicit OBJECT/PHRASE. Token-set ops only — linear, NO catastrophic
# regex (a hang in this path is a single-thread cross-run DoS, the same reason the watch loop bars
# model-authored regex). Mention alone never triggers it ("research the dark web" is allowed; "access a
# .onion drug market" is not). Used by is_refused (action-scoped), the goal pre-screen (E2), and the
# browser target floor.
_DISALLOWED_MARKERS = (".onion", "child porn", "child sexual abuse", "csam")
_ILLICIT_VERBS = frozenset({
    "buy", "order", "purchase", "acquire", "procure", "source", "obtain", "sell", "resell", "traffic",
    "smuggle", "access", "browse", "enter", "reach", "navigate", "visit", "connect", "login", "build",
    "make", "synthesize", "manufacture", "produce", "assemble", "hire", "launder",
})
_ILLICIT_OBJECTS = frozenset({
    "cocaine", "heroin", "fentanyl", "meth", "methamphetamine", "mdma", "ecstasy", "lsd", "ketamine",
    "narcotics", "darknet", "darkweb", "bomb", "explosive", "explosives", "grenade", "dynamite",
    "hitman", "hitmen",
})
_ILLICIT_PHRASES = (
    frozenset({"dark", "web"}), frozenset({"silk", "road"}), frozenset({"ghost", "gun"}),
    frozenset({"untraceable", "firearm"}), frozenset({"fake", "passport"}),
    frozenset({"counterfeit", "money"}), frozenset({"stolen", "cards"}), frozenset({"stolen", "identities"}),
    frozenset({"hire", "hitman"}), frozenset({"launder", "money"}),
)


def _alnum_tokens(text: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", (text or "").lower()) if t}


def is_disallowed_text(text: str) -> bool:
    """True iff free text reveals disallowed/illegal INTENT (dark-web/.onion access, controlled-substance
    or weapon/explosive procurement or manufacture, illicit-marketplace facilitation, CSAM). Deterministic
    + conservative; no model, no backtracking regex."""
    low = (text or "").lower()
    if any(m in low for m in _DISALLOWED_MARKERS):
        return True
    toks = _alnum_tokens(low)
    if toks & _ILLICIT_VERBS and toks & _ILLICIT_OBJECTS:
        return True
    if toks & _ILLICIT_VERBS and any(p <= toks for p in _ILLICIT_PHRASES):
        return True
    return False


def is_disallowed_content(toolkit: str, action: str) -> bool:
    """Action-scoped disallowed-content check — disallowed INTENT visible in the (toolkit, action) slug
    (e.g. an action literally named to procure contraband). ORed into is_refused. The richer signal
    usually lives in the goal text (E2 pre-screen) or a browser TARGET (the browser floor), not the slug."""
    return is_disallowed_text(f"{toolkit} {action}")


def is_refused(toolkit: str, action: str) -> bool:
    """True iff the action is a CATEGORICALLY-REFUSED non-capability — flowers cannot do it at all,
    period. Two categories: the money/payment surface (``is_money_action``) AND illegal/disallowed content
    (``is_disallowed_content``). Distinct from a tier: a refused action is never approvable, never
    overridable, and never executed (the broker hard-stops it before any read-back/execute). This is what
    makes 'flowers physically cannot spend your money / do something illegal' a capability FACT, not an
    approval prompt."""
    return is_money_action(toolkit, action) or is_disallowed_content(toolkit, action)


def _verb_tier(action: str) -> str:
    """Tier from the action slug's verb tokens. money/never > egress/ask > auto; default ask."""
    if is_money_action("", action):
        return NEVER
    tokens = _action_tokens(action)
    if tokens & _NEVER_VERBS:
        return NEVER
    if tokens & _EGRESS_VERBS:
        return ASK
    if tokens & _ASK_VERBS:
        return ASK
    if tokens & _READ_VERBS:
        return AUTO
    return ASK  # unrecognized -> conservative


# Browser driving verbs are NOT the same as integration verbs: navigating/clicking/typing/extracting
# is read-only page driving (AUTO), and the LAST-MILE mutation (submit/book/reserve) is the side effect.
# A money verb (pay/purchase/checkout) still floors to NEVER via _NEVER_VERBS below.
_BROWSER_READ_ACTIONS = frozenset({
    "NAVIGATE", "GOTO", "GO", "OPEN", "VISIT", "CLICK", "TYPE", "FILL", "SELECT", "CHECK", "SCROLL",
    "HOVER", "WAIT", "READ", "EXTRACT", "FIND", "SEARCH", "SCREENSHOT", "GET", "VIEW", "BACK",
    # observe-then-act preview: listing a page's actionable elements is a READ (no mutation) -> AUTO.
    "INSPECT", "OBSERVE", "ELEMENTS", "CANDIDATES",
})


def _browser_tier(action: str) -> str:
    """Browser-action tiering: money/irreversible -> NEVER; read-only page driving -> AUTO; everything
    else (submit/book/reserve/post/confirm/order/apply...) -> ASK (the side-effecting last mile)."""
    if is_money_action("browser", action) or (_action_tokens(action) & _NEVER_VERBS):
        return NEVER
    return AUTO if _norm_action(action) in _BROWSER_READ_ACTIONS else ASK


def classify(toolkit: str, action: str, *, overrides: dict | None = None) -> str:
    """Classify a (toolkit, action) into a tier. Resolution order:

      1. owner override for the exact (toolkit, action);
      2. owner override for the whole toolkit;
      3. built-in policy table for the exact (toolkit, action);
      4. the deterministic verb heuristic (default-conservative).

    A money/payment action returns ``REFUSE`` — a categorical non-capability that NO override can
    reach (checked first, before any tier/override logic). Otherwise an override to a LOOSER tier is
    honored, EXCEPT the irreversible floor: a never-natural action stays NEVER.
    """
    tk = _norm_toolkit(toolkit)
    act = _norm_action(action)
    # Money/payment is a categorical NON-capability: REFUSE before any tier/override logic, so it can
    # never be approved, overridden, or executed. flowers does not spend money.
    if is_refused(tk, act):
        return REFUSE
    ov = _normalize_overrides(overrides)
    if tk == "browser":
        natural = _POLICY_TABLE.get((tk, act)) or _browser_tier(act)
    else:
        natural = _POLICY_TABLE.get((tk, act)) or _verb_tier(act)
    override = None
    exact_key = f"{tk}:{act}"
    if exact_key in ov and ov[exact_key] in _TIERS:
        override = ov[exact_key]
    elif tk in ov and ov[tk] in _TIERS:
        override = ov[tk]
    if override is None:
        return natural
    # The irreversible FLOOR is un-bypassable by ANY override: a never-natural action stays NEVER.
    # (Money is already handled above — it returned REFUSE before any override was consulted.) An
    # override may still RAISE strictness, or lower a non-floored tier (e.g. ASK -> AUTO for a benign action).
    if natural == NEVER:
        return NEVER
    return override


def is_side_effecting(toolkit: str, action: str, *, overrides: dict | None = None) -> bool:
    """True iff the action is NOT auto-tier (it mutates external state — the trigger for before/after
    read-back snapshots and effect verification)."""
    return classify(toolkit, action, overrides=overrides) != AUTO
