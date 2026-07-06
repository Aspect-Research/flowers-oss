"""The broker — the single metered egress and the executor's only path to the outside world.

Every model call, search, and integration tool call goes through here. The executor (which runs in a
sandbox and holds NO credentials) calls the broker; the broker holds/reaches the model + search +
integration backends (which hold the keys). For a side-effecting integration call the broker:

  1. classifies it via the deterministic ``policy`` (auto/ask/never);
  2. if ask/never and not yet authorized -> returns ``needs_approval`` (records a ``deferred`` effect,
     so a run that claims done while the action is unauthorized is caught by the gate);
  3. if authorized (or auto) -> takes an INDEPENDENT read-back snapshot before/after, runs the action,
     and builds a typed ``EffectRecord`` with ``drift_present`` / ``expected_present`` for the gate.

This is the chokepoint that makes "never lie about what it accomplished" mechanical: the effect record
is built from an independent observation, never the executor's word.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from flowers import effects, policy
from flowers import mandate as mandate_lib
from flowers.seams.interfaces import (
    Browser,
    FetchResponse,
    Integrations,
    ModelClient,
    ModelResponse,
    SearchClient,
    SearchResponse,
)
from flowers.types import ApprovalRequest, EffectRecord

# Action TARGETS (a selector / button / ref the action TOUCHES) that mean "complete a purchase / pay".
# The spend event is a CLICK on one of these — which is AUTO-tier (read-only) by verb — so the gate must
# scan the target on EVERY browser action, not just side-effecting ones.
_PAY_TARGET_SIGNALS = (
    "checkout", "check-out", "place-order", "placeorder", "place order", "confirm-order", "confirm order",
    "complete-order", "complete-purchase", "submit-order", "submit-payment", "pay-now", "paynow", "pay now",
    "buy-now", "buynow", "buy now", "purchase", "add-payment", "payment-method", "place-bid",
)
# Field signals that mean "a card / payment input" — refuse TYPING into one (or a card-number value).
_PAY_FIELD_SIGNALS = ("card-number", "cardnumber", "card number", "cardnum", "card-num", "cvv", "cvc",
                      "credit-card", "creditcard", "credit card", "security-code", "card-expiry")
def _looks_like_card_number(value) -> bool:
    """True iff a typed value is a card-number-shaped run of 13-19 digits (spaces/dashes ignored)."""
    digits = re.sub(r"[\s-]", "", str(value or ""))
    return digits.isdigit() and 13 <= len(digits) <= 19


# Verbs that ACTIVATE something on the current page (could trigger a charge) — page-content is read for these.
_CLICK_FAMILY = frozenset({"click", "press", "tap", "submit", "confirm", "book", "reserve", "order", "post"})
# CARD-FIELD markers — present on a checkout/PAYMENT FORM but NOT on a priced listing/cart/product page,
# so requiring >=2 of these distinguishes 'a payment page' from 'a page that merely shows a price'.
_PAYMENT_FORM_MARKERS = (
    "card number", "cardnumber", "card-number", "cvv", "cvc", "security code", "card holder", "cardholder",
    "name on card", "expiration date", "expiry date", "mm/yy", "mm / yy", "credit card number", "debit card",
    "billing zip", "card details", "payment details", "card expiry",
)


def _page_is_payment_form(text: str) -> bool:
    """True iff the visible page text shows a CARD/PAYMENT INPUT FORM (not merely a price). Precise card-
    field markers (card number / cvv / expiration / name on card) appear on a checkout/payment page but NOT
    on a listing/cart/product page, so >=2 markers means 'a real payment form' — catching a generically-named
    button on a payment page without over-blocking ordinary priced pages."""
    low = (text or "").lower()
    return sum(1 for m in _PAYMENT_FORM_MARKERS if m in low) >= 2


def _browser_spend_attempt(action: str, params: dict) -> bool:
    """True iff a browser action would SPEND money or ENTER PAYMENT DATA — regardless of its verb. The
    real spend event is a discrete ``click`` on a Pay / Place-Order button (AUTO-tier by verb!), or a
    ``type`` of a card number; both must be refused even though they classify read-only. We scan the
    action's TARGET (selector/click/ref), the typed FIELD, and any card-number VALUE — but NOT the url, so
    NAVIGATING to / reading a checkout page is still allowed (reading spends nothing). Money is spent on the
    button, not the page."""
    p = params or {}
    target = (str(action or "") + " "
              + " ".join(str(p.get(k) or "") for k in ("selector", "click", "ref", "target", "button"))).lower()
    if any(s in target for s in _PAY_TARGET_SIGNALS):
        return True
    field = (str(p.get("selector") or "") + " " + str(p.get("field") or "")).lower()
    if any(s in field for s in _PAY_FIELD_SIGNALS):
        return True
    fills = p.get("fill") if isinstance(p.get("fill"), dict) else {}
    return any(_looks_like_card_number(v) for v in [p.get("text"), p.get("value"), *fills.values()])


@dataclass
class BrokerResult:
    status: str                       # "ok" | "needs_approval" | "needs_auth" | "refused" | "error"
    ok: bool = False
    data: Any = None
    effect: EffectRecord | None = None
    approval: ApprovalRequest | None = None
    cost_usd: float = 0.0
    grant_key: str = ""
    pending: dict | None = None   # the deferred {toolkit, action, params} (for resume-at-action)
    auto_release_seconds: int = 0  # >0: a mandate undo-window soft-confirm — auto-release after N seconds
    auth_url: str = ""            # needs_auth: the consent URL to send the user to connect their account
    error: str | None = None


def _is_auth_required(err: str | None) -> bool:
    """True iff a backend error means 'the user has not connected this account' (vs a genuine failure) —
    the trigger for the connect round-trip. The adapter prefixes these ``authorization_required:``."""
    low = (err or "").lower()
    return "authorization_required" in low or "authorization required" in low


class Broker:
    def __init__(
        self,
        *,
        model: ModelClient | None = None,
        search: SearchClient | None = None,
        integrations: Integrations | None = None,
        browser: Browser | None = None,
        overrides: dict | None = None,
        mandate: dict | None = None,
        mandate_counts: dict | None = None,
        trust: dict | None = None,
        trust_threshold: int = mandate_lib.LEARNED_TRUST_THRESHOLD,
        on_usage: Callable[..., None] | None = None,
        on_activity: Callable[[str], None] | None = None,
        actor: str = "executor",
        run_id: str = "",
        verify_attempts: int = 1,
        verify_delay: float = 0.0,
        forwarded_gks: set | None = None,
        verified_gks: set | None = None,
    ):
        self.model = model
        self.search_client = search
        self.integrations = integrations
        self.browser = browser
        self.overrides = overrides or {}
        # The owner-approved autonomy scope: when non-empty, an in-scope/in-cap/reversible action is
        # auto-authorized (widens ok_auth) WITHOUT touching verification. ``mandate_counts`` is the hot
        # per-step magnitude counter (enforces caps within one executor loop); the operator persists it
        # back to RunState after the step. Default-empty -> _mandate_covers always False -> ask-everything.
        self.mandate = mandate or {}
        self.mandate_counts = mandate_lib.new_counts(mandate_counts)
        # Cross-run LEARNED trust: per-user clean-approval counts per action class. Auto-covers ONLY
        # reversible non-delivering classes past the threshold (see mandate.learned_covers) — never a
        # send/recipient action, so it can't widen the injection surface.
        self.trust = trust or {}
        self.trust_threshold = trust_threshold
        self.on_usage = on_usage
        self.on_activity = on_activity
        self.actor = actor
        self.run_id = run_id
        # Verification read-back can retry for provider eventual-consistency (a just-sent email may
        # lag in Sent). Offline defaults to a single instant check (verify_attempts=1, no delay).
        self.verify_attempts = verify_attempts
        self.verify_delay = verify_delay
        # Run-scoped IDEMPOTENCY: the set of grant_keys of side-effects already VERIFIED-as-landed in
        # this run (seeded by the operator from the persisted effect ledger, then extended in-loop as
        # this broker forwards+verifies more). A byte-identical action whose gk is in here is NEVER
        # re-executed and NEVER re-prompted — the no-double-send invariant made mechanical across
        # retries, ladder climbs, plan replans, and process restarts. An action enters the set when the
        # provider ACCEPTED it and the read-back did not POSITIVELY show it missing (landed OR
        # unverifiable): re-issuing a provider-accepted send just because we couldn't verify it is how
        # duplicates happen (found live: a scope-blocked read-back made the executor re-send). Only a
        # read-back that proves the effect did NOT land (expected_present False) re-opens the action.
        self._forwarded_gks: set = set(forwarded_gks or ())
        # The VERIFIED subset of the above (read-back confirmed landed). An idempotent replay may only
        # claim expected_present=True when the ORIGINAL verified — replaying an unverifiable send as
        # verified would fabricate the very evidence the gate exists to demand.
        self._verified_gks: set = set(verified_gks or ())
        self.spent_usd = 0.0

    # ---------------------------------------------------------------- metering
    def _meter(self, kind: str, cost_usd: float, detail: dict) -> None:
        if cost_usd:
            self.spent_usd += cost_usd
        if self.on_usage:
            self.on_usage(kind=kind, cost_usd=cost_usd, detail=detail)

    def _activity(self, text: str) -> None:
        """Best-effort PRE-call heartbeat: the broker is the one chokepoint that sees every long
        provider call before it starts (a model call can block for minutes behind retries), so this is
        where "the timeline is alive" progress comes from. Never raises, never blocks the call."""
        if self.on_activity is None:
            return
        try:
            self.on_activity(text)
        except Exception:
            pass

    # ---------------------------------------------------------------- model / search
    def complete(self, messages: list[dict], *, tools=None, role: str = "executor",
                 response_format=None, max_tokens=None) -> ModelResponse:
        if self.model is None:
            raise RuntimeError("broker has no model client wired")
        self._activity("thinking…")
        resp = self.model.complete(messages, tools=tools, role=role,
                                   response_format=response_format, max_tokens=max_tokens)
        self._meter("model", resp.cost_usd, {"role": role})
        return resp

    def search(self, query: str, *, k: int = 6) -> SearchResponse:
        if self.search_client is None:
            raise RuntimeError("broker has no search client wired")
        self._activity(f"searching: {query[:80]}")
        self._meter("search", 0.0, {"query": query})
        return self.search_client.search(query, k=k)

    def fetch(self, url: str) -> FetchResponse:
        if self.search_client is None:
            raise RuntimeError("broker has no search client wired")
        return self.search_client.fetch(url)

    # ---------------------------------------------------------------- integrations (the trust path)
    def grant_key_for(self, toolkit: str, action: str, params: dict) -> str:
        """An EXACT-ACTION authorization key: a grant authorizes ONLY a call with byte-identical params
        (same recipient/subject/body/target), never every action of that toolkit and never a different
        target just because a partial semantic fingerprint is absent. Binding to the full params (not
        the read-back fingerprint) is what makes one 'yes' authorize exactly the action the owner saw."""
        return f"{toolkit}:{action}|{mandate_lib.params_digest(params or {})}"

    def _mandate_covers(self, toolkit: str, action: str, params: dict, tier: str) -> bool:
        """Does the owner-approved mandate auto-authorize THIS action? A thin wrapper over the pure
        predicate (:func:`flowers.mandate.covers`): True ONLY for a reversible (ASK-tier), non-money,
        in-scope, in-cap, non-duplicate action to an allow-listed recipient. This is OR'd into ``ok_auth``
        and is NEVER consulted by ``side`` or the read-back gate — widening authorization cannot weaken
        verification."""
        return mandate_lib.covers(self.mandate, toolkit=toolkit, action=action,
                                  params=params or {}, tier=tier, counts=self.mandate_counts)

    @staticmethod
    def _safe_snapshot(fn) -> dict | None:
        """Run a read-back snapshot/observe; a backend/parser crash degrades to None (-> unverifiable ->
        ask owner) rather than propagating and DROPPING the effect record (which would let a claimed
        done slip through with no record to flag). Never raises."""
        try:
            return fn()
        except Exception:
            return None

    def _refuse(self, *, toolkit: str, action: str,
                reason: str = "money/payment is not a capability of this agent") -> BrokerResult:
        """A categorical HARD-REFUSAL (money/payment): no approval, no grant, no pending, no execute — and
        a phase='refused' EffectRecord so the gate sees a non-completion if the run nonetheless claims
        done. This is what makes 'flowers cannot spend your money' a capability fact, not an approval
        prompt — there is no path from here to executing the action."""
        label = f"{toolkit}:{action}"
        eff = EffectRecord(toolkit=toolkit, action=action, side_effecting=True, phase="refused",
                           actor=self.actor, label=label)
        return BrokerResult(status="refused", ok=False, effect=eff, error=reason)

    def _already_done(self, *, toolkit: str, action: str, gk: str, label: str,
                      browser: bool = False) -> BrokerResult:
        """Idempotency short-circuit: a byte-identical side-effect (same ``gk``) already VERIFIED-landed
        earlier in this run, so the no-double-send invariant forbids re-executing OR re-prompting it. We
        record an HONEST ``forwarded`` replay effect (the action's objective genuinely IS accomplished — it
        was sent and read-back-verified the first time) marked ``idempotent_replay`` for the audit trail,
        and return ok WITHOUT touching the backend. This blocks BOTH authorization paths — a cached owner
        grant (silent re-execute) and a fresh per-action approval (a reflexive owner 'yes' on a duplicate)
        — closing the replan/ladder duplicate-send hole. For a composio effect whose ORIGINAL was
        read-back-verified, the replay carries ``expected_present=True`` so the step's own
        ``effect_landed`` is satisfied without a duplicate; an UNVERIFIABLE original replays as
        unverifiable (expected_present=None) — the gate still routes it to the owner, it just can't be
        re-sent. A cua/browser replay carries no independent observer, so the gate conservatively
        routes it to the owner rather than auto-verifying a replay it did not itself observe."""
        verified = gk in self._verified_gks
        eff = EffectRecord(toolkit=toolkit, action=action, side_effecting=True, phase="forwarded",
                           drift_present=True if verified else None,
                           expected_present=True if verified else None,
                           effect_kind="cua" if browser else "composio", actor=self.actor, label=label)
        eff.detail["grant_key"] = gk
        eff.detail["idempotent_replay"] = True
        return BrokerResult(status="ok", ok=True, effect=eff,
                            data={"idempotent": True,
                                  "note": "already completed earlier in this run — not re-sent"})

    def _queue_undo(self, *, toolkit: str, action: str, params: dict, tier: str, undo: int) -> BrokerResult:
        """A mandate undo-window soft-confirm: a covered send PARKS (kind='undo') with an auto-release
        timer instead of forwarding now, so the owner gets a few seconds to veto. On release (the timer
        fires, or an early yes) the issued grant authorizes the EXACT action and it forwards + verifies
        normally — so the undo-window never weakens verification, it only delays the send."""
        browser = toolkit == "browser"
        label = f"{toolkit}:{action}"
        eff = EffectRecord(toolkit=toolkit, action=action, side_effecting=True, phase="deferred",
                           effect_kind="cua" if browser else "composio", actor=self.actor, label=label)
        tgt = params.get("to") or params.get("target") or params.get("url") or ""
        prompt = (f"About to {label}" + (f" → {tgt}" if tgt else "")
                  + f". I'll go ahead in {undo}s unless you reply STOP.")
        apr = ApprovalRequest(run_id=self.run_id, kind="undo", prompt=prompt, options=["stop"],
                              tier=tier, effect_label=label)
        pending = ({"browser": True, "action": action, "params": params} if browser
                   else {"toolkit": toolkit, "action": action, "params": params})
        return BrokerResult(status="needs_approval", ok=False, effect=eff, approval=apr,
                            grant_key=self.grant_key_for(toolkit, action, params),
                            auto_release_seconds=undo, pending=pending)

    def call_integration(self, *, toolkit: str, action: str, params: dict, user_id: str,
                         authorized: bool = False, grants: set | None = None) -> BrokerResult:
        if self.integrations is None:
            raise RuntimeError("broker has no integrations backend wired")
        params = params or {}
        if policy.is_refused(toolkit, action):
            return self._refuse(toolkit=toolkit, action=action)
        self._activity(f"calling {toolkit}:{action}")
        tier = policy.classify(toolkit, action, overrides=self.overrides)
        # WHETHER to verify a world effect comes from the NATURAL tier (not overridable): an owner's
        # auto override may waive the APPROVAL prompt, but never the independent read-back verification.
        side = policy.is_side_effecting(toolkit, action)
        must_approve = tier in (policy.ASK, policy.NEVER)
        label = f"{toolkit}:{action}"
        gk = self.grant_key_for(toolkit, action, params)
        # IDEMPOTENCY (before auth/approval): this exact action already VERIFIED-landed this run -> never
        # re-send or re-prompt (the no-double-send invariant). Catches a replanned/re-attempted step that
        # re-issues a send a prior step already made, AND a within-loop identical re-issue.
        if side and gk in self._forwarded_gks:
            return self._already_done(toolkit=toolkit, action=action, gk=gk, label=label)
        # The mandate widens authorization (NOT verification): an in-scope/in-cap/reversible action is
        # auto-authorized. Requiring `side` ties coverage to exactly the actions that get read-back-
        # verified. _mandate_covers re-asserts tier==ASK and not is_refused, so money/NEVER are unreachable.
        already_authed = bool(authorized) or (gk in (grants or set()))
        mandate_covered = side and self._mandate_covers(toolkit, action, params, tier)
        # Undo-window: a mandate-covered send (not yet owner-authorized) with undo_seconds>0 becomes a timed
        # SOFT-CONFIRM — parks with an auto-release timer the owner can veto, instead of forwarding now.
        undo = mandate_lib.undo_seconds((self.mandate or {}).get("undo_seconds"))   # coerce/clamp fail-closed
        if mandate_covered and undo > 0 and not already_authed:
            return self._queue_undo(toolkit=toolkit, action=action, params=params, tier=tier, undo=undo)
        # Learned trust auto-covers a reversible class with NO external recipient the owner has approved
        # >= threshold times (label/archive/personal-event) — never a send/invite (has_recipient_intent),
        # so no scope widening. It does NOT rescue a class the ACTIVE mandate governs (its caps bind there).
        learned_covered = (side and not mandate_covered
                           and not mandate_lib.lists_action(self.mandate, toolkit, action)
                           and mandate_lib.learned_covers(
                               self.trust, toolkit=toolkit, action=action, params=params,
                               tier=tier, threshold=self.trust_threshold))
        ok_auth = already_authed or mandate_covered or learned_covered

        if must_approve and not ok_auth:
            eff = EffectRecord(toolkit=toolkit, action=action, side_effecting=True,
                               phase="deferred", actor=self.actor, label=label)
            kind = "never" if tier == policy.NEVER else "side_effect"
            fp = self.integrations.fingerprint(toolkit=toolkit, action=action, params=params)
            detail = f" -> {json.dumps(fp)}" if fp else ""
            prompt = (f"Authorize {label}{detail}? ({tier}-tier"
                      + (" — irreversible/money" if tier == policy.NEVER else "") + ")")
            # Draft-then-send preview: surface the LITERAL body of an outbound message so the owner sees
            # exactly what goes out under their name before approving (the trust answer to impersonation).
            # The grant binds the full params (byte-identical body), so this is pure UI surfacing.
            body = str(params.get("body") or params.get("text") or params.get("message") or "").strip()
            if body:
                preview = body if len(body) <= 600 else body[:600] + "…"
                prompt = f"About to send (under your name):\n\n{preview}\n\n{prompt}"
            apr = ApprovalRequest(run_id=self.run_id, kind=kind, prompt=prompt,
                                  options=["yes", "no"], tier=tier, effect_label=label)
            return BrokerResult(status="needs_approval", ok=False, effect=eff, approval=apr,
                                grant_key=gk,
                                pending={"toolkit": toolkit, "action": action, "params": params})

        before = self._safe_snapshot(lambda: self.integrations.snapshot(
            toolkit=toolkit, action=action, params=params, user_id=user_id)) if side else None
        try:
            ex = self.integrations.execute(toolkit=toolkit, action=action, params=params, user_id=user_id)
        except Exception as exc:  # a backend failure is a result, not a crash
            eff = EffectRecord(toolkit=toolkit, action=action, side_effecting=side,
                               phase="failed", actor=self.actor, label=label)
            eff.detail["grant_key"] = gk   # identity: a verified retry of THIS action can supersede it
            return BrokerResult(status="error", ok=False, effect=eff, error=f"{type(exc).__name__}: {exc}")

        if not ex.ok:
            # The user has not CONNECTED this account yet: surface needs_auth (a consent URL + the pending
            # action) instead of collapsing to a tool failure, so the operator can park + send a connect
            # link + resume-at-action once granted. (Money/illegal REFUSE was handled far above — a refused
            # action never reaches here.) A 'deferred' effect is recorded so a claimed-done while still
            # unconnected is caught by the gate. Falls through to a real error if no consent URL is available.
            if _is_auth_required(ex.error):
                authorize = getattr(self.integrations, "authorize", None)
                res = authorize(toolkit, user_id) if callable(authorize) else ("error", "")
                status, url = res[0], res[1]
                if url and status != "completed":
                    eff = EffectRecord(toolkit=toolkit, action=action, side_effecting=side,
                                       phase="deferred", actor=self.actor, label=label)
                    return BrokerResult(status="needs_auth", ok=False, effect=eff, auth_url=url,
                                        pending={"toolkit": toolkit, "action": action, "params": params},
                                        error=ex.error)
            eff = EffectRecord(toolkit=toolkit, action=action, side_effecting=side,
                               phase="failed", actor=self.actor, label=label)
            eff.detail["grant_key"] = gk   # identity: a verified retry of THIS action can supersede it
            return BrokerResult(status="error", ok=False, effect=eff, error=ex.error)

        if not side:
            eff = EffectRecord(toolkit=toolkit, action=action, side_effecting=False,
                               phase="forwarded", actor=self.actor, label=label)
            return BrokerResult(status="ok", ok=True, data=ex.data, effect=eff)

        # Read back the effect, retrying for provider eventual-consistency. Offline (verify_attempts=1)
        # this is a single instant check; live, it polls until the expected effect appears or times out.
        fp = self.integrations.fingerprint(toolkit=toolkit, action=action, params=params)
        # Bind a CREATE's verification to the EXACT record it returned (its id), so a concurrent or
        # pre-existing SAME-TITLE item can't false-verify a create that did not land: the matched read-back
        # item's own id must equal the id WE just created. An id-bound create that returns NO id (a
        # dropped/fabricated create) gets an UNMATCHABLE id -> still hard-refuses (never a title-only match).
        _ck = getattr(self.integrations, "created_key", None)
        created = _ck(toolkit=toolkit, action=action, data=ex.data) if callable(_ck) else None
        if created is not None:
            fp = {**(fp or {}), "id": str(created) if created else "\x00:flowers-no-created-id"}
        drift, expected = None, None
        for i in range(max(1, self.verify_attempts)):
            after = self._safe_snapshot(lambda: self.integrations.snapshot(
                toolkit=toolkit, action=action, params=params, user_id=user_id))
            if before is None or after is None:
                drift, expected = None, None        # no reliable read-back -> unverifiable (ask owner)
                break
            diff = effects.snapshot_diff(before, after)
            drift = effects.has_effect(diff)
            expected = effects.has_expected_effect(before, after, fp)
            if expected is True or (fp is None and drift):
                break
            if i < self.verify_attempts - 1 and self.verify_delay > 0:
                time.sleep(self.verify_delay)
        eff = EffectRecord(toolkit=toolkit, action=action, side_effecting=True, phase="forwarded",
                           drift_present=drift, expected_present=expected, effect_kind="composio",
                           actor=self.actor, label=label)
        eff.detail["grant_key"] = gk   # bind the effect to its grant so the run can dedup an exact re-send
        if mandate_covered:   # count the forwarded send against the caps + stamp the audit trail
            mandate_lib.bump(self.mandate_counts, toolkit=toolkit, action=action, params=params)
            eff.detail["authorized_by"] = "mandate"
        elif learned_covered:
            eff.detail["authorized_by"] = "learned"
        if side and expected is not False:   # landed OR unverifiable -> never re-send this run;
            self._forwarded_gks.add(gk)      # only a read-back PROVING it missing re-opens the action
        if side and expected is True:
            self._verified_gks.add(gk)       # a replay of THIS action may honestly claim verified
        return BrokerResult(status="ok", ok=True, data=ex.data, effect=eff)

    # ---------------------------------------------------------------- browser (the cua trust path)
    def call_browser(self, *, action: str, params: dict, user_id: str,
                     authorized: bool = False, grants: set | None = None) -> BrokerResult:
        """Drive a no-API last-mile browser action. A side-effecting action (submit/book/pay) parks for
        owner authorization exactly like an integration, then — once authorized — is verified by an
        INDEPENDENT observation: the effect record carries ``effect_kind='cua'`` with ``observer`` (the
        independent re-observation identity) DISTINCT from ``actor`` (the acting session), so the gate's
        provenance branch can accept it only on the expected fingerprint via that independent observer —
        never on the agent's own screenshot/self-report. No independent observation -> ask the owner."""
        if self.browser is None:
            raise RuntimeError("broker has no browser backend wired")
        params = params or {}
        self._activity(f"browser: {action}")
        # The browser is the ONE place money can actually be spent. The spend event is a CLICK on a Pay /
        # Place-Order button — which is AUTO-tier by verb — and entering a card number via TYPE (also AUTO).
        # So the money gate runs UNCONDITIONALLY (NOT behind is_side_effecting, which would skip exactly the
        # discrete clicks/types that spend): refuse a lexically-money action OR any action whose TARGET is a
        # pay/checkout button / card field / card-number value. Reading (navigate/extract) is still allowed.
        if policy.is_refused("browser", action) or _browser_spend_attempt(action, params):
            return self._refuse(toolkit="browser", action=action,
                                reason="this is a payment / checkout / card action, which flowers cannot perform")
        # Illegal-content floor (E1): refuse a navigate/action whose TARGET is a .onion hidden service or
        # otherwise reveals disallowed intent — the URL/target lives in params (not the action slug), so
        # is_refused(toolkit, action) can't see it; scan the target text the same way the money floor does.
        _tgt = " ".join(str((params or {}).get(k) or "")
                        for k in ("url", "target", "selector", "query", "text"))
        if policy.is_disallowed_text(f"{action} {_tgt}"):
            return self._refuse(toolkit="browser", action=action,
                                reason="this is a disallowed / illegal-content action, which flowers cannot perform")
        # Page-content money floor (defense-in-depth beyond the target-signal gate): a CLICK on the CURRENT
        # page (no url -> acting on the page in front of it) that is an actual PAYMENT/CARD FORM is refused —
        # this catches a GENERICALLY-named button on a checkout page that the target-signal can't recognize.
        # Precise (>=2 card-field markers), so an ordinary priced listing/cart page is NOT over-blocked.
        if (action or "").strip().lower() in _CLICK_FAMILY and not params.get("url"):
            try:
                pg = self.browser.act(action="extract", params={}, user_id=user_id)
                if getattr(pg, "ok", False) and _page_is_payment_form(getattr(pg, "text", "")):
                    return self._refuse(toolkit="browser", action=action,
                                        reason="this page is a payment/checkout form, which flowers cannot submit")
            except Exception:
                pass   # best-effort: a read failure falls through to the (primary) target-signal gate above
        tier = policy.classify("browser", action, overrides=self.overrides)
        side = policy.is_side_effecting("browser", action)   # NATURAL: verification is not overridable
        must_approve = tier in (policy.ASK, policy.NEVER)
        label = f"browser:{action}"
        fp = self.browser.fingerprint(action=action, params=params)
        gk = self.grant_key_for("browser", action, params)   # bound to the EXACT params (incl. target/url)
        # IDEMPOTENCY (before auth/approval): this exact browser action already VERIFIED-landed this run ->
        # never re-do or re-prompt (the no-double-send invariant — e.g. a re-clicked "book"/"submit").
        if side and gk in self._forwarded_gks:
            return self._already_done(toolkit="browser", action=action, gk=gk, label=label, browser=True)
        already_authed = bool(authorized) or (gk in (grants or set()))
        mandate_covered = side and self._mandate_covers("browser", action, params, tier)
        undo = mandate_lib.undo_seconds((self.mandate or {}).get("undo_seconds"))   # coerce/clamp fail-closed
        if mandate_covered and undo > 0 and not already_authed:
            return self._queue_undo(toolkit="browser", action=action, params=params, tier=tier, undo=undo)
        ok_auth = already_authed or mandate_covered

        if must_approve and not ok_auth:
            eff = EffectRecord(toolkit="browser", action=action, side_effecting=True, phase="deferred",
                               effect_kind="cua", actor=self.actor, label=label)
            kind = "never" if tier == policy.NEVER else "side_effect"
            tgt = params.get("target") or params.get("url") or ""
            detail = f" -> {json.dumps(fp)}" if fp else (f" -> {tgt}" if tgt else "")
            obs = params.get("observe_url")
            if obs:   # surface the VERIFICATION channel so the owner consents to it, not just the action
                detail += f" [verify via {params.get('observe_via') or 'http'}: {obs}]"
            prompt = (f"Authorize {label}{detail}? ({tier}-tier"
                      + (" — irreversible/money" if tier == policy.NEVER else "") + ")")
            # observe-then-act: the model's plain-English description of the resolved action (from an
            # `inspect` it just did) leads the prompt, so the owner approves the EXACT thing, not a selector.
            desc = str(params.get("describe") or "").strip()
            if desc:
                prompt = f"{desc}\n\n{prompt}"
            apr = ApprovalRequest(run_id=self.run_id, kind=kind, prompt=prompt,
                                  options=["yes", "no"], tier=tier, effect_label=label)
            return BrokerResult(status="needs_approval", ok=False, effect=eff, approval=apr,
                                grant_key=gk,
                                pending={"browser": True, "action": action, "params": params})

        before = self._safe_snapshot(
            lambda: self.browser.observe(action=action, params=params, user_id=user_id)) if side else None
        try:
            res = self.browser.act(action=action, params=params, user_id=user_id)
        except Exception as exc:  # a backend failure is a result, not a crash
            eff = EffectRecord(toolkit="browser", action=action, side_effecting=side, phase="failed",
                               effect_kind="cua", actor=self.actor, label=label)
            eff.detail["grant_key"] = gk   # identity: a verified retry of THIS action can supersede it
            return BrokerResult(status="error", ok=False, effect=eff, error=f"{type(exc).__name__}: {exc}")
        if not res.ok:
            eff = EffectRecord(toolkit="browser", action=action, side_effecting=side, phase="failed",
                               effect_kind="cua", actor=res.actor or self.actor, label=label)
            eff.detail["grant_key"] = gk   # identity: a verified retry of THIS action can supersede it
            return BrokerResult(status="error", ok=False, effect=eff, error=res.error)

        if not side:
            # read-only page driving: nothing to verify, just hand back the text/url (+ any inspect
            # candidates, so the model can pick the exact control to act on next — observe-then-act).
            eff = EffectRecord(toolkit="browser", action=action, side_effecting=False, phase="forwarded",
                               effect_kind="cua", actor=res.actor, label=label)
            data = {"text": res.text, "url": res.url}
            els = getattr(res, "elements", None)
            if els:
                data["elements"] = els
            return BrokerResult(status="ok", ok=True, data=data, effect=eff)

        # Independent read-back (retry for eventual consistency), matched against the expected fingerprint.
        observer = self.browser.observer_id(user_id)
        drift, expected = None, None
        for i in range(max(1, self.verify_attempts)):
            after = self._safe_snapshot(
                lambda: self.browser.observe(action=action, params=params, user_id=user_id))
            if before is None or after is None:
                drift, expected = None, None          # no independent observation -> unverifiable (ask owner)
                break
            diff = effects.snapshot_diff(before, after)
            drift = effects.has_effect(diff)
            expected = effects.has_expected_effect(before, after, fp)
            if expected is True or (fp is None and drift):
                break
            if i < self.verify_attempts - 1 and self.verify_delay > 0:
                time.sleep(self.verify_delay)
        eff = EffectRecord(toolkit="browser", action=action, side_effecting=True, phase="forwarded",
                           drift_present=drift, expected_present=expected, effect_kind="cua",
                           actor=res.actor, observer=observer, label=label)
        eff.detail["grant_key"] = gk   # bind the effect to its grant so the run can dedup an exact re-do
        if mandate_covered:   # count the forwarded action against the caps + stamp the audit trail
            mandate_lib.bump(self.mandate_counts, toolkit="browser", action=action, params=params)
            eff.detail["authorized_by"] = "mandate"
        if side and expected is not False:   # landed OR unverifiable -> never re-do this run;
            self._forwarded_gks.add(gk)      # only an observation PROVING it missing re-opens the action
        if side and expected is True:
            self._verified_gks.add(gk)
        return BrokerResult(status="ok", ok=True, data={"text": res.text, "url": res.url}, effect=eff)

    def perform_pending(self, *, pending: dict, user_id: str, grants: set | None = None) -> BrokerResult:
        """Execute a parked action (now authorized), routing to the right backend. Used by
        executor.resume so a browser approval resumes through call_browser, an integration through
        call_integration — the resume-at-action machinery is shared."""
        pending = pending or {}
        toolkit = "browser" if pending.get("browser") else pending.get("toolkit", "")
        action = pending.get("action", "")
        # Belt-and-suspenders: a refused (money) action can never be resumed/executed, even if a stale or
        # forged grant routed it here (call_* also refuse, so this is defense-in-depth).
        if policy.is_refused(toolkit, action):
            return self._refuse(toolkit=toolkit, action=action)
        if pending.get("browser"):
            return self.call_browser(action=action, params=pending.get("params") or {},
                                     user_id=user_id, grants=grants)
        return self.call_integration(toolkit=toolkit, action=action,
                                     params=pending.get("params") or {}, user_id=user_id, grants=grants)
