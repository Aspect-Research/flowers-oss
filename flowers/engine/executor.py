"""The executor — a plan-aware, failure-aware bounded tool loop for ONE step.

This is the direct fix for an earlier prototype's goal-blind, unbounded executor. It runs a step with:
  * the MASTER PLAN + this step's objective + done-criteria injected (so it never free-roams);
  * an operator-domain system prompt (search/stop/ask discipline; never claim an unverified effect);
  * SEMANTIC BUDGETS (cap searches/iterations) ABOVE the dollar ceiling; and
  * a per-tool CIRCUIT BREAKER — after K consecutive failures on a tool (e.g. a blocked search), it
    STOPS calling it and returns a tool-failed signal, instead of hammering it 50 times.

Every world-touching call goes through the broker (the executor holds no credentials). A side-effect
that needs owner authorization parks the loop (``needs_approval``) and returns a RESUME STATE; on
approval the operator calls :meth:`resume`, which executes the *exact* approved action (deterministic,
once) and CONTINUES the same loop — so the model never re-derives the action (no divergence) and a
step that sends to several recipients approves each in turn.
"""

from __future__ import annotations

import json

from flowers import mandate as mandate_lib
from flowers import memory as user_memory
from flowers.broker import Broker, BrokerResult
from flowers.engine.scheduler import CircuitBreaker, SemanticBudget
from flowers.seams.interfaces import Sandbox
from flowers.trustgate import content_hash
from flowers.types import EffectRecord, Goal, Plan, PlanStep, StepResult

_EXECUTOR_SYSTEM = """You are the executor of ONE step of a larger plan for an autonomous operator.
Work like a competent, methodical human — NOT by brute force.

Rules:
- You are given the master plan and THIS step's objective + done-criteria. Do only this step.
- Use the tools. If a web_search returns ok=false (blocked/rate-limited) or repeatedly empty, the tool
  is failing — STOP searching and proceed with what you have, or report you cannot continue. NEVER
  re-run the same failing search over and over.
- Do not over-search: a few good queries beat fifty. Use the results you have.
- For anything that sends/creates, call the tool; it may require the owner's authorization — that
  is expected, not an error.
- MONEY IS OFF-LIMITS: this agent has NO ability to pay, buy, check out, or move money. Never attempt a
  purchase / payment / checkout — whether via an integration OR by driving a site's Pay / Place-Order
  button — such a call is hard-REFUSED. If a task needs money spent, do the free parts, then
  finish(completed=false) and tell the owner exactly what needs their own (manual) payment.
- ILLEGAL / DARK-WEB IS OFF-LIMITS: never access a dark-web/.onion service, procure or make controlled
  substances / weapons / explosives, facilitate an illicit marketplace, or handle clearly illegal content.
  Such a call is hard-REFUSED; if a task requires it, finish(completed=false) and say you cannot do it.
- NEVER claim the step is done on an effect you have not actually achieved. When the objective is met,
  call finish(completed=true, summary=...). If you could NOT accomplish it, call
  finish(completed=false, summary=why) — be honest; never mark an unfinished step completed.
- If this step's job is to PRODUCE A WRITTEN DELIVERABLE for the owner (a writeup, a recommendation, a
  draft message, extracted details), your finish() summary MUST contain the actual deliverable itself —
  the full text of the ANSWER — NOT a description of it like "I compiled some spots". The owner sees ONLY
  what you put in finish(); if you describe instead of delivering, the work is lost.
- HARD CONSTRAINTS ARE PASS/FAIL, NOT PREFERENCES. Before you present anything, DROP every candidate that
  violates a hard constraint the owner stated (a price ceiling, a date, a required feature). Never present
  an option that breaks a hard constraint "for reference" — a $45 option for an "under $10" request is a
  FAILED result, not a suggestion. If NOTHING you found satisfies the hard constraints, do not pad the
  answer with near-misses: finish(completed=false, summary=...) and say plainly what you could not meet
  and the closest you found, so the owner can relax the constraint or redirect.
- OUTPUT STYLE — terse and to the point, ALWAYS. Answer exactly what was asked and nothing more: if the
  owner asked for ONE (e.g. "find me a rooftop bar"), give the SINGLE best that meets every hard
  constraint — one pick with its key facts, not a ranked menu. Return the count the owner asked for, no
  more. No emoji. No preamble, no filler, no self-congratulation. Minimal markdown (a short paragraph or a
  few plain bullet lines) — never a decorated report with medals or headers for a simple ask.
- Do ONLY this step's objective — not the whole goal. Reuse the results EARLIER STEPS ALREADY PRODUCED
  (shown below) instead of redoing them. The moment this step's objective is met, call finish() — do
  not keep calling tools.
- Be PRAGMATIC: if you found fewer items than the plan suggested but enough to make progress, proceed
  with what you have — do not block on hitting an exact count.
- Tool vocabulary: use `send_email` for email. For other connected accounts use
  integration(toolkit, action, params) with REAL names ONLY from AVAILABLE INTEGRATIONS below. Do NOT
  invent toolkit or action names; if a needed integration is not listed, finish(completed=false).
- When a task needs a website action that has NO API (filling a form, booking on a site), use
  browser(action, params): navigate/extract to READ a page, INSPECT to list the page's clickable
  elements (target the exact control by its label/selector — never guess a CSS selector), then
  submit/book/reserve to ACT (a side effect the owner authorizes). On any side-effecting browser action
  set `describe` to a one-line plain-English summary of what it will do, so the owner approves the EXACT
  action. Prefer a real integration/API over the browser whenever one is available.
- For LOCAL compute (run a script, transform/inspect a file, count/sort) use run_shell(command) — it runs
  in the sandboxed workspace (scoped dir, secrets stripped, dangerous commands refused). It is for
  computation, NOT for world effects; if a command keeps failing, stop and finish(completed=false)
  honestly — never claim the step done on a command whose last run failed."""

# OpenAI-shape tool specs (the live model uses these; FakeModel ignores them).
TOOL_SPECS = [
    {"type": "function", "function": {"name": "web_search",
        "description": "Search the web. Returns ranked results, or ok=false if blocked.",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}},
                       "required": ["query"]}}},
    {"type": "function", "function": {"name": "web_fetch",
        "description": "Fetch and read a URL's text.",
        "parameters": {"type": "object", "properties": {"url": {"type": "string"}},
                       "required": ["url"]}}},
    {"type": "function", "function": {"name": "send_email",
        "description": "Send an email (requires owner authorization).",
        "parameters": {"type": "object", "properties": {
            "to": {"type": "string"}, "subject": {"type": "string"}, "body": {"type": "string"}},
            "required": ["to", "subject"]}}},
    {"type": "function", "function": {"name": "integration",
        "description": "Call a connected integration tool. Use ONLY toolkit/action pairs listed in "
                       "AVAILABLE INTEGRATIONS; do not invent names.",
        "parameters": {"type": "object", "properties": {
            "toolkit": {"type": "string"}, "action": {"type": "string"}, "params": {"type": "object"}},
            "required": ["toolkit", "action"]}}},
    {"type": "function", "function": {"name": "browser",
        "description": "Drive a web browser for a no-API last mile. The session PERSISTS, so browse in "
                       "DISCRETE steps: navigate {url}, inspect -> the page's clickable elements "
                       "(ref/label/selector), click {selector}, type {selector, text}, extract {selector} "
                       "-> text. These read-only ops run immediately. Before a side effect, INSPECT to find "
                       "the exact control by its label (don't guess selectors). A side effect — "
                       "submit/book/reserve {url?, fill:{sel:val}, click:selector, ref, describe} — needs "
                       "the owner's authorization; set `describe` to a plain-English summary of what it "
                       "does so they approve the exact action. (Payments are NOT possible; never buy/check out.)",
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string"}, "params": {"type": "object"}}, "required": ["action"]}}},
    {"type": "function", "function": {"name": "write_file",
        "description": "Write a file in the workspace.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}}},
    {"type": "function", "function": {"name": "read_file",
        "description": "Read a workspace file.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
    {"type": "function", "function": {"name": "run_shell",
        "description": "Run a shell command in the sandboxed workspace (scoped workdir, secrets stripped, "
                       "dangerous commands refused). Use for local compute — run a script, transform a "
                       "file, count lines. NOT for sending/paying/posting (use the integration/browser "
                       "tools for those). Returns ok/exit_code/stdout/stderr.",
        "parameters": {"type": "object", "properties": {"command": {"type": "string"}},
                       "required": ["command"]}}},
    {"type": "function", "function": {"name": "remember",
        "description": "Save a durable note about THIS user for FUTURE sessions — a standing preference, an "
                       "important fact, or a correction/redirection they gave you. It persists across runs "
                       "and is shown back to you next time under WHAT YOU KNOW ABOUT THIS USER. Use it the "
                       "moment you learn something worth remembering long-term; do NOT use it for transient "
                       "step state. Keep each note to one specific, self-contained sentence.",
        "parameters": {"type": "object", "properties": {"note": {"type": "string"}},
                       "required": ["note"]}}},
    {"type": "function", "function": {"name": "finish",
        "description": "End the step. Set completed=true ONLY if you actually accomplished this step's "
                       "objective; set completed=false if you could not (be honest — never mark an "
                       "unfinished step completed).",
        "parameters": {"type": "object", "properties": {
            "completed": {"type": "boolean"}, "summary": {"type": "string"}},
            "required": ["completed", "summary"]}}},
]


class Executor:
    def __init__(self, *, budget: SemanticBudget | None = None):
        self.budget = budget or SemanticBudget()

    # ------------------------------------------------------------------ entry points
    def run(self, step: PlanStep, *, plan: Plan, goal: Goal, broker: Broker,
            sandbox: Sandbox, grants: set | None = None, user_id: str = "local",
            feedback: str = "", prior: list | None = None,
            available_tools: list | None = None, memory: str = "",
            role: str = "executor") -> StepResult:
        blob = self._step_blob(step, plan, goal, available_tools=available_tools)
        blob += user_memory.format_for_prompt(memory)
        if prior:
            done = "\n".join(f"  - {t}: {(s or '').strip()[:300]}" for t, s in prior if (s or "").strip())
            if done:
                blob += ("\n\nWHAT EARLIER STEPS ALREADY PRODUCED (use these — do not redo them):\n" + done)
        if feedback:
            blob += f"\n\nPRIOR ATTEMPT WAS REJECTED — fix this and try again: {feedback}"
        messages = [{"role": "system", "content": _EXECUTOR_SYSTEM}, {"role": "user", "content": blob}]
        return self._loop(messages, broker=broker, sandbox=sandbox, grants=grants or set(),
                          user_id=user_id, cb=CircuitBreaker(self.budget.max_consecutive_failures),
                          searches=0, tool_calls=0, effects=[], events=[], role=role)

    def resume(self, resume_state: dict, *, broker: Broker, sandbox: Sandbox,
               grants: set, user_id: str = "local") -> StepResult:
        """Execute the exact approved action (now authorized), then CONTINUE the parked loop."""
        messages = list(resume_state.get("messages") or [])
        pending = resume_state.get("pending") or {}
        cb = CircuitBreaker(self.budget.max_consecutive_failures)
        searches = int(resume_state.get("searches", 0))
        tool_calls = int(resume_state.get("tool_calls", 0))
        effects: list[EffectRecord] = []
        label = f"{pending.get('toolkit') or 'browser'}:{pending.get('action', '')}"
        br: BrokerResult = broker.perform_pending(pending=pending, user_id=user_id, grants=grants)
        if br.effect is not None:
            effects.append(br.effect)
        if br.status == "needs_approval":
            # The grant didn't authorize this exact action (divergence guard) — re-park honestly.
            return StepResult(claimed_done=False, ok=True, text="awaiting authorization", effects=effects,
                              signals={"needs_approval": br.approval, "pending_action": br.pending,
                                       "grant_key": br.grant_key,
                                       "resume": {**resume_state, "pending": {**pending, "grant_key": br.grant_key}}})
        if br.status == "needs_auth":
            # An owner-approved action whose ACCOUNT still isn't connected (e.g. approve-then-execute hit
            # authorization_required) -> re-park on CONNECT with the same resume state, so it runs once the
            # grant lands. Reuses the resume_state verbatim (the action is unchanged).
            return StepResult(claimed_done=False, ok=True, text="awaiting account connection", effects=effects,
                              signals={"needs_auth": {"url": br.auth_url,
                                                      "toolkit": pending.get("toolkit", ""),
                                                      "action": pending.get("action", "")},
                                       "resume": resume_state})
        content = (json.dumps({"ok": True, "result": br.data}) if br.status == "ok"
                   else f"{label} failed: {br.error}")
        messages.append({"role": "tool", "tool_call_id": pending.get("tc_id", ""), "content": content})
        return self._loop(messages, broker=broker, sandbox=sandbox, grants=grants, user_id=user_id,
                          cb=cb, searches=searches, tool_calls=tool_calls, effects=effects, events=[])

    # ------------------------------------------------------------------ the loop
    def _loop(self, messages, *, broker, sandbox, grants, user_id, cb, searches, tool_calls,
              effects, events, role: str = "executor") -> StepResult:
        def _result(**kw) -> StepResult:
            kw.setdefault("effects", effects)
            kw.setdefault("events", events)
            kw.setdefault("searches", searches)
            kw.setdefault("tool_failures", cb.total_failures("web_search"))
            return StepResult(**kw)

        for _ in range(self.budget.max_iterations):
            resp = broker.complete(messages, tools=TOOL_SPECS, role=role)
            if not resp.tool_calls:
                # A persistent model/transport error must NOT masquerade as an empty "done" — report it.
                if getattr(resp, "finish_reason", "") == "error":
                    return _result(claimed_done=False, ok=False,
                                   text=f"model call failed: {(resp.raw or {}).get('error', 'error')}",
                                   signals={"tool_failed": "model", "reason": "model_error"})
                events.append({"kind": "finish", "ok": True})
                return _result(claimed_done=True, ok=True, text=resp.content or "")
            messages.append({"role": "assistant", "content": resp.content or "",
                             "tool_calls": [self._tc_to_openai(tc) for tc in resp.tool_calls]})
            calls = list(resp.tool_calls)
            for idx, tc in enumerate(calls):
                tool_calls += 1
                if tc.name == "finish":
                    done = bool(tc.args.get("completed", True))
                    return _result(claimed_done=done, ok=True, text=str(tc.args.get("summary", "")),
                                   signals={} if done else {"blocked": True})
                if tool_calls > self.budget.max_tool_calls:
                    return self._forced_finish(messages, broker, _result)

                br = None
                try:
                    if tc.name == "remember":
                        # A durable, cross-session note about the user. No credentials/world-effect: the
                        # executor just emits an event; the operator persists it to the user's memory.
                        note = str((tc.args or {}).get("note", "")).strip()
                        if note:
                            events.append({"kind": "remember", "note": note})
                        content = ("noted — I'll remember that about you for next time."
                                   if note else "nothing to remember (empty note).")
                    elif tc.name == "web_search":
                        if searches >= self.budget.max_searches:
                            content = ("search budget for this step is exhausted; use the results you "
                                       "already have or call finish() with an honest status.")
                        else:
                            searches += 1
                            res = broker.search(str((tc.args or {}).get("query", "")))
                            cb.record("web_search", res.ok)
                            if not res.ok and cb.tripped("web_search"):
                                return _result(claimed_done=False, ok=False,
                                               text=f"web_search failing repeatedly ({res.reason}); stopping",
                                               signals={"tool_failed": "web_search", "reason": res.reason})
                            if not res.ok:
                                content = f"web_search failed ({res.reason}); try a different approach."
                            else:
                                payload = [{"title": r.title, "url": r.url, "snippet": r.snippet}
                                           for r in res.results]
                                content = json.dumps({"ok": True, "results": payload})
                    else:
                        content, br = self._dispatch(tc, broker=broker, sandbox=sandbox,
                                                     grants=grants, user_id=user_id)
                except Exception as e:  # a bad tool arg must NEVER crash the loop — feed the error back
                    content = (f"the tool '{tc.name}' errored: {type(e).__name__}: {e}; "
                               "fix the arguments, try a different approach, or call finish().")
                    br = None

                if tc.name == "write_file" and (tc.args or {}).get("path"):
                    # record the agent's OWN writes (with a content hash) so the gate can tell them apart
                    # from external drift AND confirm an identical-content redo (forgot-own-edit).
                    events.append({"kind": "write", "path": str(tc.args["path"]),
                                   "ok": content == "written",
                                   "hash": content_hash(str((tc.args or {}).get("content", "")))})
                if tc.name == "web_fetch" and (tc.args or {}).get("url"):
                    # record SUCCESSFUL fetches so the gate's source_membership check can verify a cited
                    # URL was actually retrieved through the proxy (anti-citation-fabrication). Also capture
                    # any EMAIL addresses on the page (bounded), so the operator can admit a discovered
                    # recipient to the mandate scope ONLY when its domain matches this page's host
                    # (provenance-tracked discovery — see flowers.mandate.host_admits).
                    fok, ems = False, []
                    try:
                        parsed = json.loads(content)
                        fok = bool(parsed.get("ok"))
                        ems = mandate_lib.emails_in(parsed.get("text") or "")[:20] if fok else []
                    except Exception:
                        fok, ems = False, []
                    ev = {"kind": "fetch", "url": str(tc.args["url"]), "ok": fok}
                    if ems:
                        ev["emails"] = ems
                    events.append(ev)
                if tc.name == "run_shell" and (tc.args or {}).get("command"):
                    # record shell runs (keyed by the command) so the gate's failed-retry floor can refuse
                    # a completion claimed over a command whose FINAL run still failed.
                    try:
                        rok = bool(json.loads(content).get("ok"))
                    except Exception:
                        rok = False
                    events.append({"kind": "run", "path": str(tc.args["command"]), "ok": rok})
                if br is not None and br.effect is not None:
                    effects.append(br.effect)
                if br is not None and br.status == "needs_approval":
                    # Park: give any later calls in this turn a placeholder result (no dangling ids),
                    # and hand back a RESUME STATE so the approved action runs deterministically on yes.
                    for later in calls[idx + 1:]:
                        messages.append({"role": "tool", "tool_call_id": later.id,
                                         "content": "deferred: awaiting approval of a prior action"})
                    resume = {"messages": messages, "searches": searches, "tool_calls": tool_calls,
                              "pending": {**(br.pending or {}), "tc_id": tc.id, "grant_key": br.grant_key}}
                    return _result(claimed_done=False, ok=True, text="awaiting authorization",
                                   signals={"needs_approval": br.approval, "pending_action": br.pending,
                                            "grant_key": br.grant_key, "resume": resume,
                                            "auto_release_seconds": br.auto_release_seconds})

                if br is not None and br.status == "needs_auth":
                    # Park on CONNECT: the user must connect this account (OAuth). Same resume-at-action
                    # machinery as approval — hand back a RESUME STATE so the EXACT action runs once the
                    # grant lands (no re-derivation), plus the consent URL + pending toolkit for the
                    # connect event + the completion poll.
                    for later in calls[idx + 1:]:
                        messages.append({"role": "tool", "tool_call_id": later.id,
                                         "content": "deferred: awaiting account connection of a prior action"})
                    resume = {"messages": messages, "searches": searches, "tool_calls": tool_calls,
                              "pending": {**(br.pending or {}), "tc_id": tc.id, "grant_key": br.grant_key}}
                    return _result(claimed_done=False, ok=True, text="awaiting account connection",
                                   signals={"needs_auth": {"url": br.auth_url,
                                                           "toolkit": (br.pending or {}).get("toolkit", ""),
                                                           "action": (br.pending or {}).get("action", "")},
                                            "resume": resume})

                messages.append({"role": "tool", "tool_call_id": tc.id, "content": content})

        return self._forced_finish(messages, broker, _result)

    def _forced_finish(self, messages, broker, _result) -> StepResult:
        """One explicit, finish-only turn when the budget is spent. HONEST: a completion is claimed
        only if the model actually finishes/concludes; otherwise the step is reported not-completed."""
        finish_only = [t for t in TOOL_SPECS if t["function"]["name"] == "finish"]
        msgs = messages + [{"role": "user", "content":
            "You are out of tool budget for this step. Call finish(summary) now with an HONEST summary "
            "of what you accomplished and anything still incomplete — do not call any other tool."}]
        try:
            resp = broker.complete(msgs, tools=finish_only, role="executor")
        except Exception:
            return _result(claimed_done=False, ok=False,
                           text="step did not complete within the budget", signals={"exhausted": True})
        for tc in (resp.tool_calls or []):
            if tc.name == "finish":
                done = bool(tc.args.get("completed", True))
                return _result(claimed_done=done, ok=True, text=str(tc.args.get("summary", "")),
                               signals={} if done else {"blocked": True})
        return _result(claimed_done=False, ok=True, text=resp.content or "step could not be completed",
                       signals={"blocked": True})

    # ------------------------------------------------------------------ dispatch (non-search tools)
    def _dispatch(self, tc, *, broker: Broker, sandbox: Sandbox, grants: set, user_id: str):
        name, args = tc.name, (tc.args or {})
        if name == "web_fetch":
            f = broker.fetch(str(args.get("url", "")))
            return (json.dumps({"ok": f.ok, "title": f.title, "text": f.text[:4000], "error": f.error}),
                    None)
        if name == "send_email":
            params = {"to": args.get("to"), "subject": args.get("subject"), "body": args.get("body", "")}
            return self._integration("gmail", "GMAIL_SEND_EMAIL", params, broker, grants, user_id)
        if name == "integration":
            return self._integration(str(args.get("toolkit", "")), str(args.get("action", "")),
                                     args.get("params") or {}, broker, grants, user_id)
        if name == "browser":
            return self._browser(str(args.get("action", "")), args.get("params") or {},
                                 broker, grants, user_id)
        if name == "write_file":
            sandbox.write_file(str(args.get("path", "")), str(args.get("content", "")))
            return ("written", None)
        if name == "read_file":
            try:
                return (sandbox.read_file(str(args.get("path", ""))), None)
            except Exception as e:
                return (f"error: {e}", None)
        if name == "run_shell":
            res = sandbox.run(str(args.get("command", "")), timeout=float(args.get("timeout", 60.0)))
            return (json.dumps({"ok": res.ok, "exit_code": res.exit_code,
                                "stdout": (res.stdout or "")[:4000],
                                "stderr": (res.stderr or "")[:2000]}), None)
        return (f"unknown tool {name}", None)

    def _integration(self, toolkit, action, params, broker, grants, user_id):
        label = f"{toolkit}:{action}"
        br: BrokerResult = broker.call_integration(toolkit=toolkit, action=action, params=params,
                                                   user_id=user_id, grants=grants)
        if br.status == "refused":
            return (f"{label} is not available — this agent does not handle payments / money. Do the rest "
                    "of the task WITHOUT it; if it cannot be completed without a payment, finish and tell "
                    "the owner exactly what THEY need to pay (what, where, how much). Do NOT retry.", br)
        if br.status == "needs_approval":
            return (f"{label} requires the owner's authorization (parked).", br)
        if br.status == "needs_auth":
            return (f"{label} needs the user to CONNECT their {toolkit} account first (parked; a connect "
                    "link is being sent). This is expected, not an error — do not retry.", br)
        if br.status == "error":
            return (f"{label} failed: {br.error}", br)
        return (json.dumps({"ok": True, "result": br.data}), br)

    def _browser(self, action, params, broker, grants, user_id):
        label = f"browser:{action}"
        br: BrokerResult = broker.call_browser(action=action, params=params, user_id=user_id, grants=grants)
        if br.status == "refused":
            return (f"{label} is not available — this agent does not handle payments / money. Do the rest "
                    "of the task WITHOUT it; if it cannot be completed without a payment, finish and tell "
                    "the owner exactly what THEY need to pay (what, where, how much). Do NOT retry.", br)
        if br.status == "needs_approval":
            return (f"{label} requires the owner's authorization (parked).", br)
        if br.status == "error":
            return (f"{label} failed: {br.error}", br)
        return (json.dumps({"ok": True, "result": br.data}), br)

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _tc_to_openai(tc) -> dict:
        return {"id": tc.id, "type": "function",
                "function": {"name": tc.name, "arguments": json.dumps(tc.args or {})}}

    @staticmethod
    def _step_blob(step: PlanStep, plan: Plan, goal: Goal, available_tools: list | None = None) -> str:
        plan_lines = "\n".join(f"  {s.index + 1}. {s.text}" for s in plan.steps)
        dc = json.dumps(step.done_criteria) if step.done_criteria else "(none specified)"
        blob = (f"OVERALL GOAL: {goal.text}\n"
                f"MASTER PLAN:\n{plan_lines}\n\n"
                f"YOUR STEP ({step.index + 1}): {step.text}\n"
                f"DONE-CRITERIA: {dc}\n"
                f"Constraints: {json.dumps(goal.constraints) if goal.constraints else '(none)'}")
        if available_tools:
            blob += ("\n\nAVAILABLE INTEGRATIONS (use integration(toolkit, action, params) with EXACTLY "
                     "these; send_email is shorthand for gmail GMAIL_SEND_EMAIL):\n"
                     + "\n".join(f"  - {t}" for t in available_tools))
        return blob
