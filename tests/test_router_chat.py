"""Router (classify) + chat lane + reply-style persona — the flowers-side hooks for a chat front end."""

import json

import pytest
from _harness import build
from starlette.testclient import TestClient

from flowers.channels.web import WebChannel, create_app
from flowers.controlplane import _is_ack
from flowers.seams.model import FakeModel, ModelResponse

RUNS = [
    {"n": 1, "goal": "clone the flowers-oss repo", "status": "running", "awaiting": None},
    {"n": 2, "goal": "send an email to mom", "status": "awaiting_approval", "awaiting": "approval"},
]


def _cp_with(reply):
    """A control plane whose model echoes `reply` (a dict → JSON, or a str) and records the last call."""
    seen = {}

    def fn(messages, tools, role):
        seen["messages"] = messages
        seen["role"] = role
        content = json.dumps(reply) if isinstance(reply, dict) else str(reply)
        return ModelResponse(content=content)

    return build(model=FakeModel(on_complete=fn))["cp"], seen


# ── classify ────────────────────────────────────────────────────────────────
def test_classify_task():
    cp, _ = _cp_with({"intent": "task", "n": None})
    assert cp.classify(text="email my landlord", runs=[])["intent"] == "task"


def test_classify_reply_needs_valid_task_number():
    cp, _ = _cp_with({"intent": "reply", "n": 2})
    out = cp.classify(text="yes", runs=RUNS)
    assert out == {"intent": "reply", "n": 2}


def test_classify_reply_to_unknown_task_falls_back_to_task():
    # The model says reply→#9 but there is no #9: a reply that names no real task is unsafe to route,
    # so it defaults to a new (gated) task.
    cp, _ = _cp_with({"intent": "reply", "n": 9})
    assert cp.classify(text="whatever", runs=RUNS) == {"intent": "task", "n": None}


def test_classify_chat():
    cp, _ = _cp_with({"intent": "chat", "n": None})
    assert cp.classify(text="how's it going?", runs=RUNS)["intent"] == "chat"


def test_classify_defaults_to_task_on_garbage():
    cp, _ = _cp_with("not json at all")
    assert cp.classify(text="hi", runs=RUNS) == {"intent": "task", "n": None}


# ── ack routing (P1.2) ────────────────────────────────────────────────────────
def test_classify_ack_reply_with_bad_ordinal_downgrades_to_chat():
    # The model says reply→#9 but there is no #9. A bare ack ("thanks") with nowhere to land must NOT
    # spawn a spurious task — it downgrades to chat (never a new run).
    cp, _ = _cp_with({"intent": "reply", "n": 9})
    assert cp.classify(text="thanks", runs=RUNS) == {"intent": "chat", "n": None}


def test_classify_emoji_ack_reply_with_bad_ordinal_downgrades_to_chat():
    # A bare reaction emoji is a pure ack: reply→bad-ordinal downgrades to chat, not task.
    cp, _ = _cp_with({"intent": "reply", "n": 9})
    assert cp.classify(text="👍", runs=RUNS) == {"intent": "chat", "n": None}


def test_classify_actionable_reply_with_bad_ordinal_still_task():
    # An ACTIONABLE reply with a bad ordinal is NOT an ack — it keeps the fail-toward-doing default (task),
    # so a real request with nowhere to land still becomes a gated run.
    cp, _ = _cp_with({"intent": "reply", "n": 9})
    assert cp.classify(text="email my landlord instead", runs=RUNS) == {"intent": "task", "n": None}


def test_classify_ack_on_classifier_error_is_chat():
    # The hard default on classifier ERROR is task — EXCEPT a deterministically ack-shaped text, which
    # must never spawn a run even when the classifier is down.
    cp, _ = _cp_with("not json at all")
    assert cp.classify(text="ok", runs=RUNS) == {"intent": "chat", "n": None}
    assert cp.classify(text="got it", runs=RUNS) == {"intent": "chat", "n": None}
    assert cp.classify(text="👍", runs=RUNS) == {"intent": "chat", "n": None}


def test_classify_actionable_on_classifier_error_stays_task():
    # A real request on classifier error stays task (fail-toward-doing) — the ack exception is narrow.
    cp, _ = _cp_with("not json at all")
    assert cp.classify(text="send an email to mom", runs=RUNS) == {"intent": "task", "n": None}


def test_classify_valid_reply_ordinal_unaffected_by_ack_rule():
    # "yes" to a real awaiting task stays a reply (guard the existing SAFETY behavior): the ack downgrade
    # only fires on an INVALID ordinal, never on a reply the model correctly bound to an active task.
    cp, _ = _cp_with({"intent": "reply", "n": 2})
    assert cp.classify(text="yes", runs=RUNS) == {"intent": "reply", "n": 2}


# An ACK-PREFIXED real command ("ok cancel it") is NOT an ack — the leading "ok"/"thanks" is politeness on
# a genuine request, and it must never be downgraded to chat (that would DROP the command).
_ACK_PREFIXED_COMMANDS = ["ok cancel it", "ok send it", "thanks, now send the follow-up"]


@pytest.mark.parametrize("text", _ACK_PREFIXED_COMMANDS)
def test_ack_prefixed_command_is_not_an_ack(text):
    # Directly: the ack detector rejects a command that merely STARTS with an ack word (it has a real verb
    # + object beyond the ack set), so the downgrade never fires on it.
    assert _is_ack(text) is False


@pytest.mark.parametrize("text", _ACK_PREFIXED_COMMANDS)
def test_classify_ack_prefixed_command_with_bad_ordinal_stays_task(text):
    # Via the downgrade path: the model says reply→#9 (no such task). An ack-prefixed REAL command is not an
    # ack, so it keeps the fail-toward-doing default (task) — the command becomes a gated run, never chat.
    cp, _ = _cp_with({"intent": "reply", "n": 9})
    assert cp.classify(text=text, runs=RUNS) == {"intent": "task", "n": None}


def test_classify_empty_text_is_task_without_a_model_call():
    cp, seen = _cp_with({"intent": "chat"})
    assert cp.classify(text="   ", runs=RUNS) == {"intent": "task", "n": None}
    assert "messages" not in seen  # short-circuited, no model call spent


def test_classify_sees_active_tasks():
    cp, seen = _cp_with({"intent": "chat", "n": None})
    cp.classify(text="what are you up to?", runs=RUNS)
    blob = seen["messages"][1]["content"]
    assert "clone the flowers-oss repo" in blob and "#2" in blob


# ── chat ─────────────────────────────────────────────────────────────────────
def test_chat_returns_text_and_sees_runs():
    cp, seen = _cp_with("still cloning, about halfway")
    out = cp.chat(text="how's the repo thing?", history=[], runs=RUNS)
    assert out == "still cloning, about halfway"
    assert "clone the flowers-oss repo" in seen["messages"][0]["content"]  # run context in system


def test_chat_includes_recent_history_capped():
    cp, seen = _cp_with("ok")
    history = [{"role": "owner", "text": f"m{i}"} for i in range(15)]
    cp.chat(text="now", history=history, runs=[])
    # system + last 10 history + current = 12
    assert len(seen["messages"]) == 12
    assert seen["messages"][-1]["content"] == "now"


def test_chat_empty_response_falls_back():
    cp, _ = _cp_with("")
    assert cp.chat(text="hey", history=[], runs=[]) == "…"


# ── persona (reply_style) ────────────────────────────────────────────────────
def test_reply_style_flows_into_chat_system(monkeypatch):
    monkeypatch.setenv("FLOWERS_REPLY_STYLE", "text like a person, one line")
    cp, seen = _cp_with("yo")
    cp.chat(text="hi", history=[], runs=[])
    assert "text like a person, one line" in seen["messages"][0]["content"]


def test_reply_style_absent_by_default(monkeypatch):
    monkeypatch.delenv("FLOWERS_REPLY_STYLE", raising=False)  # hermetic: ignore any deployed .env value
    cp, seen = _cp_with("yo")
    cp.chat(text="hi", history=[], runs=[])
    assert "STYLE:" not in seen["messages"][0]["content"]


# ── endpoints ────────────────────────────────────────────────────────────────
@pytest.fixture
def client():
    def fn(messages, tools, role):
        # /api/route asks for JSON; /api/chat asks for prose. Distinguish by the system prompt.
        if "route ONE incoming text" in messages[0]["content"]:
            return ModelResponse(content=json.dumps({"intent": "chat", "n": None}))
        return ModelResponse(content="hey there")

    cp = build(model=FakeModel(on_complete=fn))["cp"]
    with TestClient(create_app(cp, WebChannel(cp.store))) as c:
        yield c


def test_route_endpoint(client):
    r = client.post("/api/route", json={"text": "hello", "runs": RUNS})
    assert r.status_code == 200 and r.json()["intent"] == "chat"


def test_chat_endpoint(client):
    r = client.post("/api/chat", json={"text": "hi", "history": [], "runs": []})
    assert r.status_code == 200 and r.json()["reply"] == "hey there"


def test_route_endpoint_rejects_non_object(client):
    assert client.post("/api/route", json=["nope"]).status_code == 400
