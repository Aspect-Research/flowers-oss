"""The planner — goal -> a revisable, batch-structured master DAG.

The decisive inversion of an earlier prototype's "smallest set of steps / don't pad" bias: for outreach/sourcing
goals the plan is EXPLICITLY batch-structured with wait nodes, e.g.
``search(batch) -> email_batch -> AWAIT replies(window, k) -> evaluate -> [next batch]``. The model
proposes steps as JSON; ``_parse_steps`` is the real (deterministic) code under test — it validates
the DAG (backward-only deps, clamps cycles/forward-refs, caps the step count) and coerces kinds. A
model that is unavailable or returns junk degrades to a single generic step (fail-open, never crash).
"""

from __future__ import annotations

import json

from flowers import mandate as mandate_lib
from flowers import memory as user_memory
from flowers.types import Goal, Plan, PlanStep, StepKind, StepStatus


def _memory_blob(md: str) -> str:
    """Inject what we already know about this user, so the plan is informed by prior sessions."""
    return user_memory.format_for_prompt(md)

_PLANNER_SYSTEM = """You are the planner for an autonomous everything-operator. Decompose the goal into
a SMALL, ORDERED dependency graph of concrete steps that a competent human would actually follow —
methodically, not by brute force. Match the number of results to what the owner actually asked for: if
they asked for ONE ("find me a rooftop bar"), plan to research candidates and then SELECT THE SINGLE BEST
that meets ALL stated constraints — the deliverable is one recommendation, not a menu of five. For
outreach/sourcing goals, structure the work as BATCHES with explicit WAIT steps: find a small batch (e.g.
5), contact them, then WAIT a reasonable time for replies before doing more — do NOT enumerate hundreds.

Return ONLY JSON: {"steps": [{"text": str, "kind": "generic"|"await_replies"|"monitor"|"recurring",
"depends_on": [int...], "needs": [str...], "params": {...}, "done_criteria": [{...}]}]}.
- depends_on are 0-based indices of EARLIER steps (backward-only).
- kind "await_replies" params: {"window_seconds": int, "min_replies": int, "match": {"subject"?:str,"from"?:str}}.
- kind "monitor" params: {"interval_seconds": int, "probe": "inbox"|"url"|"browser", "url"?: str,
  "match": {...}, "notify": str, "max_polls"?: int, "confirm_polls"?: int}. Use this to WATCH something over
  hours/days until it flips (a restock, a price drop, a cancellation slot, a reply). For probe "inbox" match
  is {subject?,from?}; for "url"/"browser" (watch a PAGE) match is {contains?|absent?} substrings over the
  page text (e.g. {"absent": "Sold Out"} = back in stock). Set "max_polls" for a multi-day watch.
  "confirm_polls" = how many CONSECUTIVE matching reads before firing (default 2 for a page watch, to
  debounce a flicker); set confirm_polls=1 + a short interval to SNIPE a fleeting opening instantly.
  Richer page conditions (linear, no regex): {"count": {"of": str, "at_least"?: int, "at_most"?: int}},
  {"number_near": {"anchor": str, "at_least"?|"at_most"?|"equals"?: number}} (e.g. anchor "$" at_most 50 =
  'price below $50'), {"changed": true} (fire when the page changes at all).
- kind "recurring" params: {"interval_seconds": int, "max_occurrences"?: int, "until_ts"?: float,
  "notify"?: str}. Use this for a CRON-ish heartbeat that repeats on a schedule (e.g. "every morning remind
  me ..."), bounded by max_occurrences/until_ts; each occurrence notifies the owner.
- WATCH-THEN-ACT: a monitor (or await_replies) step can TRIGGER a dependent ACTION step the moment it
  flips — give the action step `depends_on: [<monitor index>]` and it runs automatically in the SAME
  durable run when the watch fires (the action still parks for approval / rides the mandate + gate). Prefer
  this over a bare watch-and-notify whenever the goal is to DO something on the signal. Canonical shapes —
  e.g. snipe a cancellation slot then book it:
    {"steps":[{"text":"watch the booking page for an open slot","kind":"monitor",
               "params":{"interval_seconds":120,"probe":"url","url":"...","confirm_polls":1,
                         "match":{"absent":"fully booked"}}},
              {"text":"book the freed slot (hand off the card moment to the owner)","kind":"generic",
               "depends_on":[0]}]}
  Other shapes: watch a restock then add-to-cart-and-hand-off; await a reply then act on its verdict
  (offer/accept/reject); a condition flips then email the human who can resolve it.
- For any step that SENDS/CREATES/BOOKS (a side-effect), set "produces": "<toolkit>:<ACTION>" to
  the exact capability label from AVAILABLE CAPABILITIES below, so the gate can REQUIRE that the effect
  actually landed. Use ONLY labels listed there; never invent toolkit/action names.
- HARD CONSTRAINTS the owner stated (a price ceiling, a date, a required feature) are PASS/FAIL, not
  preferences. Plan the work so the deliverable can actually SHOW it meets them (e.g. a research step that
  captures each option's price), and end with a SELECT-THE-BEST synthesis step that returns only an option
  meeting every hard constraint. An independent verifier checks the final answer against these constraints,
  so a plan that can't surface the evidence (e.g. never gathers prices for a budget goal) will be sent back.
- MONEY/PAYMENT IS NOT A CAPABILITY: this operator CANNOT pay, buy, check out, or move money — never plan
  a step that spends money. If the goal needs a purchase/payment, plan the FREE work up to that point, then
  a final step that HANDS OFF to the owner to pay themselves (state what to pay, where, and how much).
- ILLEGAL / DARK-WEB IS OFF-LIMITS: never plan a step that accesses a dark-web/.onion service, procures or
  makes controlled substances / weapons / explosives, facilitates an illicit marketplace, or touches any
  clearly illegal content. Such a request is hard-REFUSED at the door; do not plan around it.
- Keep it to the fewest steps that genuinely accomplish the goal.

You MAY also return an optional "mandate" object describing the AUTONOMY you want for this run, so the owner
can approve it ONCE (a single card) instead of being asked before every send. Shape:
  "mandate": {"action_types": ["<toolkit>:<ACTION>", ...], "recipient_scope": ["email or @domain", ...],
              "magnitude_caps": {"max_sends": int, "per_domain": int, "per_recipient": int},
              "done_definition": "one sentence: what 'done' means"}
Rules for the mandate (it is enforced deterministically; over-reaching is stripped, so be precise):
- action_types: ONLY reversible send/create labels this plan actually uses (e.g. gmail:GMAIL_SEND_EMAIL).
  NEVER list a delete/pay/cancel/irreversible action — those always ask the owner regardless.
- recipient_scope: ONLY specific recipients the goal names or clearly implies — exact emails or @domains.
  NEVER "anyone" / a wildcard / a recipient you only inferred from web text. A recipient not in scope will
  simply ask the owner (the safe default), so keep this tight and concrete.
- magnitude_caps: a sensible ceiling on volume (e.g. a handful of recipients, 1-2 follow-ups each).
- Omit the mandate entirely if the goal needs no autonomous sends (research/monitor-only)."""

_PLAN_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "plan",
        "schema": {
            "type": "object",
            "properties": {"steps": {"type": "array", "items": {"type": "object"}},
                           "mandate": {"type": "object"}},
            "required": ["steps"],
        },
    },
}


def _coerce_kind(value) -> StepKind:
    try:
        return StepKind(str(value or "generic").lower())
    except ValueError:
        return StepKind.GENERIC


class Planner:
    def __init__(self, model, *, max_steps: int = 12):
        self.model = model
        self.max_steps = max_steps

    def _complete(self, messages, *, broker=None):
        client = broker or self.model
        return client.complete(messages, role="planner", response_format=_PLAN_RESPONSE_FORMAT)

    # ------------------------------------------------------------------ public
    def plan(self, goal: Goal, *, broker=None, catalog=None, memory: str = "") -> Plan:
        if not getattr(self.model, "available", lambda: False)():
            return self.single_task_plan(goal)
        try:
            messages = [
                {"role": "system", "content": _PLANNER_SYSTEM},
                {"role": "user", "content": self._goal_blob(goal) + self._catalog_blob(catalog)
                                            + _memory_blob(memory)},
            ]
            resp = self._complete(messages, broker=broker)
            steps = self._parse_steps(resp.content, goal.text)
            # The planner may also propose an autonomy mandate; parse_mandate is the deterministic
            # validator (drops money/irreversible labels, clamps caps, unions goal-named recipients).
            # Only attach it to a REAL multi-step plan (a degenerate single-step fallback gets none).
            mandate = mandate_lib.parse_mandate(resp.content, goal) if steps else {}
        except Exception:
            steps, mandate = [], {}
        return Plan(steps=steps or self._single_steps(goal.text), goal_text=goal.text, mandate=mandate)

    def replan(self, goal: Goal, done_steps: list[PlanStep], reason: str,
               new_info: str = "", *, broker=None, catalog=None, memory: str = "") -> Plan:
        """Revise the FUTURE plan while PRESERVING completed work. Done steps are kept verbatim
        (marked DONE), re-indexed first; the model proposes new remaining steps that depend on them."""
        done = [PlanStep(index=i, text=s.text, kind=s.kind, status=StepStatus.DONE,
                         params=dict(s.params), done_criteria=list(s.done_criteria))
                for i, s in enumerate(done_steps)]
        if not getattr(self.model, "available", lambda: False)():
            return Plan(steps=done, goal_text=goal.text)
        try:
            blob = (self._goal_blob(goal)
                    + f"\n\nCOMPLETED (do not redo): {[s.text for s in done]}"
                    + f"\nWHY REPLAN: {reason}\nNEW INFO: {new_info}"
                    + "\nReturn ONLY the REMAINING steps as JSON {\"steps\":[...]} (their depends_on may "
                    + f"reference completed steps by index 0..{len(done) - 1})."
                    + self._catalog_blob(catalog) + _memory_blob(memory))
            resp = self._complete([{"role": "system", "content": _PLANNER_SYSTEM},
                                   {"role": "user", "content": blob}], broker=broker)
            new_steps = self._parse_steps(resp.content, goal.text, offset=len(done))
        except Exception:
            new_steps = []
        return Plan(steps=done + new_steps, goal_text=goal.text)

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _goal_blob(goal: Goal) -> str:
        blob = f"GOAL: {goal.text}\nBUDGET_USD: {goal.budget_usd}"
        if goal.constraints:
            blob += f"\nCONSTRAINTS: {json.dumps(goal.constraints)}"
        return blob

    @staticmethod
    def _catalog_blob(catalog) -> str:
        if not catalog:
            return ""
        lines = "\n".join(f"  - {c['label']}: {c.get('description', '')}"
                          + (" [side-effecting]" if c.get("side_effecting") else "")
                          for c in catalog)
        return "\n\nAVAILABLE CAPABILITIES (use these exact labels for `produces`/`needs`):\n" + lines

    def single_task_plan(self, goal: Goal) -> Plan:
        return Plan(steps=self._single_steps(goal.text), goal_text=goal.text)

    @staticmethod
    def _single_steps(goal_text: str) -> list[PlanStep]:
        return [PlanStep(index=0, text=goal_text, kind=StepKind.GENERIC)]

    def _parse_steps(self, content: str, goal_text: str, *, offset: int = 0) -> list[PlanStep]:
        """Parse + VALIDATE model step JSON into a clean DAG. The real deterministic code under test:
        backward-only deps (relative to the full, offset-adjusted index), cycle/forward-ref clamping,
        kind coercion, and a hard step cap."""
        try:
            data = json.loads(content)
        except (TypeError, ValueError):
            return []
        raw = data.get("steps") if isinstance(data, dict) else (data if isinstance(data, list) else None)
        if not isinstance(raw, list):
            return []
        steps: list[PlanStep] = []
        for item in raw[: self.max_steps]:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or item.get("step") or "").strip()
            if not text:
                continue
            idx = offset + len(steps)
            deps_in = item.get("depends_on") or item.get("deps") or []
            deps: list[int] = []
            for d in deps_in if isinstance(deps_in, list) else []:
                try:
                    di = int(d)
                except (TypeError, ValueError):
                    continue
                if 0 <= di < idx and di not in deps:   # backward-only; drop self/forward/cycle refs
                    deps.append(di)
            params = item.get("params") if isinstance(item.get("params"), dict) else {}
            dc = list(item.get("done_criteria")) if isinstance(item.get("done_criteria"), list) else []
            # A declared `produces` effect label -> a deterministic effect_landed criterion, so the gate
            # REQUIRES the side-effect actually landed (closes the no-op-accept gap at the source).
            # Only `effect_landed` is auto-emitted here. The file-deliverable
            # objective checks (`file_exists`/`file_count`/`regex_present`, implemented in trustgate) are NOT
            # planner-emitted — the empty-claim / unsupported-completion floor already refuses a fabricated
            # file-producing step. A model MAY still hand-author them in done_criteria. Auto-emitting a
            # file_exists rule for deliverable steps is a future hardening, not needed for v1.
            produces = str(item.get("produces") or "").strip()
            if produces and ":" in produces and not any(
                    isinstance(c, dict) and isinstance(c.get("objective_check"), dict)
                    and c["objective_check"].get("kind") == "effect_landed" for c in dc):
                dc.append({"id": "effect_landed",
                           "objective_check": {"kind": "effect_landed", "params": {"label": produces}}})
            needs = [str(n) for n in (item.get("needs") or []) if isinstance(item.get("needs"), list)]
            steps.append(PlanStep(index=idx, text=text, kind=_coerce_kind(item.get("kind")),
                                  depends_on=deps, needs=needs, params=dict(params),
                                  done_criteria=dc))
        return steps
