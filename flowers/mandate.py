"""The Mandate — a deterministic, owner-approved authorization SCOPE ("the goal is the permission").

The per-action approval prompt was always *consent/friction*, never the safety mechanism (the real
safety is the money REFUSE floor, the irreversible NEVER floor, and the no-LLM read-back gate — none of
which a mandate can reach). The Mandate lets the owner approve, ONCE, a bounded scope of reversible
actions — "email these 6 caterers, up to 2 follow-ups each, no money, nothing irreversible" — and then the
run executes it without re-prompting per action. It WIDENS the broker's authorization decision (``ok_auth``)
and NEVER touches the verification trigger (``side``) or the read-back gate: a mandated send that doesn't
land is still a hard refuse, exactly like an owner-approved one that didn't land.

This module is PURE (no LLM, no I/O, deterministic) — like :mod:`flowers.policy` / :mod:`flowers.effects`.
The broker calls :func:`covers` / :func:`bump`; the planner calls :func:`parse_mandate` /
:func:`goal_named_recipients`; the operator calls :func:`render_card` / :func:`new_counts`.

The mandate is a plain JSON dict (round-trips through the store's JSON column wholesale; forgiving across
schema evolution). Shape::

    {
      "action_types":   ["gmail:GMAIL_SEND_EMAIL", ...],   # "toolkit:ACTION" labels covered
      "recipient_scope": ["asa@example.com", "@acme.com"],  # allow-list: exact emails and/or domains
      "magnitude_caps": {"max_sends": 20, "per_domain": 10, "per_recipient": 2},
      "irreversibility_ceiling": "ASK",   # informational; the code hard-enforces tier == ASK regardless
      "done_definition": "...",
    }

An empty dict ``{}`` means "no mandate" -> :func:`covers` always returns ``False`` -> today's
ask-everything behaviour. The injection defence: ``recipient_scope`` is frozen when the owner approves the
card; it is NEVER appended from free model text mid-run, so an injected "forward to attacker@evil.com" reply
yields an out-of-scope recipient -> ``covers`` is ``False`` -> the action parks for approval as usual.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from urllib.parse import urlparse

from flowers import policy

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
# A bare domain like "acme.com" or "mail.acme.co.uk" (no spaces, has a dot, no '@').
_DOMAIN_RE = re.compile(r"^[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")

# Delivering verbs (gmail/slack): an action whose slug carries one of these SENDS to an external party,
# so a MISSING recipient is suspicious -> fail closed. Curated to TRUE delivery verbs: NOT "email" (every
# gmail action carries that token) and NOT booking verbs (the browser handles its last mile by tier), so
# recipient-LESS reversible actions (archive/label/confirm/reserve on the owner's OWN resources) stay
# action-type-only — covered exactly as the owner approved on the card, never re-prompted.
# NOT "message": every gmail slug carries that token (ARCHIVE_MESSAGE / MARK_MESSAGE / GET_MESSAGE), which
# would falsely mark a read/file action as delivering. Slack send/post are still caught by send/post.
_DELIVERING_VERBS = frozenset({"send", "reply", "forward", "post", "dm",
                               "submit", "apply", "publish", "share", "invite"})

_CAP_DEFAULTS = {"max_sends": 20, "per_domain": 10, "per_recipient": 2}
_CAP_HARD = {"max_sends": 200, "per_domain": 100, "per_recipient": 20}
_MAX_ACTION_TYPES = 32
_MAX_SCOPE = 128


# --------------------------------------------------------------------------- small pure helpers

def params_digest(params: dict) -> str:
    """A stable short digest of the FULL action params — the dedupe key for a mandate-covered action, so
    a byte-identical repeat is NOT auto-resent (a genuine resend is the owner's call). Same algorithm as
    the broker's grant digest, kept here so the module has no import cycle on the broker."""
    try:
        blob = json.dumps(params or {}, sort_keys=True, default=str)
    except Exception:
        blob = repr(params)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _tokens(action: str) -> set[str]:
    return set((action or "").upper().replace("-", "_").lower().split("_"))


def _norm_label(label: str) -> str:
    """Normalize a "toolkit:ACTION" label to lower-toolkit / UPPER-action, so case can't dodge a scope
    match. A label with no ':' is lowercased whole."""
    s = str(label or "").strip()
    if ":" not in s:
        return s.lower()
    tk, _, act = s.partition(":")
    return f"{tk.strip().lower()}:{act.strip().upper()}"


def _domain(recipient: str) -> str:
    r = (recipient or "").strip().lower()
    if "@" in r:
        return r.split("@")[-1]
    return r   # already a bare host/domain token (browser/slack)


def _host(url: str) -> str:
    s = str(url or "").strip().lower()
    if not s:
        return ""
    if "://" not in s:
        s = "http://" + s
    try:
        net = urlparse(s).netloc
    except Exception:
        return ""
    return net.split("@")[-1].split(":")[0].strip()


def _emails_from(value) -> list[str]:
    """Every email address found in a recipient value (a list, a comma/semicolon string, or a
    'Name <email>' form). Returns ONLY regex-matched addresses — an unparseable recipient yields [] so
    a recipient-bearing action fails closed (-> ask), never auto-sends to an address we couldn't read."""
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        out: list[str] = []
        for v in value:
            out.extend(_emails_from(v))
        return out
    return [m.lower() for m in _EMAIL_RE.findall(str(value))]


def _dedup_lower(items) -> list[str]:
    out: list[str] = []
    for it in items:
        s = (str(it) if it is not None else "").strip().lower()
        if s and s not in out:
            out.append(s)
    return out


def _caps(mandate: dict) -> dict:
    raw = (mandate or {}).get("magnitude_caps") or {}
    out = {}
    for k, dflt in _CAP_DEFAULTS.items():
        try:
            v = int(raw.get(k, dflt))
        except (TypeError, ValueError):
            v = dflt
        out[k] = max(1, min(v, _CAP_HARD[k]))
    return out


def _sanitize_scope_entry(entry: str) -> str | None:
    """An allow-list entry is either an exact email or a domain ('@acme.com' or 'acme.com'). Anything
    else (free text, 'anyone', a partial) is dropped — the scope only ever holds concrete recipients."""
    s = str(entry or "").strip().lower()
    if not s:
        return None
    if "@" in s and not s.startswith("@"):
        m = _EMAIL_RE.search(s)
        return m.group(0) if m else None
    dom = s.lstrip("@")
    return dom if _DOMAIN_RE.match(dom) else None


def _on_allowlist(recipient: str, scope: list) -> bool:
    """Fail-closed allow-list match. An exact email/token matches its scope twin; a domain scope entry
    ('acme.com') matches an email at that domain (alice@acme.com) or a host equal-or-subdomain
    (www.acme.com) — using the equal-or-dot-suffix rule so 'acme.com' never matches 'notacme.com' or
    'acme.com.evil.com'."""
    r = (recipient or "").strip().lower()
    if not r:
        return False
    dom = _domain(r)
    for entry in scope or []:
        e = str(entry or "").strip().lower().lstrip("@")
        if not e:
            continue
        if r == e:
            return True                                   # exact email / token
        if dom and (dom == e or dom.endswith("." + e)):   # recipient's domain is e or a subdomain of e
            return True
        if "@" not in r and (r == e or r.endswith("." + e)):  # recipient IS a host (browser/slack)
            return True
    return False


# --------------------------------------------------------------------------- recipients

def is_recipient_bearing(toolkit: str, action: str) -> bool:
    """True iff a MISSING recipient should FAIL CLOSED (the action delivers to an external party). A
    side-effecting BROWSER action always lands on its target host (book/order/reserve/submit/confirm act
    on a page's site), so it is recipient-bearing by TIER — not by a verb set the booking last-mile would
    dodge. gmail/slack use the delivering-verb heuristic. Every OTHER toolkit is action-type-only: a
    parseable recipient is STILL scope-checked (in ``covers``), but a recipient-LESS reversible action the
    owner approved (calendar confirm, gmail archive/label, github comment) is covered without re-asking."""
    tk = (toolkit or "").strip().lower()
    if tk == "browser":
        return policy.classify("browser", action) != policy.AUTO
    if tk in ("gmail", "slack"):
        return bool(_tokens(action) & _DELIVERING_VERBS)
    return False


# Every param key that names an external party an action reaches — direct recipients AND fan-out
# (calendar attendees/guests, channel members). Harvested for EVERY toolkit so a calendar CREATE_EVENT
# with `attendees` or a slack member-add is scoped/fail-closed exactly like an email send — an attendee
# IS a recipient (the injection surface), so it must never be auto-covered unscoped.
_RECIPIENT_KEYS = ("to", "cc", "bcc", "recipient", "recipients", "attendees", "attendee_emails",
                   "guests", "participants", "invitees", "members", "users", "user", "invited")


def extract_recipients(toolkit: str, action: str, params: dict) -> list[str]:
    """The external recipients of an action, normalized for the allow-list. Harvests every recipient/
    fan-out key (to/cc/bcc/recipients AND attendees/guests/members/...) as emails for ANY toolkit; the
    browser target host is its 'recipient'; a slack channel is a token recipient. Fail-closed: a fan-out
    action we can't parse a recipient for yields [] (-> ``covers`` parks via has_recipient_intent)."""
    tk = (toolkit or "").strip().lower()
    p = params or {}
    if tk == "browser":
        host = _host(p.get("url") or p.get("target") or "")
        return [host] if host else []
    out: list[str] = []
    for key in _RECIPIENT_KEYS:
        out.extend(_emails_from(p.get(key)))
    if tk == "slack":
        ch = str(p.get("channel") or "").strip().lower()
        if ch:
            out.append(ch)
    return _dedup_lower(out)


def has_recipient_intent(toolkit: str, action: str, params: dict) -> bool:
    """True iff the action reaches ANY external party — a delivering verb, a parsed recipient, OR a
    populated fan-out key (attendees/members/...) even when its value isn't a parseable email (e.g. a
    slack user id). Used to FAIL CLOSED in ``covers`` and to bar an action from ``learned_covers``, so
    learned trust only ever auto-covers actions with ZERO external-party intent (a personal calendar
    event, a label, an archive)."""
    if is_recipient_bearing(toolkit, action) or extract_recipients(toolkit, action, params):
        return True
    p = params or {}
    return any(p.get(k) for k in _RECIPIENT_KEYS)


def lists_action(mandate: dict, toolkit: str, action: str) -> bool:
    """True iff the mandate's action_types explicitly governs this (toolkit, action) — so its magnitude
    caps bind it and learned trust must NOT rescue a cap-rejected call of that class."""
    cur = _norm_label(f"{toolkit}:{action}")
    return cur in {_norm_label(a) for a in (mandate or {}).get("action_types") or []}


def emails_in(text: str) -> list[str]:
    """Every distinct email address appearing in a blob of text (lowercased)."""
    return _dedup_lower(_EMAIL_RE.findall(str(text or "")))


# Common multi-label public suffixes (ccTLD second-levels). A pragmatic, NON-exhaustive list (no PSL
# dependency, to keep the trust core stdlib-pure) — enough that a registrable-domain comparison treats
# e.g. ``bistro.co.uk`` as the registrable unit and refuses a bare ``co.uk`` as an org. Extend as needed.
_MULTI_PUBLIC_SUFFIXES = frozenset({
    "co.uk", "org.uk", "me.uk", "ltd.uk", "plc.uk", "net.uk", "sch.uk", "ac.uk", "gov.uk", "nhs.uk",
    "com.au", "net.au", "org.au", "edu.au", "gov.au", "id.au",
    "co.jp", "or.jp", "ne.jp", "ac.jp", "go.jp", "co.nz", "net.nz", "org.nz", "govt.nz", "ac.nz",
    "co.za", "org.za", "com.br", "net.br", "gov.br", "com.cn", "net.cn", "org.cn", "gov.cn",
    "co.in", "net.in", "org.in", "gov.in", "co.kr", "or.kr", "com.mx", "com.sg", "com.hk", "com.tw",
    "co.il", "org.il", "com.tr", "gov.tr", "com.ua", "com.ar", "co.id", "or.id",
    # common shared-hosting platforms where each subdomain is a DIFFERENT tenant (so a sibling page must
    # not admit a sibling's address) — treated as suffixes so the registrable unit is the full subdomain.
    "github.io", "herokuapp.com", "web.app", "firebaseapp.com", "netlify.app", "vercel.app", "pages.dev",
    "blogspot.com", "wordpress.com", "s3.amazonaws.com", "azurewebsites.net", "cloudfront.net",
})


def _registrable(domain: str) -> str:
    """The registrable domain (eTLD+1) of a host/email-domain under a pragmatic public-suffix list. For
    ``www.bistro.com`` -> ``bistro.com``; ``mail.bistro.co.uk`` -> ``bistro.co.uk``; ``a.github.io`` ->
    ``a.github.io`` (shared host: the subdomain IS the registrant). Returns "" when the input IS a bare
    public suffix (``co.uk``, ``s3.amazonaws.com``, a single-label TLD) — no registrant, never admitted."""
    parts = [p for p in (domain or "").strip().lower().strip(".").split(".") if p]
    if len(parts) < 2:
        return ""                                   # a single label (bare TLD) is not registrable
    for n in (3, 2):                                 # longest matching multi-label public suffix wins
        if len(parts) >= n and ".".join(parts[-n:]) in _MULTI_PUBLIC_SUFFIXES:
            return ".".join(parts[-(n + 1):]) if len(parts) >= n + 1 else ""
    return ".".join(parts[-2:])                      # default: eTLD+1 under a single-label TLD


def host_admits(email: str, host: str) -> bool:
    """The PROVENANCE rule for admitting a discovered recipient: an email is admissible from a page iff it
    shares the page host's REGISTRABLE domain (eTLD+1). ``chef@bistro.com`` on ``bistro.com`` /
    ``www.bistro.com`` is admitted; ``chef@bistro.co.uk`` on ``www.bistro.co.uk`` is admitted; an injected
    ``attacker@evil.com`` on ``bistro.com``, or a public-suffix recipient ``noreply@co.uk`` on
    ``www.bistro.co.uk``, is NOT — which keeps a malicious page from widening scope to an unrelated org or
    a non-registrant suffix. (Residual, accepted: a page on an attacker-writable SUBDOMAIN of a target org
    can still admit a same-org address — bounded LOW: capped, reversible, non-money, still gate-verified.)"""
    reg_email = _registrable(_domain((email or "").strip().lower()))
    reg_host = _registrable((host or "").strip().lower())
    return bool(reg_email) and reg_email == reg_host


def admitted_from_fetch(events) -> set[str]:
    """The set of emails this step's FETCH events admit to scope under :func:`host_admits` — found on a
    page whose host their own domain matches. Provenance-tracked discovery, never free model text: an email
    that only appears in the model's own output (no fetch event) is never here."""
    out: set[str] = set()
    for e in (events or []):
        if e.get("kind") != "fetch" or not e.get("ok") or not e.get("url"):
            continue
        host = _host(e["url"])
        for em in (e.get("emails") or []):
            if host_admits(em, host):
                out.add(str(em).strip().lower())
    return out


def goal_named_recipients(goal) -> set[str]:
    """The email addresses the owner literally wrote in the goal (text + constraint values). These are
    ALWAYS unioned into ``recipient_scope`` so a recipient the owner named is in scope even if the model
    omitted it. Emails only — we never INFER a domain from prose (too permissive)."""
    parts = [str(getattr(goal, "text", "") or "")]
    for v in (getattr(goal, "constraints", {}) or {}).values():
        parts.append(str(v))
    return {m.lower() for m in _EMAIL_RE.findall(" ".join(parts))}


# --------------------------------------------------------------------------- the counter

def new_counts(existing: dict | None = None) -> dict:
    """A normalized magnitude counter (a fresh working copy that never aliases the caller's dict)."""
    e = existing or {}
    return {
        "sends_total": int(e.get("sends_total", 0) or 0),
        "by_domain": {str(k): int(v) for k, v in (e.get("by_domain") or {}).items()},
        "by_recipient": {str(k): int(v) for k, v in (e.get("by_recipient") or {}).items()},
        "sent_digests": list(e.get("sent_digests") or []),
    }


def bump(counts: dict, *, toolkit: str, action: str, params: dict) -> None:
    """Record a forwarded mandate-covered side-effect: increment the totals and remember its digest (for
    dedupe). Called by the broker ONLY on a real forward — never on a park or a failed send — so a failed
    send doesn't burn a cap and an identical resend is later refused."""
    counts.setdefault("sends_total", 0)
    counts.setdefault("by_domain", {})
    counts.setdefault("by_recipient", {})
    counts.setdefault("sent_digests", [])
    counts["sends_total"] = int(counts["sends_total"]) + 1
    for r in extract_recipients(toolkit, action, params):
        counts["by_recipient"][r] = int(counts["by_recipient"].get(r, 0)) + 1
        d = _domain(r)
        if d:
            counts["by_domain"][d] = int(counts["by_domain"].get(d, 0)) + 1
    dig = params_digest(params)
    if dig not in counts["sent_digests"]:
        counts["sent_digests"].append(dig)


# --------------------------------------------------------------------------- the predicate

def covers(mandate: dict, *, toolkit: str, action: str, params: dict, tier: str, counts: dict) -> bool:
    """The ONLY way a mandate auto-authorizes an action. Returns True (-> widen ``ok_auth``) iff EVERY
    condition holds; any miss -> False (-> the action falls through to the normal per-action approval).

      1. a non-empty mandate exists;
      2. tier == ASK (excludes AUTO/NEVER/REFUSE -> the irreversible floor is unreachable);
      3. the action is not money/refused (defence-in-depth beyond the broker's own floor);
      4. the action's label is in ``action_types``;
      5. (recipient-bearing only) every recipient is on ``recipient_scope`` (the injection guard);
      6. it is not a byte-identical repeat of an already-forwarded action (dedupe);
      7. it is under the magnitude caps (total / per-recipient / per-domain).
    """
    if not mandate:
        return False
    if tier != policy.ASK:
        return False
    if policy.is_refused(toolkit, action):
        return False
    cur = _norm_label(f"{toolkit}:{action}")
    if cur not in {_norm_label(a) for a in (mandate.get("action_types") or [])}:
        return False
    recips = extract_recipients(toolkit, action, params)
    scope = list(mandate.get("recipient_scope") or [])
    # Scope (I5): EVERY recipient we can extract must be on the frozen allow-list — for ANY toolkit, not
    # only verb-detected "delivering" ones (a browser BOOK/ORDER/RESERVE lands on its target HOST, which
    # extract_recipients surfaces). And a delivering action we CANNOT parse a recipient for fails CLOSED.
    if recips and not all(_on_allowlist(r, scope) for r in recips):
        return False
    if has_recipient_intent(toolkit, action, params) and not recips:
        return False
    counts = counts or {}
    if params_digest(params) in set(counts.get("sent_digests") or []):
        return False
    caps = _caps(mandate)
    if int(counts.get("sends_total", 0)) >= caps["max_sends"]:
        return False
    # Caps are PROJECTED over the recipients in THIS action, so a single multi-recipient send can't blow
    # per-domain/per-recipient (the anti-blast bound): reject if the post-send total would exceed any cap.
    by_dom = counts.get("by_domain") or {}
    by_rec = counts.get("by_recipient") or {}
    rec_in_send = Counter(recips)
    dom_in_send = Counter(d for d in (_domain(r) for r in recips) if d)
    if any(int(by_rec.get(r, 0)) + n > caps["per_recipient"] for r, n in rec_in_send.items()):
        return False
    if any(int(by_dom.get(d, 0)) + n > caps["per_domain"] for d, n in dom_in_send.items()):
        return False
    return True


# --------------------------------------------------------------------------- planner / operator surface

LEARNED_TRUST_THRESHOLD = 5   # clean owner approvals of a class before flowers stops asking for it


def learned_covers(trust_counts: dict, *, toolkit: str, action: str, params: dict | None = None,
                   tier: str, threshold: int = LEARNED_TRUST_THRESHOLD) -> bool:
    """Cross-run LEARNED trust: True iff the owner has cleanly approved this action CLASS at least
    ``threshold`` times, so flowers stops asking for it. Deliberately NARROW and safe:

      * ``tier == ASK`` and ``not is_refused`` — money/irreversible are never learnable (NEVER is the
        floor; REFUSE is categorical);
      * NO external-party intent (``has_recipient_intent`` — a delivering verb, a parsed recipient, OR a
        fan-out key like calendar ``attendees`` / a channel member) is EVER learned-covered, so learned
        trust can never widen the injection surface. It only auto-covers reversible actions on the owner's
        OWN resources with no external recipient (label / archive / a personal calendar event) —
        "stop asking me to organize my inbox," never "email anyone" and never "invite anyone."

    No-LLM, deterministic. The count is incremented by the operator only on a real owner ``yes``."""
    if tier != policy.ASK or policy.is_refused(toolkit, action):
        return False
    if has_recipient_intent(toolkit, action, params or {}):
        return False
    label = _norm_label(f"{toolkit}:{action}")
    return int((trust_counts or {}).get(label, 0)) >= max(1, int(threshold))


def trust_label(effect_label: str) -> str:
    """Normalize an approval's ``toolkit:ACTION`` effect_label to the learned-trust counter key."""
    return _norm_label(effect_label)


def parse_mandate(content: str, goal) -> dict:
    """Deterministically validate a planner's proposed mandate JSON into the sanitized dict the broker
    enforces. Fail-OPEN: anything missing/garbled/empty -> {} (no mandate -> ask-everything). Drops any
    money/irreversible action_type (a model can't pre-authorize a NEVER), clamps caps, and unions the
    goal-named recipients into the scope. ``content`` is the planner model's raw JSON string."""
    try:
        data = json.loads(content)
    except (TypeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    raw = data.get("mandate")
    if not isinstance(raw, dict):
        return {}

    types: list[str] = []
    for a in (raw.get("action_types") or []):
        lbl = _norm_label(str(a))
        if ":" not in lbl:
            continue
        tk, _, act = lbl.partition(":")
        if not tk or not act:
            continue
        if policy.is_refused(tk, act) or policy.classify(tk, act) == policy.NEVER:
            continue                                   # money/irreversible is never mandate-able
        if lbl not in types:
            types.append(lbl)
        if len(types) >= _MAX_ACTION_TYPES:
            break
    if not types:
        return {}                                      # nothing coverable -> no mandate

    scope: list[str] = []
    for r in (raw.get("recipient_scope") or []):
        s = _sanitize_scope_entry(str(r))
        if s and s not in scope:
            scope.append(s)
    for r in goal_named_recipients(goal):
        if r not in scope:
            scope.append(r)
    scope = scope[:_MAX_SCOPE]

    return {
        "action_types": types,
        "recipient_scope": scope,
        "magnitude_caps": _caps({"magnitude_caps": raw.get("magnitude_caps") or {}}),
        "irreversibility_ceiling": "ASK",
        "done_definition": str(raw.get("done_definition") or "").strip()[:500],
        "undo_seconds": undo_seconds(raw.get("undo_seconds")),
    }


def undo_seconds(value) -> int:
    """Clamp a requested undo-window (seconds) to [0, 3600]. 0 = off (sends forward immediately)."""
    try:
        return max(0, min(int(value), 3600))
    except (TypeError, ValueError):
        return 0


def render_card(mandate: dict) -> str:
    """The owner-facing approval card — the ONE consolidated 'grant this autonomy?' prompt that replaces
    the per-action prompts."""
    if not mandate:
        return "No special autonomy requested; I'll ask before each action."
    types = mandate.get("action_types") or []
    scope = mandate.get("recipient_scope") or []
    caps = mandate.get("magnitude_caps") or {}
    lines = [
        "Here's the autonomy I'd like for this run (approve once, then I won't ask per action):",
        "  • I can: " + (", ".join(types) if types else "(nothing)"),
        "  • Only to: " + (", ".join(scope) if scope else "(no recipients in scope)"),
        f"  • Up to: {caps.get('max_sends', '?')} sends total, "
        f"{caps.get('per_recipient', '?')} per recipient, {caps.get('per_domain', '?')} per domain",
        "  • I will NOT spend money or do anything irreversible (delete/pay/cancel) without asking you each time.",
    ]
    done = (mandate.get("done_definition") or "").strip()
    if done:
        lines.append(f"  • Done when: {done}")
    undo = int(mandate.get("undo_seconds") or 0)
    if undo > 0:
        lines.append(f"  • I'll pause {undo}s before each send so you can text STOP to cancel it.")
    lines.append("Reply YES to grant this, or NO to keep approving each action individually.")
    return "\n".join(lines)
