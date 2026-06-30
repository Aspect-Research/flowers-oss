"""The ``model`` seam — offline tests.

These never touch the network: FakeModel is scripted, and OpenRouterModel is asserted unavailable
under the suite's offline contract (FLOWERS_FORCE_OFFLINE=1 + blanked keys, see conftest.py).
"""

from __future__ import annotations

import pytest

from flowers.seams.interfaces import ModelClient, ModelResponse
from flowers.seams.model import DEFAULT_ROLE_CONFIG, FakeModel, OpenRouterModel
from flowers.types import ToolCall

# --------------------------------------------------------------------------- FakeModel

def test_fake_model_is_a_model_client_and_available():
    m = FakeModel([ModelResponse(content="hi")])
    assert isinstance(m, ModelClient)
    assert m.available() is True


def test_fake_model_returns_scripted_text():
    m = FakeModel([ModelResponse(content="the answer is 42", finish_reason="stop")])
    resp = m.complete([{"role": "user", "content": "q"}])
    assert resp.content == "the answer is 42"
    assert resp.finish_reason == "stop"
    assert resp.cost_usd == 0.0


def test_fake_model_returns_tool_calls():
    m = FakeModel([ModelResponse(
        tool_calls=[ToolCall(name="web_search", args={"query": "venues"})],
        finish_reason="tool_calls",
    )])
    resp = m.complete([{"role": "user", "content": "find venues"}], role="executor")
    assert resp.finish_reason == "tool_calls"
    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0].name == "web_search"
    assert resp.tool_calls[0].args == {"query": "venues"}
    assert resp.cost_usd == 0.0


def test_fake_model_scripts_responses_in_order():
    m = FakeModel([
        ModelResponse(content="first"),
        ModelResponse(content="second"),
    ])
    assert m.complete([]).content == "first"
    assert m.complete([]).content == "second"


def test_fake_model_exhausted_script_raises():
    m = FakeModel([ModelResponse(content="only one")])
    m.complete([])
    with pytest.raises(RuntimeError, match="exhausted"):
        m.complete([])


def test_fake_model_callable_form():
    def on_complete(messages, tools, role):
        return ModelResponse(content=f"role={role}", finish_reason="stop")

    m = FakeModel(on_complete=on_complete)
    resp = m.complete([{"role": "user", "content": "x"}], role="planner")
    assert resp.content == "role=planner"
    assert resp.cost_usd == 0.0


def test_fake_model_callable_passed_positionally():
    m = FakeModel(lambda messages, tools, role: ModelResponse(content="ok"))
    assert m.complete([]).content == "ok"


def test_fake_model_records_calls():
    m = FakeModel([ModelResponse(content="x")])
    m.complete([{"role": "user", "content": "hi"}], role="planner", max_tokens=10)
    assert m.calls[0]["role"] == "planner"
    assert m.calls[0]["max_tokens"] == 10


# --------------------------------------------------------------------------- OpenRouterModel

def test_openrouter_is_a_model_client():
    assert isinstance(OpenRouterModel(), ModelClient)


def test_openrouter_unavailable_offline():
    # Under the suite's offline contract, the live adapter must report unavailable.
    assert OpenRouterModel().available() is False


def test_openrouter_complete_refuses_when_unavailable():
    m = OpenRouterModel()
    with pytest.raises(RuntimeError, match="unavailable"):
        m.complete([{"role": "user", "content": "hi"}])


def test_role_config_resolves_planner_and_executor():
    m = OpenRouterModel()
    assert m.role_config["planner"]["model"] == "z-ai/glm-5.2"
    assert m.role_config["planner"]["reasoning"] == "high"
    assert m.role_config["executor"]["model"] == "deepseek/deepseek-v4-pro"
    assert m.role_config["executor"]["reasoning"] == "low"
    # unknown role falls back to executor config
    assert m._resolve("nope") == m.role_config["executor"]


def test_role_config_is_overridable():
    custom = {"planner": {"model": "custom/model", "reasoning": "medium"}}
    m = OpenRouterModel(role_config=custom)
    assert m.role_config["planner"]["model"] == "custom/model"
    assert m.role_config["planner"]["reasoning"] == "medium"
    # constructor copies the top-level dict so adding a role to the original does not leak in
    custom["executor"] = {"model": "leak", "reasoning": "low"}
    assert "executor" not in m.role_config


def test_partial_role_config_still_resolves_to_a_real_model():
    # A partial override that omits both 'executor' and 'executor_hard' must NOT resolve to {} (which would
    # post model:None and fail late at the API): _resolve falls back to the DEFAULT executor config.
    m = OpenRouterModel(role_config={"planner": {"model": "custom/model", "reasoning": "medium"}})
    assert m._resolve("executor_hard").get("model")        # a real model, never None
    assert m._resolve("executor").get("model")
    assert m._resolve("anything").get("model")


def test_default_role_config_constant():
    assert set(DEFAULT_ROLE_CONFIG) == {"planner", "executor", "executor_hard", "verifier"}
    # "executor_hard" is the hard-rung escalation model (lever 1): it MUST exist with a model + high
    # reasoning, else operator escalation silently no-ops (an unknown role falls back to the executor cfg).
    assert DEFAULT_ROLE_CONFIG["executor_hard"]["model"]
    assert DEFAULT_ROLE_CONFIG["executor_hard"]["reasoning"] == "high"


# --------------------------------------------------------------------------- response parsing
# Exercises the pure parser directly (no network): OpenAI-shape JSON -> ModelResponse.

def test_parse_text_response():
    payload = {
        "choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}],
        "usage": {"cost": 0.0012},
    }
    resp = OpenRouterModel._parse(payload)
    assert resp.content == "hello"
    assert resp.finish_reason == "stop"
    assert resp.cost_usd == 0.0012
    assert resp.tool_calls == []


def test_parse_tool_calls_decodes_arguments():
    payload = {
        "choices": [{
            "message": {
                "content": "",
                "tool_calls": [{
                    "id": "call_abc",
                    "function": {"name": "web_search", "arguments": '{"query": "venues"}'},
                }],
            },
            "finish_reason": "tool_calls",
        }],
        "usage": {"cost": 0.02},
    }
    resp = OpenRouterModel._parse(payload)
    assert resp.finish_reason == "tool_calls"
    assert len(resp.tool_calls) == 1
    tc = resp.tool_calls[0]
    assert isinstance(tc, ToolCall)
    assert tc.name == "web_search"
    assert tc.args == {"query": "venues"}
    assert tc.id == "call_abc"
    assert resp.cost_usd == 0.02


def test_parse_falls_back_to_zero_cost_without_usage():
    payload = {"choices": [{"message": {"content": "x"}, "finish_reason": "stop"}]}
    assert OpenRouterModel._parse(payload).cost_usd == 0.0


def test_parse_infers_tool_calls_finish_reason():
    payload = {
        "choices": [{
            "message": {"tool_calls": [{"function": {"name": "f", "arguments": "{}"}}]},
            "finish_reason": "stop",
        }],
    }
    resp = OpenRouterModel._parse(payload)
    assert resp.finish_reason == "tool_calls"
    assert resp.tool_calls[0].args == {}
