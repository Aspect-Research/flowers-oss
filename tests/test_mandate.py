"""The Mandate pure module — parsing, the coverage predicate, the allow-list, and the counter.

These pin down the deterministic core: a mandate only ever auto-authorizes a reversible (ASK-tier),
non-money, in-scope, in-cap, non-duplicate action to an allow-listed recipient. Everything else falls
through to the normal per-action approval (the safe default).
"""

from __future__ import annotations

from flowers import mandate as m
from flowers import policy
from flowers.types import Goal


def _goal(text="email the caterers", **kw):
    return Goal(text=text, **kw)


# --------------------------------------------------------------------------- parse_mandate

def test_parse_missing_or_garbled_is_empty():
    assert m.parse_mandate("not json", _goal()) == {}
    assert m.parse_mandate("{}", _goal()) == {}
    assert m.parse_mandate('{"steps": []}', _goal()) == {}        # no mandate key
    assert m.parse_mandate('{"mandate": {"action_types": []}}', _goal()) == {}  # nothing coverable


def test_parse_drops_money_and_irreversible_action_types():
    content = ('{"mandate": {"action_types": ["gmail:GMAIL_SEND_EMAIL", "gmail:GMAIL_DELETE_MESSAGE", '
               '"stripe:STRIPE_CREATE_CHARGE"], "recipient_scope": ["a@b.com"]}}')
    out = m.parse_mandate(content, _goal())
    assert out["action_types"] == ["gmail:GMAIL_SEND_EMAIL"]      # NEVER + REFUSE labels stripped


def test_parse_clamps_caps_and_forces_ceiling():
    content = ('{"mandate": {"action_types": ["gmail:GMAIL_SEND_EMAIL"], '
               '"magnitude_caps": {"max_sends": 999999, "per_recipient": 0}, '
               '"irreversibility_ceiling": "AUTO"}}')
    out = m.parse_mandate(content, _goal())
    assert out["magnitude_caps"]["max_sends"] == m._CAP_HARD["max_sends"]
    assert out["magnitude_caps"]["per_recipient"] == 1            # clamped up from 0
    assert out["irreversibility_ceiling"] == "ASK"


def test_parse_unions_goal_named_recipients():
    content = '{"mandate": {"action_types": ["gmail:GMAIL_SEND_EMAIL"], "recipient_scope": ["@acme.com"]}}'
    out = m.parse_mandate(content, _goal("email bob@named.com about lunch"))
    assert "acme.com" in out["recipient_scope"]
    assert "bob@named.com" in out["recipient_scope"]              # pulled from the goal text


def test_parse_drops_junk_scope_entries():
    content = ('{"mandate": {"action_types": ["gmail:GMAIL_SEND_EMAIL"], '
               '"recipient_scope": ["anyone", "a@b.com", "acme.com", ""]}}')
    out = m.parse_mandate(content, _goal())
    assert set(out["recipient_scope"]) == {"a@b.com", "acme.com"}  # 'anyone' / '' dropped


# --------------------------------------------------------------------------- recipients + allow-list

def test_extract_gmail_recipients_shapes():
    assert m.extract_recipients("gmail", "GMAIL_SEND_EMAIL", {"to": "a@b.com"}) == ["a@b.com"]
    assert m.extract_recipients("gmail", "GMAIL_SEND_EMAIL",
                                {"to": ["a@b.com", "c@d.com"]}) == ["a@b.com", "c@d.com"]
    assert m.extract_recipients("gmail", "GMAIL_SEND_EMAIL",
                                {"to": "Alice <a@b.com>, c@d.com"}) == ["a@b.com", "c@d.com"]
    assert m.extract_recipients("gmail", "GMAIL_SEND_EMAIL", {"subject": "hi"}) == []  # unparseable -> closed


def test_extract_browser_host():
    assert m.extract_recipients("browser", "submit", {"url": "https://book.acme.com/x?y=1"}) == ["book.acme.com"]
    assert m.extract_recipients("browser", "submit", {"target": "shop.example.org"}) == ["shop.example.org"]


def test_allowlist_exact_and_domain_and_subdomain():
    assert m._on_allowlist("alice@acme.com", ["alice@acme.com"]) is True       # exact email
    assert m._on_allowlist("alice@acme.com", ["acme.com"]) is True             # domain
    assert m._on_allowlist("alice@acme.com", ["@acme.com"]) is True            # @domain form
    assert m._on_allowlist("book.acme.com", ["acme.com"]) is True              # host subdomain
    assert m._on_allowlist("alice@acme.com", ["other.com"]) is False
    assert m._on_allowlist("alice@notacme.com", ["acme.com"]) is False         # not a suffix match
    assert m._on_allowlist("alice@acme.com.evil.com", ["acme.com"]) is False   # suffix-trick rejected


# --------------------------------------------------------------------------- covers (the truth table)

_MANDATE = {
    "action_types": ["gmail:GMAIL_SEND_EMAIL"],
    "recipient_scope": ["@acme.com", "named@x.com"],
    "magnitude_caps": {"max_sends": 5, "per_domain": 3, "per_recipient": 2},
    "irreversibility_ceiling": "ASK",
    "done_definition": "",
}


def _covers(params, tier=policy.ASK, counts=None, mandate=None):
    return m.covers(mandate if mandate is not None else _MANDATE, toolkit="gmail",
                    action="GMAIL_SEND_EMAIL", params=params, tier=tier, counts=counts or m.new_counts())


def test_covers_clean_in_scope_send():
    assert _covers({"to": "a@acme.com"}) is True
    assert _covers({"to": "named@x.com"}) is True


def test_covers_false_when_empty_mandate():
    assert m.covers({}, toolkit="gmail", action="GMAIL_SEND_EMAIL",
                    params={"to": "a@acme.com"}, tier=policy.ASK, counts=m.new_counts()) is False


def test_covers_false_for_non_ask_tier():
    assert _covers({"to": "a@acme.com"}, tier=policy.NEVER) is False
    assert _covers({"to": "a@acme.com"}, tier=policy.AUTO) is False


def test_covers_false_for_money_action():
    # even if a money label somehow sat in action_types, is_refused short-circuits covers.
    mm = dict(_MANDATE, action_types=["stripe:STRIPE_CREATE_CHARGE"])
    assert m.covers(mm, toolkit="stripe", action="STRIPE_CREATE_CHARGE",
                    params={"to": "a@acme.com"}, tier=policy.ASK, counts=m.new_counts()) is False


def test_covers_false_for_out_of_scope_action_type():
    assert m.covers(_MANDATE, toolkit="gmail", action="GMAIL_TRASH_MESSAGE",
                    params={"to": "a@acme.com"}, tier=policy.ASK, counts=m.new_counts()) is False


def test_covers_false_for_out_of_scope_recipient():
    assert _covers({"to": "attacker@evil.com"}) is False          # the injection guard
    assert _covers({"to": "a@acme.com, attacker@evil.com"}) is False  # any out-of-scope recipient -> park


def test_covers_false_when_recipient_unparseable():
    assert _covers({"subject": "no recipient"}) is False          # recipient-bearing + empty -> fail closed


def test_covers_dedupe_blocks_identical_repeat():
    counts = m.new_counts()
    params = {"to": "a@acme.com", "subject": "hi"}
    assert m.covers(_MANDATE, toolkit="gmail", action="GMAIL_SEND_EMAIL",
                    params=params, tier=policy.ASK, counts=counts) is True
    m.bump(counts, toolkit="gmail", action="GMAIL_SEND_EMAIL", params=params)
    # identical params now de-duped (a true resend is the owner's call)
    assert m.covers(_MANDATE, toolkit="gmail", action="GMAIL_SEND_EMAIL",
                    params=params, tier=policy.ASK, counts=counts) is False
    # a DIFFERENT email to the same recipient is still allowed (a follow-up)
    assert m.covers(_MANDATE, toolkit="gmail", action="GMAIL_SEND_EMAIL",
                    params={"to": "a@acme.com", "subject": "follow up"}, tier=policy.ASK,
                    counts=counts) is True


def test_covers_caps():
    counts = m.new_counts()
    # per_recipient = 2: two distinct sends ok, third to same recipient blocked
    for i in range(2):
        params = {"to": "a@acme.com", "subject": f"n{i}"}
        assert m.covers(_MANDATE, toolkit="gmail", action="GMAIL_SEND_EMAIL",
                        params=params, tier=policy.ASK, counts=counts) is True
        m.bump(counts, toolkit="gmail", action="GMAIL_SEND_EMAIL", params=params)
    assert m.covers(_MANDATE, toolkit="gmail", action="GMAIL_SEND_EMAIL",
                    params={"to": "a@acme.com", "subject": "n3"}, tier=policy.ASK, counts=counts) is False


def test_covers_max_sends_cap():
    counts = m.new_counts()
    counts["sends_total"] = _MANDATE["magnitude_caps"]["max_sends"]
    assert _covers({"to": "a@acme.com"}, counts=counts) is False


def test_covers_browser_book_scopes_the_host():
    # I5 regression: a side-effecting browser action lands on its target HOST, which must be allow-listed
    # — even though 'book'/'confirm' aren't email-style "delivering" verbs.
    mb = {"action_types": ["browser:BOOK", "browser:CONFIRM"], "recipient_scope": ["opentable.com"],
          "magnitude_caps": {"max_sends": 5, "per_domain": 5, "per_recipient": 5}}
    on = m.covers(mb, toolkit="browser", action="BOOK",
                  params={"url": "https://book.opentable.com/r/1"}, tier=policy.ASK, counts=m.new_counts())
    off = m.covers(mb, toolkit="browser", action="BOOK",
                   params={"url": "https://evil.com/checkout"}, tier=policy.ASK, counts=m.new_counts())
    nohost = m.covers(mb, toolkit="browser", action="CONFIRM",   # acts on whatever page — no host -> closed
                      params={}, tier=policy.ASK, counts=m.new_counts())
    assert on is True and off is False and nohost is False


def test_covers_recipientless_reversible_action_is_action_type_only():
    # a reversible ASK action on the owner's OWN resources (archive / calendar-confirm) carries no external
    # recipient -> scope is action-type-only -> covered without re-asking (NOT fail-closed). Regression for
    # the round-2 finding that the booking-verb broadening over-triggered fail-closed.
    mb = {"action_types": ["gmail:GMAIL_ARCHIVE_EMAIL", "googlecalendar:GOOGLECALENDAR_CONFIRM_EVENT"],
          "recipient_scope": [], "magnitude_caps": {"max_sends": 5, "per_domain": 5, "per_recipient": 5}}
    assert m.covers(mb, toolkit="gmail", action="GMAIL_ARCHIVE_EMAIL",
                    params={"id": "m1"}, tier=policy.ASK, counts=m.new_counts()) is True
    assert m.covers(mb, toolkit="googlecalendar", action="GOOGLECALENDAR_CONFIRM_EVENT",
                    params={"event_id": "e1"}, tier=policy.ASK, counts=m.new_counts()) is True
    # but a gmail SEND with no parseable recipient STILL fails closed (a genuine delivering action)
    assert m.covers({"action_types": ["gmail:GMAIL_SEND_EMAIL"], "recipient_scope": ["@acme.com"]},
                    toolkit="gmail", action="GMAIL_SEND_EMAIL", params={"subject": "no recipient"},
                    tier=policy.ASK, counts=m.new_counts()) is False


def test_covers_multi_recipient_send_respects_caps():
    # cap regression: a SINGLE send to many in-scope recipients must not blow per_domain/per_recipient.
    mb = {"action_types": ["gmail:GMAIL_SEND_EMAIL"], "recipient_scope": ["@acme.com"],
          "magnitude_caps": {"max_sends": 20, "per_domain": 3, "per_recipient": 2}}
    blast = {"to": [f"u{i}@acme.com" for i in range(10)]}            # 10 acme recipients, per_domain=3
    assert m.covers(mb, toolkit="gmail", action="GMAIL_SEND_EMAIL",
                    params=blast, tier=policy.ASK, counts=m.new_counts()) is False
    ok = {"to": ["u0@acme.com", "u1@acme.com"]}                      # 2 recipients, within per_domain=3
    assert m.covers(mb, toolkit="gmail", action="GMAIL_SEND_EMAIL",
                    params=ok, tier=policy.ASK, counts=m.new_counts()) is True


def test_bump_increments_totals():
    counts = m.new_counts()
    m.bump(counts, toolkit="gmail", action="GMAIL_SEND_EMAIL", params={"to": "a@acme.com"})
    assert counts["sends_total"] == 1
    assert counts["by_recipient"]["a@acme.com"] == 1
    assert counts["by_domain"]["acme.com"] == 1
    assert len(counts["sent_digests"]) == 1


# --------------------------------------------------------------------------- learned trust

def test_learned_covers_threshold_scope_and_floors():
    counts = {"gmail:GMAIL_ADD_LABEL": 5, "gmail:GMAIL_SEND_EMAIL": 99,
              "googlecalendar:GOOGLECALENDAR_CONFIRM_EVENT": 5}
    # at/above threshold, a reversible NON-delivering class is covered
    assert m.learned_covers(counts, toolkit="gmail", action="GMAIL_ADD_LABEL",
                            tier=policy.ASK, threshold=5) is True
    assert m.learned_covers(counts, toolkit="googlecalendar", action="GOOGLECALENDAR_CONFIRM_EVENT",
                            tier=policy.ASK, threshold=5) is True
    # below threshold -> not covered
    assert m.learned_covers({"gmail:GMAIL_ADD_LABEL": 4}, toolkit="gmail", action="GMAIL_ADD_LABEL",
                            tier=policy.ASK, threshold=5) is False
    # a DELIVERING action is never learned-covered, however high the count (no scope-widening)
    assert m.learned_covers(counts, toolkit="gmail", action="GMAIL_SEND_EMAIL",
                            tier=policy.ASK, threshold=5) is False
    # money / non-ASK never covered
    assert m.learned_covers({"stripe:STRIPE_CREATE_CHARGE": 99}, toolkit="stripe",
                            action="STRIPE_CREATE_CHARGE", tier=policy.ASK, threshold=5) is False
    assert m.learned_covers({"gmail:GMAIL_DELETE_MESSAGE": 99}, toolkit="gmail",
                            action="GMAIL_DELETE_MESSAGE", tier=policy.NEVER, threshold=5) is False
    # a personal calendar event (no attendees) IS learnable; the SAME class WITH external attendees is NOT
    # (an attendee is a recipient — the fan-out / injection surface learned trust must never widen).
    assert m.learned_covers({"googlecalendar:GOOGLECALENDAR_CREATE_EVENT": 9},
                            toolkit="googlecalendar", action="GOOGLECALENDAR_CREATE_EVENT",
                            params={"summary": "gym"}, tier=policy.ASK, threshold=5) is True
    assert m.learned_covers({"googlecalendar:GOOGLECALENDAR_CREATE_EVENT": 9},
                            toolkit="googlecalendar", action="GOOGLECALENDAR_CREATE_EVENT",
                            params={"summary": "mtg", "attendees": ["attacker@evil.com"]},
                            tier=policy.ASK, threshold=5) is False


def test_covers_scopes_calendar_attendees():
    # a mandate covering CREATE_EVENT must scope-check the ATTENDEES, not just to/cc (the fan-out surface).
    mb = {"action_types": ["googlecalendar:GOOGLECALENDAR_CREATE_EVENT"], "recipient_scope": ["@acme.com"],
          "magnitude_caps": {"max_sends": 5, "per_domain": 5, "per_recipient": 5}}
    ok = m.covers(mb, toolkit="googlecalendar", action="GOOGLECALENDAR_CREATE_EVENT",
                  params={"summary": "mtg", "attendees": ["bob@acme.com"]}, tier=policy.ASK,
                  counts=m.new_counts())
    bad = m.covers(mb, toolkit="googlecalendar", action="GOOGLECALENDAR_CREATE_EVENT",
                   params={"summary": "mtg", "attendees": ["attacker@evil.com"]}, tier=policy.ASK,
                   counts=m.new_counts())
    assert ok is True and bad is False


# --------------------------------------------------------------------------- provenance admission

def test_host_admits_registrable_domain():
    assert m.host_admits("chef@bistro.com", "bistro.com") is True
    assert m.host_admits("chef@bistro.com", "www.bistro.com") is True    # same registrable domain
    assert m.host_admits("chef@mail.bistro.com", "bistro.com") is True   # same registrable domain
    assert m.host_admits("chef@bistro.co.uk", "www.bistro.co.uk") is True  # eTLD+1 under a public suffix
    assert m.host_admits("attacker@evil.com", "bistro.com") is False     # injected on a legit page
    assert m.host_admits("a@notbistro.com", "bistro.com") is False       # suffix trick
    assert m.host_admits("noreply@co.uk", "www.bistro.co.uk") is False   # public-suffix recipient rejected
    assert m.host_admits("x@bistro.com", "co.uk") is False               # bare-suffix host admits nothing
    assert m.host_admits("", "bistro.com") is False


def test_admitted_from_fetch_only_on_domain_emails():
    events = [
        {"kind": "fetch", "url": "https://bistro.com/contact", "ok": True,
         "emails": ["chef@bistro.com", "attacker@evil.com"]},            # only the on-domain one
        {"kind": "fetch", "url": "https://other.com", "ok": False, "emails": ["x@other.com"]},  # failed fetch
        {"kind": "write", "path": "f.txt"},
    ]
    assert m.admitted_from_fetch(events) == {"chef@bistro.com"}


# --------------------------------------------------------------------------- undo-window

def test_undo_seconds_clamp():
    assert m.undo_seconds(30) == 30
    assert m.undo_seconds(999999) == 3600
    assert m.undo_seconds(-5) == 0
    assert m.undo_seconds("x") == 0
    assert m.undo_seconds(None) == 0


def test_parse_mandate_carries_undo_seconds():
    out = m.parse_mandate('{"mandate": {"action_types": ["gmail:GMAIL_SEND_EMAIL"], "undo_seconds": 45}}',
                          _goal())
    assert out["undo_seconds"] == 45
    out2 = m.parse_mandate('{"mandate": {"action_types": ["gmail:GMAIL_SEND_EMAIL"]}}', _goal())
    assert out2["undo_seconds"] == 0   # default off


# --------------------------------------------------------------------------- render

def test_render_card_and_empty():
    card = m.render_card(_MANDATE)
    low = card.lower()
    # conversational: plain-English verb, real recipients, the cap, a yes/no close — and NO raw slug/JSON.
    assert "send the email" in low
    assert "@acme.com" in card and "named@x.com" in card
    assert "5 sends" in low
    assert "yes" in low and "no" in low
    assert "GMAIL_SEND_EMAIL" not in card and "{" not in card
    assert "ask before each action" in m.render_card({}).lower()
