"""The capability policy — tiers, the money/irreversible floor, and owner overrides."""

from __future__ import annotations

from flowers import policy as p


def test_read_verb_is_auto():
    assert p.classify("gmail", "GMAIL_FETCH_EMAILS") == p.AUTO
    assert p.classify("web", "SEARCH") == p.AUTO


def test_send_is_ask():
    assert p.classify("gmail", "GMAIL_SEND_EMAIL") == p.ASK
    assert p.classify("googlecalendar", "GOOGLECALENDAR_CREATE_EVENT") == p.ASK


def test_delete_is_never_and_money_is_refused():
    # irreversible NON-money (delete) -> NEVER (ask-owner); money -> REFUSE (a categorical non-capability)
    assert p.classify("gmail", "GMAIL_DELETE_MESSAGE") == p.NEVER
    assert p.classify("stripe", "STRIPE_CREATE_CHARGE") == p.REFUSE  # 'charge' verb -> money -> refuse
    assert p.classify("vendor", "PAY_INVOICE") == p.REFUSE


def test_bulk_egress_is_ask_not_auto():
    assert p.classify("gmail", "GMAIL_EXPORT_MAILBOX") == p.ASK


def test_unknown_verb_defaults_to_ask():
    assert p.classify("frob", "FROBNICATE_THING") == p.ASK


def test_policy_table_overrides_heuristic():
    # 'delete' verb would be never anyway, but the table pins gmail delete-message to never explicitly.
    assert p.classify("gmail", "GMAIL_DELETE_MESSAGE") == p.NEVER
    # a draft delete is intentionally only ask
    assert p.classify("gmail", "GMAIL_DELETE_DRAFT") == p.ASK


def test_owner_override_can_loosen_reversible_to_auto():
    ov = {"gmail:GMAIL_SEND_EMAIL": p.AUTO}
    assert p.classify("gmail", "GMAIL_SEND_EMAIL", overrides=ov) == p.AUTO


def test_owner_override_cannot_loosen_never_to_auto():
    ov = {"gmail:GMAIL_DELETE_MESSAGE": p.AUTO}
    assert p.classify("gmail", "GMAIL_DELETE_MESSAGE", overrides=ov) == p.NEVER


def test_owner_override_cannot_reach_a_money_action():
    # money is a categorical non-capability now: NO override (auto OR ask) makes it executable.
    assert p.classify("stripe", "STRIPE_CREATE_PAYOUT",
                      overrides={"stripe:STRIPE_CREATE_PAYOUT": p.AUTO}) == p.REFUSE
    assert p.classify("stripe", "STRIPE_UPDATE_CUSTOMER",
                      overrides={"stripe:STRIPE_UPDATE_CUSTOMER": p.AUTO}) == p.REFUSE  # money-by-toolkit
    # a non-money never-natural action still stays NEVER under an override (the irreversible floor)
    assert p.classify("gmail", "GMAIL_DELETE_MESSAGE", overrides={"gmail": p.AUTO}) == p.NEVER


def test_owner_override_can_raise_strictness():
    ov = {"gmail": p.NEVER}
    assert p.classify("gmail", "GMAIL_SEND_EMAIL", overrides=ov) == p.NEVER


def test_is_money_action():
    assert p.is_money_action("stripe", "STRIPE_UPDATE_CUSTOMER") is True   # toolkit
    assert p.is_money_action("vendor", "PAY_INVOICE") is True              # verb
    assert p.is_money_action("gmail", "GMAIL_SEND_EMAIL") is False


def test_helpers():
    assert p.is_side_effecting("gmail", "GMAIL_SEND_EMAIL") is True
    assert p.is_side_effecting("gmail", "GMAIL_FETCH_EMAILS") is False


# --------------------------------------------------------------------------- override floor (audit fix)

def test_override_cannot_loosen_the_never_floor():
    # HARDENED (audit): an override may RAISE strictness or lower a benign tier, but it can NEVER loosen a
    # never-natural action below NEVER. Previously only an AUTO override was clamped — an ASK override
    # slipped through and downgraded NEVER -> ASK (contradicting the 'never-tier stays NEVER' docstring).
    nev = "GMAIL_DELETE_MESSAGE"
    assert p.classify("gmail", nev) == p.NEVER
    assert p.classify("gmail", nev, overrides={f"gmail:{nev}": "ask"}) == p.NEVER    # was ASK (the bug)
    assert p.classify("gmail", nev, overrides={f"gmail:{nev}": "auto"}) == p.NEVER
    # benign tiers can still be raised (ask->never) or lowered (ask->auto) by an override
    assert p.classify("gmail", "GMAIL_SEND_EMAIL", overrides={"gmail:GMAIL_SEND_EMAIL": "never"}) == p.NEVER
    assert p.classify("gmail", "GMAIL_SEND_EMAIL", overrides={"gmail:GMAIL_SEND_EMAIL": "auto"}) == p.AUTO


def test_money_toolkit_is_refused_even_for_a_read():
    # the whole payments-toolkit surface is a non-capability now (read or write) -> REFUSE, un-overridable
    assert p.classify("stripe", "GET_BALANCE") == p.REFUSE
    assert p.classify("stripe", "GET_BALANCE", overrides={"stripe:GET_BALANCE": "auto"}) == p.REFUSE
