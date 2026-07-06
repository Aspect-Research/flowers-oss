"""Integrations seam — per-user OAuth tool calling (Gmail/Calendar/Slack/...).

The live backend is **Arcade.dev**: the LLM never sees the OAuth token; the engine injects creds
and returns only the payload. The broker orchestrates verification by taking an INDEPENDENT
``snapshot`` of the read-back surface before/after ``execute`` and matching the AFTER state against
``fingerprint`` (see ``flowers.effects``). An action with no read-back surface returns
``snapshot(...) is None`` -> the gate routes it to ``unverifiable`` (ask the owner).

``FakeIntegrations`` is the offline model the whole engine + test suite runs against; it can simulate
a send that does NOT land (the fabricated-completion case) and a toolkit with no read-back.
"""

from __future__ import annotations

import re

from flowers import runtime
from flowers.seams.interfaces import ExecResult

# (toolkit_lower, ACTION_UPPER) -> the arg fields identifying THIS action's expected effect. After
# forwarding, an ADDED read-back item must MATCH these (see flowers.effects). Shared by Fake + Arcade.
_EXPECTED_FIELDS: dict[tuple[str, str], list[str]] = {
    ("gmail", "GMAIL_SEND_EMAIL"): ["to", "recipient_email", "subject"],
    # trash/label identify the affected message ONLY by id (the live tools take just email_id), so the
    # fingerprint keys off the id — matched against the `id` field _parse_emails now exposes on the
    # ADDED read-back item (the trashed msg appears in TRASH; the labeled msg appears in the label list).
    ("gmail", "GMAIL_TRASH_MESSAGE"): ["email_id", "message_id", "id"],
    ("gmail", "GMAIL_ADD_LABEL"): ["email_id", "message_id", "id"],
    ("googlecalendar", "GOOGLECALENDAR_CREATE_EVENT"): ["summary", "title"],
}

# (toolkit_lower, ACTION_UPPER) -> ("write"|"read", resource). The resource is the surface the action
# affects (for a write) or reads (for a read). A write's resource is what the broker reads back.
_ACTIONS: dict[tuple[str, str], tuple[str, str]] = {
    ("gmail", "GMAIL_SEND_EMAIL"): ("write", "sent"),
    ("gmail", "GMAIL_FETCH_EMAILS"): ("read", "inbox"),
    ("gmail", "GMAIL_SEARCH_EMAILS"): ("read", "inbox"),
    ("gmail", "GMAIL_TRASH_MESSAGE"): ("write", "trash"),
    ("gmail", "GMAIL_ADD_LABEL"): ("write", "labeled"),
    ("googlecalendar", "GOOGLECALENDAR_CREATE_EVENT"): ("write", "events"),
    ("googlecalendar", "GOOGLECALENDAR_LIST_EVENTS"): ("read", "events"),
}

# Actions that are side-effecting but have NO reliable INDEPENDENT read-back -> the gate routes them to
# ``unverifiable`` (ask the owner), both live (``readback: None``) and offline (snapshot returns None).
# Empty in the Gmail+Calendar surface (every supported write IS read-back-verifiable); kept as the seam a
# future toolkit's change-not-add write (e.g. an in-place edit) would plug into.
_UNVERIFIABLE_ACTIONS: frozenset[tuple[str, str]] = frozenset()

# CREATE actions that fingerprint on a free-text TITLE/SUMMARY but whose provider response carries the
# new record's stable ID. The broker binds verification to THAT id (exact), so a concurrent/pre-existing
# SAME-TITLE item can never false-verify a create that did not land (the read-back item's own id must
# equal the id WE just created). Each extractor pulls the id out of the execute() response.
_CREATED_KEY_EXTRACTORS: dict[tuple[str, str], object] = {
    ("googlecalendar", "GOOGLECALENDAR_CREATE_EVENT"):
        lambda d: str((d.get("event") or {}).get("id") or d.get("id") or d.get("event_id") or ""),
}
_ID_BOUND_CREATE_ACTIONS: frozenset[tuple[str, str]] = frozenset(_CREATED_KEY_EXTRACTORS)


def created_record_id(toolkit: str, action: str, data) -> str | None:
    """The stable id of the record an execute() just created, for the id-bound CREATE actions — used by
    the broker to bind verification to the EXACT record (not any same-title item). Returns the id string,
    ``""`` when the action is id-bound but the response carried no id (a dropped/fabricated create -> the
    broker makes it UNMATCHABLE so it still hard-refuses), or ``None`` when the action is not id-bound."""
    ex = _CREATED_KEY_EXTRACTORS.get(_key(toolkit, action))
    if ex is None:
        return None
    try:
        return ex(data if isinstance(data, dict) else {})
    except Exception:
        return ""


# The canonical action vocabulary the planner + executor are TOLD about, so the planner uses real
# labels (and attaches effect_landed to side-effecting steps) and the executor stops inventing tool
# names. Backends map these to their own tools (Arcade: Gmail.SendEmail, etc.).
CAPABILITY_CATALOG: list[dict] = [
    {"toolkit": "gmail", "action": "GMAIL_SEND_EMAIL", "label": "gmail:GMAIL_SEND_EMAIL",
     "description": "Send an email (use the send_email tool)", "side_effecting": True},
    {"toolkit": "gmail", "action": "GMAIL_FETCH_EMAILS", "label": "gmail:GMAIL_FETCH_EMAILS",
     "description": "Read recent emails", "side_effecting": False},
    {"toolkit": "gmail", "action": "GMAIL_SEARCH_EMAILS", "label": "gmail:GMAIL_SEARCH_EMAILS",
     "description": "Search/filter emails by sender, subject, body, label, or date range "
                    "(e.g. unread from a specific person)", "side_effecting": False},
    {"toolkit": "gmail", "action": "GMAIL_TRASH_MESSAGE", "label": "gmail:GMAIL_TRASH_MESSAGE",
     "description": "Move an email to Trash by id (reversible; use to clear spam/clutter)",
     "side_effecting": True},
    {"toolkit": "gmail", "action": "GMAIL_ADD_LABEL", "label": "gmail:GMAIL_ADD_LABEL",
     "description": "Add a label to an email by id (organize the inbox)", "side_effecting": True},
    {"toolkit": "googlecalendar", "action": "GOOGLECALENDAR_CREATE_EVENT",
     "label": "googlecalendar:GOOGLECALENDAR_CREATE_EVENT",
     "description": "Create a calendar event", "side_effecting": True},
    {"toolkit": "googlecalendar", "action": "GOOGLECALENDAR_LIST_EVENTS",
     "label": "googlecalendar:GOOGLECALENDAR_LIST_EVENTS",
     "description": "List upcoming calendar events", "side_effecting": False},
]


def _key(toolkit: str, action: str) -> tuple[str, str]:
    return ((toolkit or "").lower(), (action or "").upper())


# A representative WRITE tool per toolkit — authorizing it consents the whole scope (granting
# Gmail.SendEmail grants gmail.send). Used by ArcadeIntegrations.authorize to map a toolkit -> the tool
# whose OAuth flow the user completes. A value already containing "." is treated as an explicit tool name.
_AUTH_TOOL: dict[str, str] = {
    "gmail": "Gmail.SendEmail",
    "googlecalendar": "GoogleCalendar.CreateEvent",
}


def _auth_tool_for(toolkit_or_tool: str) -> str:
    s = str(toolkit_or_tool or "").strip()
    if "." in s:           # already a qualified tool name (e.g. "Gmail.SendEmail")
        return s
    return _AUTH_TOOL.get(s.lower(), s)


def fingerprint_for(toolkit: str, action: str, params: dict) -> dict | None:
    """The expected-effect fingerprint dict for a (toolkit, action), or None."""
    fields = _EXPECTED_FIELDS.get(_key(toolkit, action))
    if not fields:
        return None
    fp = {f: params[f] for f in fields if (params or {}).get(f) not in (None, "")}
    return fp or None


class FakeIntegrations:
    """An in-memory, fully offline integrations backend for the engine + tests.

    State is per user: ``{user_id: {resource: {item_id: fields}}}``. Construct with:
      * ``drop_actions`` — a set of (toolkit, ACTION) tuples whose ``execute`` returns ok but does NOT
        land the effect (simulates a provider that accepted the call but nothing appeared) — the
        fabricated-completion case the gate must refuse;
      * ``no_readback`` — toolkits with no read-back surface (``snapshot`` returns None -> unverifiable).
    """

    def __init__(self, *, drop_actions=(), no_readback=(), unauthorized=()):
        self._state: dict[str, dict[str, dict[str, dict]]] = {}
        self._drop = {(_key(t, a)) for (t, a) in drop_actions}
        self._no_readback = {(t or "").lower() for t in no_readback}
        # toolkits the user has NOT yet connected: execute() fails with authorization_required and
        # authorize() returns a (pending, url) — the offline model of the OAuth connect round-trip.
        self._unauthorized = {(t or "").lower() for t in unauthorized}
        self._counter = 0

    # ---- helpers ----
    def available(self) -> bool:
        return True

    def _resource(self, user_id: str, resource: str) -> dict:
        return self._state.setdefault(user_id, {}).setdefault(resource, {})

    def _next_id(self) -> str:
        self._counter += 1
        return f"item_{self._counter}"

    def surface(self, user_id: str, resource: str) -> dict:
        """Test/assert helper: a copy of a user's surface."""
        return {k: dict(v) for k, v in self._resource(user_id, resource).items()}

    def deliver_inbound(self, user_id: str, *, sender: str, subject: str, body: str = "") -> str:
        """Inject an inbound message into the user's inbox (for await/monitor scenarios)."""
        iid = self._next_id()
        self._resource(user_id, "inbox")[iid] = {"from": sender, "subject": subject, "body": body}
        return iid

    # ---- connect round-trip (offline model) ----
    def authorize(self, toolkit_or_tool: str, user_id: str) -> tuple[str, str]:
        """(status, url) for connecting a toolkit/tool. 'completed' (no url) when already connected, else
        'pending' + a fake consent URL. Mirrors ArcadeIntegrations.authorize offline."""
        tk = (toolkit_or_tool or "").split(".")[0].split(":")[0].lower()
        if tk in self._unauthorized:
            return ("pending", f"https://connect.arcade.test/{tk}?user={user_id}")
        return ("completed", "")

    def grant(self, toolkit: str) -> None:
        """Test helper: simulate the user COMPLETING the connect flow for a toolkit (the OAuth grant lands)."""
        self._unauthorized.discard((toolkit or "").lower())

    # ---- Protocol ----
    def execute(self, *, toolkit: str, action: str, params: dict, user_id: str) -> ExecResult:
        params = params or {}
        if (toolkit or "").lower() in self._unauthorized:
            return ExecResult(ok=False, error=f"authorization_required: connect {toolkit} to continue")
        entry = _ACTIONS.get(_key(toolkit, action))
        if entry is None:
            return ExecResult(ok=False, error=f"unknown action {toolkit}:{action}")
        kind, resource = entry
        if kind == "read":
            return ExecResult(ok=True, data=self.surface(user_id, resource))
        # write
        if _key(toolkit, action) in self._drop:
            # Accepted by the "provider" but the effect never lands — the fabricated-completion case.
            return ExecResult(ok=True, data={"id": None, "dropped": True})
        fields = {f: params[f] for f in params}  # store everything the caller passed
        # Faithful to the LIVE read-back: an effect identified by a RECORD ID (trash/label by message id)
        # appears in the surface keyed by that id, with an `id` field — so the gate's exact-id match
        # (effects.has_expected_effect) behaves the same offline and live. Other writes key by a synthetic
        # id (their fingerprint is to/subject/text, matched whole-field/token — id is irrelevant).
        rid = params.get("email_id") or params.get("message_id") or params.get("id")
        if rid:
            iid = str(rid)
            fields.setdefault("id", iid)
        else:
            iid = self._next_id()
        self._resource(user_id, resource)[iid] = fields
        return ExecResult(ok=True, data={"id": iid})

    def snapshot(self, *, toolkit: str, action: str, params: dict, user_id: str) -> dict | None:
        if (toolkit or "").lower() in self._no_readback:
            return None
        if _key(toolkit, action) in _UNVERIFIABLE_ACTIONS:
            return None     # intrinsically unverifiable (e.g. a GitHub comment) -> ask the owner
        entry = _ACTIONS.get(_key(toolkit, action))
        if entry is None:
            return None
        _kind, resource = entry
        return self.surface(user_id, resource)

    def fingerprint(self, *, toolkit: str, action: str, params: dict) -> dict | None:
        return fingerprint_for(toolkit, action, params or {})

    def created_key(self, *, toolkit: str, action: str, data) -> str | None:
        # the fake stores a created item under the id it returned in execute()'s data ({"id": iid}); a
        # dropped create returns {"id": None} -> "" (the broker then makes verification unmatchable).
        if _key(toolkit, action) not in _ID_BOUND_CREATE_ACTIONS:
            return None
        return str((data if isinstance(data, dict) else {}).get("id") or "")


_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def _extract_email(s) -> str:
    """The bare email from a possibly display-name-wrapped header (`Asa <a@x.com>` -> `a@x.com`), so
    the expected-effect fingerprint (which requires whole-field email equality) matches the read-back."""
    m = _EMAIL_RE.search(str(s or ""))
    return m.group(0).lower() if m else str(s or "")


# --- read-back PARSERS: turn an Arcade read-back response into {item_id: {field: value}} (the shape
# flowers.effects.snapshot_diff / has_expected_effect consume). One per surface, so verification is
# effect-kind-agnostic and a new toolkit only needs a parser + a row in _ARCADE_TOOLS below.

def _parse_emails(val) -> dict[str, dict]:
    emails = val.get("emails", []) if isinstance(val, dict) else []
    out: dict[str, dict] = {}
    for i, e in enumerate(emails):
        if not isinstance(e, dict):     # a provider returning a bare string/null item must not crash
            continue
        # a real Gmail id / header id, else a clearly-synthetic non-collidable fallback (never a bare
        # index like "3", which — being opaque-id-shaped — could equal an attacker-chosen fingerprint).
        eid = str(e.get("id") or e.get("header_message_id") or f"_noid_{i}")
        raw_from = e.get("from_", e.get("from", ""))
        # `from` is the BARE email (so an await/monitor `match` on a sender address — which the gate matches
        # as a WHOLE field value — works against a display-name header like "Asa <a@x.com>"); keep the raw.
        # `id` is exposed as a FIELD VALUE (not just the dict key) so trash/label — whose only identity in
        # the action params is the message id — can fingerprint-verify (the id must match an ADDED item's
        # field value, per flowers.effects). Additive: it never makes a recipient/subject match spuriously.
        out[eid] = {"id": eid, "subject": e.get("subject", ""), "to": _extract_email(e.get("to", "")),
                    "to_raw": e.get("to", ""), "from": _extract_email(raw_from), "from_raw": raw_from,
                    # retain the BODY (the live Arcade read-back carries body/snippet) so the await loop can
                    # READ what a reply said (flowers.replies), not just confirm it arrived. The gate still
                    # matches on whole field VALUES (subject/from); body is extra, never a match key by default.
                    "body": e.get("body", "") or e.get("snippet", "") or "", "snippet": e.get("snippet", "")}
    return out


def _as_label_list(p: dict) -> list:
    """The labels to add, tolerant of shape: a list passthrough, or a single `label`/`labels` string."""
    v = p.get("labels_to_add") or p.get("label") or p.get("labels") or []
    if isinstance(v, str):
        return [v] if v else []
    return [str(x) for x in v if x] if isinstance(v, (list, tuple)) else []


def _first_label(p: dict) -> str:
    labels = _as_label_list(p)
    return labels[0] if labels else ""


def _msg_id(p: dict) -> str:
    return p.get("email_id") or p.get("message_id") or p.get("id") or ""


def _parse_events(val) -> dict[str, dict]:
    if isinstance(val, dict):
        events = val.get("events") or val.get("items") or []
    else:
        events = val if isinstance(val, list) else []
    out: dict[str, dict] = {}
    for i, ev in enumerate(events):
        if not isinstance(ev, dict):
            continue
        eid = str(ev.get("id") or ev.get("event_id") or i)
        title = ev.get("summary") or ev.get("title") or ""
        start = ev.get("start")
        start = start.get("dateTime", start.get("date", "")) if isinstance(start, dict) else str(start or "")
        out[eid] = {"summary": title, "title": title, "start": start}
    return out


# Canonical (toolkit, ACTION) -> how to call it on Arcade, how to read it back, and how to PARSE that
# read-back. The read-back is an INDEPENDENT query (a different Arcade tool) so the gate never verifies
# an effect through the same call that performed it. A write with no reliable read-back sets
# ``readback: None`` -> snapshot returns None -> the gate routes it to unverifiable (ask the owner).
# NOTE: the non-Gmail Arcade tool/field names are INDICATIVE (Gmail is the live-verified one) — confirm
# them against the live Arcade catalog when that toolkit is first connected, same as the model slugs.
_ARCADE_TOOLS: dict[tuple[str, str], dict] = {
    ("gmail", "GMAIL_SEND_EMAIL"): {
        "tool": "Gmail.SendEmail",
        "kind": "write",
        "to_input": lambda p: {
            "recipient": p.get("to") or p.get("recipient") or "",
            "subject": p.get("subject", "") or "",
            "body": p.get("body") or p.get("subject", "") or "",
        },
        # read the SENT label filtered to this recipient -> the new message shows up as an added item
        "readback": lambda p: ("Gmail.ListEmailsByHeader",
                               {"recipient": p.get("to") or p.get("recipient") or "",
                                "label": "SENT", "max_results": 25}),
        "parse": _parse_emails,
    },
    ("gmail", "GMAIL_FETCH_EMAILS"): {
        "tool": "Gmail.ListEmails", "kind": "read",
        "to_input": lambda p: {"n_emails": int(p.get("n_emails", 10) or 10)},
        "readback": None,
    },
    ("gmail", "GMAIL_SEARCH_EMAILS"): {
        # server-side filtered fetch -> "summarize my unread from boss" / "find the thread about X".
        # A READ (AUTO): execute returns the matching emails; the await/monitor loop can also snapshot it.
        "tool": "Gmail.ListEmailsByHeader", "kind": "read",
        "to_input": lambda p: {k: v for k, v in {
            "sender": p.get("sender") or p.get("from") or "",
            "recipient": p.get("recipient") or p.get("to") or "",
            "subject": p.get("subject") or "",
            "body": p.get("body") or p.get("query") or "",
            "date_range": p.get("date_range") or "",
            "label": p.get("label") or "",
            "max_results": int(p.get("max_results", 25) or 25),
        }.items() if v not in (None, "", [])},
        "readback": None,
    },
    ("gmail", "GMAIL_TRASH_MESSAGE"): {
        "tool": "Gmail.TrashEmail",
        "kind": "write",
        "to_input": lambda p: {"email_id": _msg_id(p)},
        # read the TRASH surface back -> the just-trashed message shows up as an ADDED item; the gate
        # fingerprints on the message id (the only identity in the params) against the `id` field, by
        # EXACT id-equality (see effects.has_expected_effect) so a concurrent/injected item can't spoof it.
        # Trash is REVERSIBLE (restorable for ~30d) -> ASK, never the permanent-delete NEVER floor.
        # KNOWN LIMITATION: this lists the TRASH surface capped at 100 (Gmail's max) — trashing a message
        # whose RECEIVED date ranks outside the 100 newest in Trash reads back as absent -> the gate
        # FAILS CLOSED (over-asks the owner), never falsely verifies. Robust per-id read-back awaits a
        # get-message-by-id Arcade tool (only GetThread-by-thread_id exists today).
        "readback": lambda p: ("Gmail.ListEmailsByHeader", {"label": "TRASH", "max_results": 100}),
        "parse": _parse_emails,
    },
    ("gmail", "GMAIL_ADD_LABEL"): {
        "tool": "Gmail.ChangeEmailLabels",
        "kind": "write",
        # v1 is ADD-only (labels_to_remove empty): adding a label is an ADDED-item effect the gate can
        # verify; a label REMOVE would be a removal -> not added-item verifiable (would route to owner).
        "to_input": lambda p: {"email_id": _msg_id(p),
                               "labels_to_add": _as_label_list(p),
                               "labels_to_remove": []},
        # read the label's surface back -> the newly-labeled message shows up as an ADDED item (by id,
        # whole-field equality). Same windowing limitation as trash (capped at 100, fails closed).
        "readback": lambda p: ("Gmail.ListEmailsByHeader",
                               {"label": _first_label(p), "max_results": 100}),
        "parse": _parse_emails,
    },
    ("googlecalendar", "GOOGLECALENDAR_CREATE_EVENT"): {
        "tool": "GoogleCalendar.CreateEvent",
        "kind": "write",
        "to_input": lambda p: {k: v for k, v in {
            "summary": p.get("summary") or p.get("title") or "",
            "start_datetime": p.get("start") or p.get("start_datetime") or "",
            "end_datetime": p.get("end") or p.get("end_datetime") or "",
            "calendar_id": p.get("calendar_id") or "primary",
            "description": p.get("description") or "",
            "location": p.get("location") or "",
            "attendee_emails": p.get("attendees") or p.get("attendee_emails") or [],
        }.items() if v not in (None, "", [])},
        # read the events surface back -> the just-created event shows up as an added item matching summary.
        # The live GoogleCalendar.ListEvents@3.4.0 requires BOTH min_end_datetime AND max_start_datetime
        # (confirmed live 2026-06-23). A far-past min_end + far-future max_start = a window over essentially
        # all events (ends after 1970 AND starts before 2100), so the just-created event is always in-window.
        "readback": lambda p: ("GoogleCalendar.ListEvents",
                               {"max_results": 50, "calendar_id": p.get("calendar_id") or "primary",
                                "min_end_datetime": p.get("min_end_datetime") or "1970-01-01T00:00:00Z",
                                "max_start_datetime": p.get("max_start_datetime") or "2100-01-01T00:00:00Z"}),
        "parse": _parse_events,
    },
    ("googlecalendar", "GOOGLECALENDAR_LIST_EVENTS"): {
        "tool": "GoogleCalendar.ListEvents", "kind": "read",
        # both datetime bounds are REQUIRED by the live tool; default to a wide all-events window unless
        # the caller narrows it (e.g. "events this week" -> a tighter min_end/max_start).
        "to_input": lambda p: {"max_results": int(p.get("max_results", 25) or 25),
                               "calendar_id": p.get("calendar_id") or "primary",
                               "min_end_datetime": p.get("min_end_datetime") or "1970-01-01T00:00:00Z",
                               "max_start_datetime": p.get("max_start_datetime") or "2100-01-01T00:00:00Z"},
        "readback": None,
    },
}


class ArcadeIntegrations:
    """Live adapter (Arcade.dev) on the ``arcadepy`` SDK. The model never sees the OAuth token — Arcade
    injects it. Gated by ``ARCADE_API_KEY`` + the offline switch. Accepts an injected ``client`` so the
    canonical->Arcade mapping and the read-back logic are unit-testable offline with a fake SDK.

    The ``user_id`` is the local-user identity (``flowers.runtime.local_user()`` — flowers is a
    single-user tool). Arcade "dev mode" ("Arcade.dev users only" verification) REJECTS any user_id
    that isn't the signed-in Arcade account (``user_mismatch``), so dev-mode owners must set
    ``FLOWERS_USER_ID`` to their Arcade account email; the Google account granted during consent is
    independent and becomes the connected mailbox.
    """

    def __init__(self, *, client=None):
        self._client = client

    def available(self) -> bool:
        if self._client is not None:
            return True
        return runtime.adapter_available(key_env="ARCADE_API_KEY")

    def _arcade(self):
        if self._client is not None:
            return self._client
        if not runtime.adapter_available(key_env="ARCADE_API_KEY"):
            raise RuntimeError("ArcadeIntegrations unavailable (offline or no ARCADE_API_KEY)")
        from arcadepy import Arcade
        self._client = Arcade(api_key=runtime.env("ARCADE_API_KEY"))
        return self._client

    @staticmethod
    def _value(resp):
        out = getattr(resp, "output", None)
        val = getattr(out, "value", None)
        return val if val is not None else out

    @staticmethod
    def _error(resp) -> str | None:
        """Arcade can report ``success=True`` while the WRAPPED tool failed: ``output.error`` is set (e.g.
        a GitHub 403, a retryable runtime error). That is NOT a success — surface it as the error string so
        a failed write is treated as failed (the gate would refuse it anyway via read-back, but a buried
        error must not read as ok). Returns the message, or None when the call genuinely succeeded."""
        out = getattr(resp, "output", None)
        err = getattr(out, "error", None) if out is not None else None
        if err is None and getattr(resp, "success", True) is False:
            return "tool reported failure"
        if err is None:
            return None
        return getattr(err, "message", None) or str(err)

    def _exec_raw(self, tool_name: str, inp: dict, user_id: str):
        return self._arcade().tools.execute(tool_name=tool_name, input=inp, user_id=user_id)

    # ---- Protocol ----
    def execute(self, *, toolkit: str, action: str, params: dict, user_id: str) -> ExecResult:
        entry = _ARCADE_TOOLS.get(((toolkit or "").lower(), (action or "").upper()))
        if entry is None:
            return ExecResult(ok=False, error=f"unknown action {toolkit}:{action}")
        try:
            resp = self._exec_raw(entry["tool"], entry["to_input"](params or {}), user_id)
        except Exception as exc:  # noqa: BLE001 - a backend failure is a result, not a crash
            msg = f"{type(exc).__name__}: {exc}"
            if "authoriz" in msg.lower() or "permission" in msg.lower():
                return ExecResult(ok=False, error=f"authorization_required: {msg}")
            return ExecResult(ok=False, error=msg)
        err = self._error(resp)
        if err is not None:
            low = err.lower()
            if "authoriz" in low or "permission" in low or "forbidden" in low or "403" in low:
                return ExecResult(ok=False, error=f"authorization_required: {err}")
            return ExecResult(ok=False, error=err)
        return ExecResult(ok=True, data=self._value(resp), error=None)

    def snapshot(self, *, toolkit: str, action: str, params: dict, user_id: str) -> dict | None:
        entry = _ARCADE_TOOLS.get(((toolkit or "").lower(), (action or "").upper()))
        if entry is None:
            return None
        # A WRITE action is read back via its INDEPENDENT `readback` tool (the gate never verifies through
        # the same call that performed the effect). A READ action (e.g. inbox fetch) IS its own surface —
        # execute its own tool and parse. The operator's await/monitor loop reads the inbox via
        # snapshot(GMAIL_FETCH_EMAILS), so a read MUST return its parsed surface, not None.
        if entry.get("readback"):
            tool_name, inp = entry["readback"](params or {})
        elif entry.get("kind") == "read":
            tool_name, inp = entry["tool"], entry["to_input"](params or {})
        else:
            return None              # a write with no independent read-back -> unverifiable (ask the owner)
        parse = entry.get("parse") or _parse_emails
        try:
            resp = self._exec_raw(tool_name, inp, user_id)
            if self._error(resp) is not None:
                return None     # the read-back call itself failed -> no reliable surface -> ask the owner
            return parse(self._value(resp) or {})
        except Exception:
            return None     # a backend OR parser failure -> no reliable read-back -> unverifiable (ask owner)

    def fingerprint(self, *, toolkit: str, action: str, params: dict) -> dict | None:
        return fingerprint_for(toolkit, action, params or {})

    def created_key(self, *, toolkit: str, action: str, data) -> str | None:
        return created_record_id(toolkit, action, data)

    def authorize(self, toolkit_or_tool: str, user_id: str) -> tuple[str, str]:
        """Start (or check) the OAuth connect flow for a toolkit/tool for ``user_id`` via Arcade, returning
        ``(status, url)``: ``status == 'completed'`` (url='') when the scope is already granted, else a
        pending/started status with the consent URL to send the user. Wraps ``client.tools.authorize``. A
        backend failure returns ``('error', '')`` — never raises, so an auth probe can't crash a run/tick.
        Granting one representative write tool consents the whole toolkit scope (e.g. Gmail.SendEmail ->
        gmail.send)."""
        tool_name = _auth_tool_for(toolkit_or_tool)
        try:
            client = self._arcade()
            resp = client.tools.authorize(tool_name=tool_name, user_id=user_id)
        except Exception:
            return ("error", "")
        status = str(getattr(resp, "status", None) or "pending")
        url = str(getattr(resp, "url", None) or "")
        return (status, url)
