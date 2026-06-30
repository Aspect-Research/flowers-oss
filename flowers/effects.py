"""Read-back diffing + expected-effect fingerprint matching — the verification math (pure, no LLM).

Ported faithfully from an earlier prototype's read-back matching logic. Given an INDEPENDENT read-back
of a surface BEFORE and AFTER a side-effecting action, these functions decide whether the action's
*specific* expected effect landed — defeating the concurrent-writer false positive (a teammate's
email arriving must not "verify" a send that actually failed). The broker calls these to populate an
``EffectRecord``'s ``drift_present`` / ``expected_present``; the gate adjudicates from there.
"""

from __future__ import annotations

import re
from typing import Any


def snapshot_diff(before: dict, after: dict) -> dict:
    """The structured diff between two read-back snapshots: ``{added, changed, removed}`` (sorted)."""
    bk, ak = set(before or {}), set(after or {})
    added = sorted(ak - bk)
    removed = sorted(bk - ak)
    changed = sorted(k for k in (bk & ak) if (before or {}).get(k) != (after or {}).get(k))
    return {"added": added, "changed": changed, "removed": removed}


def has_effect(diff: dict) -> bool:
    """True iff the diff shows the PRESENCE of an effect (something added/changed/removed) — the
    non-exclusive check. Never asserts 'nothing else changed', so concurrent writers aren't failure."""
    return bool(diff.get("added") or diff.get("changed") or diff.get("removed"))


_ADDRESS_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


def _norm(v: Any) -> str:
    return str(v).strip().lower()


def _word_tokens(v: Any) -> set:
    return set(re.findall(r"[a-z0-9]+", str(v).lower()))


def _item_matches(item: dict, expected: list[str]) -> bool:
    """Does an added read-back item carry the SPECIFIC expected effect? EVERY expected value must
    match, but not as a loose substring:

      * an ADDRESS-bearing token (an email / anything with '@') must EQUAL a whole field VALUE — a
        send to bob@acme.com is NOT verified by a message that merely mentions bob@acme.com in its body;
      * a free-text value (subject/title) must have ALL its words present as whole tokens within a
        SINGLE field (so 'Fix' no longer matches 'Prefix', and the value is tied to one field).

    (Opaque RECORD IDS — email_id/message_id/... — are handled separately in ``has_expected_effect`` by
    exact id equality, NOT here, so a message id can never match a loose body/sender token.)

    Fail-CLOSED: a legit effect whose read-back embeds the recipient rather than exposing it as a field
    value returns False -> the gate asks the owner rather than auto-accepting (the safe direction)."""
    item = item or {}
    field_norms = [_norm(v) for v in item.values()]
    field_tokensets = [_word_tokens(v) for v in item.values()]
    for exp in expected:
        e = _norm(exp)
        if not e:
            continue
        if "@" in e or _ADDRESS_RE.fullmatch(e):
            if e in field_norms:
                continue
            return False
        ewords = _word_tokens(exp)
        if not ewords:
            continue
        if any(ewords <= toks for toks in field_tokensets):
            continue
        return False
    return True


def fingerprint_values(fingerprint: dict | None) -> list[str] | None:
    """The identifying VALUES of an expected-effect fingerprint dict (e.g. ``{"to": x, "subject": y}``
    -> ``[x, y]``), or None when none can be formed (verification then falls back to presence)."""
    if not fingerprint:
        return None
    vals = [str(v).strip() for v in fingerprint.values() if v not in (None, "")]
    return [v for v in vals if v] or None


# Fingerprint fields that name an OPAQUE RECORD ID (a Gmail message id, etc.) rather than free text.
# These must verify by EXACT id equality against the read-back item's own id (its ``id`` field or its
# read-back KEY), never via the loose word-token matcher — otherwise a concurrent/injected item whose
# sender local-part or body merely CONTAINS the id would false-verify an effect that did not land.
_ID_FINGERPRINT_FIELDS = frozenset({"email_id", "message_id", "id", "thread_id", "draft_id"})


def has_expected_effect(before_items: dict, after_items: dict,
                        fingerprint: dict | None) -> bool | None:
    """Did the SPECIFIC expected effect land? Returns True (an ADDED item matches the fingerprint),
    False (items may have been added but NONE match — the expected effect did not land, even under
    concurrent unrelated drift), or None (no fingerprint -> the caller falls back to ``has_effect``).

    An id-keyed fingerprint (trash/label, whose only identity is the message id) is matched by EXACT id
    equality against the added item's own id; any non-id fields still go through the whole-field/token
    matcher. A pure free-text fingerprint (send/event/browser) is unchanged."""
    expected = fingerprint_values(fingerprint)
    if not expected:
        return None
    fp = fingerprint or {}
    id_vals = [_norm(v) for k, v in fp.items() if k in _ID_FINGERPRINT_FIELDS and str(v).strip()]
    text_vals = [str(v) for k, v in fp.items() if k not in _ID_FINGERPRINT_FIELDS and str(v).strip()]
    added = set(after_items or {}) - set(before_items or {})
    for key in added:
        item = (after_items or {}).get(key) or {}
        if id_vals:
            # the item's own identity: its `id` field if present, else its read-back KEY. ALL fingerprint
            # id values must EQUAL it (whole value) — a body/sender token can never satisfy this.
            item_id = _norm(item.get("id") or key)
            if not all(v == item_id for v in id_vals):
                continue
        if _item_matches(item, text_vals):
            return True
    return False
