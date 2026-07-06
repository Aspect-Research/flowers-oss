"""The real ArcadeIntegrations adapter — canonical->Arcade mapping + the independent Sent read-back,
verified offline with a fake arcadepy SDK client (no network). The headline tests prove the FULL
production path: a real send is verified via read-back and accepted by the gate; a non-landing send is
refused; an auth failure surfaces.
"""

from __future__ import annotations

from flowers import trustgate as g
from flowers.broker import Broker
from flowers.seams.integrations import ArcadeIntegrations, _extract_email


# --- a minimal arcadepy-SDK-shaped fake (client.tools.execute(tool_name, input, user_id)) ---
class _Out:
    def __init__(self, value):
        self.value = value


class _Resp:
    def __init__(self, success, value):
        self.success = success
        self.output = _Out(value)


class _Tools:
    def __init__(self, parent):
        self._p = parent

    def execute(self, *, tool_name, input, user_id):  # noqa: A002 - mirror the arcadepy signature
        return self._p._execute(tool_name, input, user_id)


class FakeArcade:
    def __init__(self, *, drop=False, raise_auth=False):
        self.tools = _Tools(self)
        self._sent: dict[str, list] = {}
        self._events: dict[str, list] = {}
        self.drop = drop
        self.raise_auth = raise_auth
        self.calls: list = []

    def _execute(self, tool_name, inp, user_id):
        self.calls.append((tool_name, inp, user_id))
        if self.raise_auth:
            raise PermissionError(f"authorization required to call {tool_name}")
        if tool_name == "Gmail.SendEmail":
            if not self.drop:
                box = self._sent.setdefault(user_id, [])
                box.append({"id": f"m{len(box)+1}",
                            "to": f"Venue Team <{inp.get('recipient')}>",   # display-name wrapped on purpose
                            "subject": inp.get("subject"), "from_": user_id})
            return _Resp(True, {"id": "sent-123"})
        if tool_name == "Gmail.ListEmailsByHeader":
            rec = (inp.get("recipient") or "").lower()
            ems = [e for e in self._sent.get(user_id, []) if not rec or rec in (e.get("to") or "").lower()]
            return _Resp(True, {"emails": ems})
        if tool_name == "Gmail.ListEmails":
            return _Resp(True, {"emails": self._sent.get(user_id, [])})
        if tool_name == "GoogleCalendar.CreateEvent":
            # the create RESPONSE id and the ListEvents read-back id are the SAME real event id (live
            # Google behaviour), so the broker can bind verification to the exact created event.
            eid = f"evt-{len(self._events.get(user_id, [])) + 1}"
            if not self.drop:
                self._events.setdefault(user_id, []).append(
                    {"id": eid, "summary": inp.get("summary"),
                     "start": {"dateTime": inp.get("start_datetime", "")}})
            return _Resp(True, {"event": {"id": eid}})
        if tool_name == "GoogleCalendar.ListEvents":
            return _Resp(True, {"events": self._events.get(user_id, [])})
        return _Resp(False, {"error": "unknown tool"})


def _adapter(**kw):
    return ArcadeIntegrations(client=FakeArcade(**kw))


def test_extract_email_handles_display_name():
    assert _extract_email("Asa <a@x.com>") == "a@x.com"
    assert _extract_email("bob@acme.com") == "bob@acme.com"


def test_available_with_injected_client_true_offline_false():
    assert _adapter().available() is True
    assert ArcadeIntegrations().available() is False   # offline, no client


def test_execute_maps_canonical_to_arcade_tool():
    fake = FakeArcade()
    a = ArcadeIntegrations(client=fake)
    res = a.execute(toolkit="gmail", action="GMAIL_SEND_EMAIL",
                    params={"to": "venue@example.com", "subject": "Inquiry", "body": "hi"}, user_id="u1")
    assert res.ok is True
    tool, inp, uid = fake.calls[0]
    assert tool == "Gmail.SendEmail" and uid == "u1"
    assert inp["recipient"] == "venue@example.com" and inp["subject"] == "Inquiry" and inp["body"] == "hi"


def test_snapshot_reads_back_sent_with_extracted_email():
    fake = FakeArcade()
    a = ArcadeIntegrations(client=fake)
    a.execute(toolkit="gmail", action="GMAIL_SEND_EMAIL",
              params={"to": "venue@example.com", "subject": "Inquiry"}, user_id="u1")
    snap = a.snapshot(toolkit="gmail", action="GMAIL_SEND_EMAIL",
                      params={"to": "venue@example.com"}, user_id="u1")
    assert snap and len(snap) == 1
    item = next(iter(snap.values()))
    assert item["to"] == "venue@example.com" and item["subject"] == "Inquiry"   # extracted from display-name


def test_unknown_action_fails():
    assert _adapter().execute(toolkit="frob", action="FROB", params={}, user_id="u1").ok is False


def test_auth_failure_surfaces():
    res = _adapter(raise_auth=True).execute(toolkit="gmail", action="GMAIL_SEND_EMAIL",
                                            params={"to": "x@y.com", "subject": "s"}, user_id="u1")
    assert res.ok is False and "authorization_required" in (res.error or "")


# --- the headline: full production path through the broker + gate ---
def _gate(effect):
    unver, unverifiable = g.classify_effects([effect.as_gate_dict()], claimed_done=True)
    return g.gate_verdict(claimed_done=True, ok=True, stale_files=[], gate_breaking=[],
                          unverified_external=unver, unverifiable_external=unverifiable)


def test_verified_send_accepted_through_real_adapter_path():
    b = Broker(integrations=_adapter(), run_id="r")
    res = b.call_integration(toolkit="gmail", action="GMAIL_SEND_EMAIL",
                             params={"to": "venue@example.com", "subject": "Venue inquiry"},
                             user_id="u1", authorized=True)
    assert res.status == "ok" and res.effect.expected_present is True
    accept, _ = _gate(res.effect)
    assert accept is True


def test_nonlanding_send_refused_through_real_adapter_path():
    b = Broker(integrations=_adapter(drop=True), run_id="r")   # send "succeeds" but never appears in Sent
    res = b.call_integration(toolkit="gmail", action="GMAIL_SEND_EMAIL",
                             params={"to": "venue@example.com", "subject": "Venue inquiry"},
                             user_id="u1", authorized=True)
    assert res.effect.expected_present is False
    accept, reason = _gate(res.effect)
    assert accept is False and "not reflected" in reason


# --- capability breadth — Calendar verifies via independent read-back; Slack is unverifiable ---
def test_calendar_create_verified_through_independent_read_back():
    b = Broker(integrations=_adapter(), run_id="r")
    res = b.call_integration(toolkit="googlecalendar", action="GOOGLECALENDAR_CREATE_EVENT",
                             params={"summary": "Dinner date", "start": "2026-09-10T19:00:00",
                                     "end": "2026-09-10T21:00:00"},
                             user_id="u1", authorized=True)
    assert res.status == "ok" and res.effect.expected_present is True   # the new event appears in ListEvents
    accept, _ = _gate(res.effect)
    assert accept is True


def test_calendar_create_that_does_not_land_is_refused():
    b = Broker(integrations=_adapter(drop=True), run_id="r")   # "created" but never appears in the calendar
    res = b.call_integration(toolkit="googlecalendar", action="GOOGLECALENDAR_CREATE_EVENT",
                             params={"summary": "Dinner date", "start": "2026-09-10T19:00:00"},
                             user_id="u1", authorized=True)
    assert res.effect.expected_present is False
    accept, _ = _gate(res.effect)
    assert accept is False

# --- the connect flow must authorize the READ-BACK tool too (found live: gmail.send alone
# leaves the Sent read-back 403-unauthorized, so the gate can never verify a send) ---

class _AuthFlow:
    def __init__(self, status, url=""):
        self.status = status
        self.url = url


class _AuthTools(_Tools):
    def authorize(self, *, tool_name, user_id):
        granted = self._p.granted
        if tool_name in granted:
            return _AuthFlow("completed")
        return _AuthFlow("pending", f"https://consent.example/{tool_name}?user={user_id}")


class FakeArcadeAuth(FakeArcade):
    def __init__(self, granted=()):
        super().__init__()
        self.granted = set(granted)
        self.tools = _AuthTools(self)


def test_authorize_requires_write_AND_readback_tools():
    # send granted, read-back not -> still pending, and the consent url is for the READ tool
    a = ArcadeIntegrations(client=FakeArcadeAuth(granted={"Gmail.SendEmail"}))
    status, url = a.authorize("gmail", "u1")
    assert status == "pending" and "Gmail.ListEmails" in url
    # nothing granted -> pending on the WRITE tool first
    a2 = ArcadeIntegrations(client=FakeArcadeAuth())
    status2, url2 = a2.authorize("gmail", "u1")
    assert status2 == "pending" and "Gmail.SendEmail" in url2
    # both granted -> completed
    a3 = ArcadeIntegrations(client=FakeArcadeAuth(granted={"Gmail.SendEmail", "Gmail.ListEmails"}))
    assert a3.authorize("gmail", "u1") == ("completed", "")
