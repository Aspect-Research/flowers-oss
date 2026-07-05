"""Money is an ARCHITECTURAL non-capability — hard-REFUSED, never approvable or executable.

"flowers physically cannot spend your money" is a capability fact, not an approval prompt: a money
action returns a categorical REFUSE from policy and the broker short-circuits it with no approval/grant/
pending/execute.
"""

from __future__ import annotations

from _harness import build, make_brain, tc

from flowers import policy
from flowers.broker import Broker
from flowers.seams.browser import FakeBrowser
from flowers.seams.integrations import FakeIntegrations
from flowers.seams.telemetry import LocalTracer
from flowers.types import Goal, RunStatus

# --------------------------------------------------------------------------- policy

def test_policy_refuses_money_and_is_unoverridable():
    assert policy.classify("stripe", "CREATE_PAYMENT") == policy.REFUSE     # money toolkit
    assert policy.classify("acme", "CHECKOUT_CART") == policy.REFUSE        # money stem
    assert policy.classify("acme", "make-payment") == policy.REFUSE
    assert policy.classify("browser", "pay") == policy.REFUSE
    # un-overridable: NO override can turn a money action into anything executable
    assert policy.classify("stripe", "CREATE_PAYMENT", overrides={"stripe:CREATE_PAYMENT": "auto"}) == policy.REFUSE
    assert policy.classify("acme", "pay", overrides={"acme:pay": "ask"}) == policy.REFUSE
    # non-money actions are unchanged
    assert policy.classify("gmail", "GMAIL_SEND_EMAIL") == policy.ASK
    assert policy.is_refused("paypal", "anything") is True
    assert policy.is_refused("gmail", "GMAIL_SEND_EMAIL") is False


# --------------------------------------------------------------------------- broker (the authoritative floor)

def test_broker_refuses_money_integration_with_no_approval_path():
    b = Broker(integrations=FakeIntegrations())
    r = b.call_integration(toolkit="stripe", action="CREATE_PAYMENT", params={"amount": 100}, user_id="u")
    assert r.status == "refused" and r.ok is False
    assert r.approval is None and r.grant_key == "" and r.pending is None   # nothing to approve/grant/resume
    assert r.effect is not None and r.effect.phase == "refused"


def test_broker_refuses_browser_checkout_even_with_a_generic_action():
    b = Broker(browser=FakeBrowser())
    assert b.call_browser(action="pay", params={"url": "https://shop/cart"}, user_id="u").status == "refused"
    # GENERIC action='submit' on a checkout page -> caught by the param payment-signal, not the verb
    rc = b.call_browser(action="submit", params={"url": "https://shop/checkout", "ref": "place-order"}, user_id="u")
    assert rc.status == "refused"
    # a benign side-effecting submit is NOT refused (it parks for approval as normal)
    rb = b.call_browser(action="submit", params={"url": "https://site/contact", "ref": "send"}, user_id="u")
    assert rb.status != "refused"


def test_perform_pending_cannot_resume_a_money_action():
    # belt-and-suspenders: even a (stale/forged) parked money action can't be executed on resume
    b = Broker(integrations=FakeIntegrations())
    r = b.perform_pending(pending={"toolkit": "stripe", "action": "CREATE_PAYMENT", "params": {}}, user_id="u")
    assert r.status == "refused"


# --------------------------------------------------------------------------- through the production path

def test_money_action_never_reaches_done():
    # a step that attempts a payment can never reach DONE — the gate sees a refused (non-verified) effect
    brain = make_brain(
        steps=[{"text": "pay the invoice"}],
        actions={"pay the invoice": [tc("integration", toolkit="stripe", action="CREATE_PAYMENT",
                                        params={"amount": 50})]})
    h = build(model=brain)
    run = h["op"].start(Goal(text="g"))
    assert run.status is RunStatus.ESCALATED
    assert any(e.phase == "refused" for e in h["store"].get_effects(run.run_id))   # recorded, never landed


def test_money_attempt_is_logged_to_internal_telemetry():
    # if the model reaches for money anyway (it shouldn't — money is removed from the planner/affordances),
    # the refusal is logged to OUR telemetry as a tripwire we can monitor — an internal signal, never a
    # user-facing 'the agent tried to pay' artifact.
    tracer = LocalTracer()
    brain = make_brain(
        steps=[{"text": "pay it"}],
        actions={"pay it": [tc("integration", toolkit="stripe", action="CREATE_PAYMENT", params={})]})
    h = build(model=brain, tracer=tracer)
    h["op"].start(Goal(text="g"))
    assert any(s.name == "money_attempt_refused" for s in tracer.spans())


# ---------------------------------------------------------- adversarial-review fixes

def test_money_detection_is_precise_no_false_positives():
    # REVIEW FIX: naked substring stems hard-blocked legit consumer actions (a regression once money
    # became a categorical REFUSE). These are NOT money and must be allowed:
    for a in ["transfer_file", "data_transfer", "transfer_ownership", "transfer_repository",
              "wireless_connect", "wired_connection", "rewire_config", "recharge_device", "discharge_summary",
              "charger_status", "charge_battery", "list_invoices", "view_invoice", "view_billing_history",
              "get_billing_address", "build_payload", "copay_lookup", "view_paystub", "buyer_info"]:
        assert policy.is_refused("app", a) is False, a
    # real money is STILL refused (toolkit, word-boundary spend verb, or explicit financial phrase):
    for a in ["pay", "PAYMENT", "make-payment", "PAYNOW", "checkout", "check-out", "purchase", "refund",
              "buy", "BUY_NOW", "wire_transfer", "charge_card", "TRANSFER_FUNDS", "SEND_MONEY"]:
        assert policy.is_refused("app", a) is True, a
    assert policy.is_refused("stripe", "GET_BALANCE") is True   # payment toolkit -> refused


def test_browser_refuses_the_spend_event_regardless_of_verb():
    # CRITICAL REVIEW FIX: the spend event is a discrete CLICK on a pay/order button (AUTO by verb), or a
    # TYPE of a card number (also AUTO) — the gate must refuse these even though they classify read-only,
    # while still ALLOWING reads (navigate/extract) of a checkout page.
    b = Broker(browser=FakeBrowser())

    def st(a, p):
        return b.call_browser(action=a, params=p, user_id="u").status

    # the holes — all REFUSED now:
    assert st("click", {"selector": "#place-order"}) == "refused"
    assert st("click", {"selector": "button.pay-now"}) == "refused"
    assert st("click", {"selector": "#confirm-order", "url": "https://x/cart/9"}) == "refused"
    assert st("type", {"selector": "#x", "text": "4242 4242 4242 4242"}) == "refused"   # a card number
    assert st("type", {"selector": "#card-number", "text": "x"}) == "refused"           # a card field
    # reads + benign actions still work:
    assert st("navigate", {"url": "https://x/checkout"}) != "refused"
    assert st("extract", {"selector": "body"}) != "refused"
    assert st("click", {"selector": "a.listing"}) != "refused"
    assert st("type", {"selector": "#name", "text": "Asa Shepard"}) != "refused"


def test_browser_refuses_a_generic_click_on_a_payment_form_page():
    # REVIEW FIX #1: a GENERICALLY-named button (no pay/checkout word) on a page that IS a card/payment
    # FORM is refused via page-content detection — without over-blocking an ordinary priced listing page.
    pay_page = "Complete your order\nCard Number\nCVV\nExpiration date MM/YY\nName on card\nTotal: $49.99"
    listing = "Used couch — $80\nGreat condition. Contact seller. Add to cart."
    fb = FakeBrowser(pages={"checkout": pay_page, "listing": listing})
    b = Broker(browser=fb)

    def click_on(page_url):
        fb.act(action="navigate", params={"url": page_url}, user_id="u")        # land on the page
        return b.call_browser(action="click", params={"selector": "#submit"}, user_id="u").status

    assert click_on("https://shop/checkout") == "refused"      # a card-form page -> generic click refused
    assert click_on("https://market/listing") != "refused"     # an ordinary priced page -> click allowed
