"""The ``model`` seam — OpenAI-shape chat/tool-calling, role-keyed, over OpenRouter.

Two implementations of :class:`flowers.seams.interfaces.ModelClient`:

* :class:`FakeModel` — the offline implementation the test suite and the engine use. It returns
  scripted :class:`ModelResponse` objects (either a fixed list popped in order, or a callable that
  computes a response from the incoming messages). It never touches the network and is always
  ``available()``.
* :class:`OpenRouterModel` — the live adapter. OpenRouter is the single gateway; the ``role`` selects
  a model config (planner -> a strong model @ high reasoning; executor -> a cheap, high-throughput
  model). It is gated by :func:`flowers.runtime.adapter_available` so it is unavailable offline or
  without a key, and every live method refuses to run when unavailable.
"""

from __future__ import annotations

import http.client
import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable

from flowers import runtime
from flowers.seams.interfaces import ModelClient, ModelResponse
from flowers.types import ToolCall

# A live completion must survive a transient transport hiccup (a chunked-transfer IncompleteRead, a
# dropped connection, a 429/5xx) instead of crashing the whole run. Retry a few times with a small
# linear backoff; only genuinely non-retryable failures (4xx other than 429) return an error response.
_MAX_ATTEMPTS = 3
_RETRY_BACKOFF_S = 1.5

# --------------------------------------------------------------------------- fake

class FakeModel(ModelClient):
    """Offline, scriptable model client.

    Construct it EITHER with a list of scripted responses (popped FIFO on each ``complete`` call)
    OR with an ``on_complete(messages, tools, role) -> ModelResponse`` callable that computes a
    response dynamically. ``available()`` is always True and ``cost_usd`` is 0.0 (fakes are free).

    Scripting examples::

        # (a) a plain text completion
        m = FakeModel([ModelResponse(content="hello", finish_reason="stop")])

        # (b) a completion that asks for a tool call
        m = FakeModel([ModelResponse(
            tool_calls=[ToolCall(name="web_search", args={"query": "venues"})],
            finish_reason="tool_calls",
        )])

    If the scripted list is exhausted, ``complete`` raises — that is a test bug (an unexpected extra
    model call), never a silent empty response.
    """

    def __init__(
        self,
        scripted: list[ModelResponse] | Callable[..., ModelResponse] | None = None,
        *,
        on_complete: Callable[..., ModelResponse] | None = None,
    ) -> None:
        # Accept a callable passed positionally as a convenience.
        if callable(scripted) and on_complete is None:
            on_complete = scripted  # type: ignore[assignment]
            scripted = None
        self._scripted: list[ModelResponse] = list(scripted or [])
        self._on_complete = on_complete
        self.calls: list[dict] = []   # recorded call kwargs, for test assertions

    def available(self) -> bool:
        return True

    def complete(
        self,
        messages: list[dict],
        *,
        tools: list[dict] | None = None,
        role: str = "executor",
        response_format: dict | None = None,
        max_tokens: int | None = None,
    ) -> ModelResponse:
        self.calls.append({
            "messages": messages, "tools": tools, "role": role,
            "response_format": response_format, "max_tokens": max_tokens,
        })
        if self._on_complete is not None:
            resp = self._on_complete(messages, tools, role)
            resp.cost_usd = 0.0
            return resp
        if not self._scripted:
            raise RuntimeError(
                "FakeModel script exhausted: complete() called with no scripted response left "
                "(this is a test bug — script another ModelResponse or use on_complete)."
            )
        resp = self._scripted.pop(0)
        resp.cost_usd = 0.0
        return resp


# --------------------------------------------------------------------------- live adapter

# Role -> model config. Model slugs are INDICATIVE — confirm them against the live OpenRouter
# catalog at deploy time. Overridable via the OpenRouterModel constructor.
# Executor choice was validated on the real loop: of 5 candidates, deepseek-v4-pro and glm-5.2 were the
# only two to cleanly handle a 2-tool read-then-send sequence (2/2); deepseek-v4-pro won on cost
# (~$0.023 vs $0.034) and glm-5.2 is already the planner.
# "executor_hard" is the STRONGER executor the operator escalates to on hard ladder rungs (rung >=
# operator._HARD_RUNG): more reasoning horsepower exactly when the cheap first approach has already
# failed and been redirected. Reuses the (already-validated) planner slug at high reasoning.
DEFAULT_ROLE_CONFIG: dict[str, dict] = {
    "planner": {"model": "z-ai/glm-5.2", "reasoning": "high"},
    "executor": {"model": "deepseek/deepseek-v4-pro", "reasoning": "low"},
    "executor_hard": {"model": "z-ai/glm-5.2", "reasoning": "high"},
    # The independent completion verifier — a strong model at high reasoning: judging whether a deliverable
    # meets the owner's hard constraints is a careful call. It fires once per finishing-deliverable check
    # (so once per ladder rung / final-step replan), bounded by the run's budget + deadline.
    "verifier": {"model": "z-ai/glm-5.2", "reasoning": "high"},
}

_ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"


class OpenRouterModel(ModelClient):
    """Live OpenRouter adapter (OpenAI-shape chat/tool-calling).

    Gated by :func:`flowers.runtime.adapter_available` on ``OPENROUTER_API_KEY`` — unavailable when
    forced offline or the key is absent. ``complete`` POSTs an OpenAI-shape body to OpenRouter via
    ``urllib`` (stdlib only) and parses the response into a :class:`ModelResponse`, mapping the
    provider's ``tool_calls`` into :class:`flowers.types.ToolCall` and taking ``cost_usd`` from the
    provider's ``usage`` accounting (0.0 if absent). It NEVER calls the network when unavailable.
    """

    KEY_ENV = "OPENROUTER_API_KEY"

    def __init__(
        self,
        *,
        role_config: dict[str, dict] | None = None,
        endpoint: str = _ENDPOINT,
        timeout: float = 120.0,
    ) -> None:
        self.role_config = dict(role_config) if role_config else dict(DEFAULT_ROLE_CONFIG)
        self.endpoint = endpoint
        self.timeout = timeout

    def available(self) -> bool:
        return runtime.adapter_available(key_env=self.KEY_ENV)

    def _resolve(self, role: str) -> dict:
        """Resolve a role to its model config: the role, else the 'executor' universal fallback, else the
        DEFAULT executor config. The final fallback means a PARTIAL role_config override (one that omits
        'executor'/'executor_hard') still posts a real model rather than ``model: None`` failing late at
        the API — a never-empty result by construction."""
        return (self.role_config.get(role) or self.role_config.get("executor")
                or DEFAULT_ROLE_CONFIG["executor"])

    def complete(
        self,
        messages: list[dict],
        *,
        tools: list[dict] | None = None,
        role: str = "executor",
        response_format: dict | None = None,
        max_tokens: int | None = None,
    ) -> ModelResponse:
        if not self.available():
            raise RuntimeError(
                "OpenRouterModel.complete() called while unavailable (offline or missing "
                f"{self.KEY_ENV}). The engine must use a Fake when available() is False."
            )

        cfg = self._resolve(role)
        body: dict = {
            "model": cfg.get("model"),
            "messages": messages,
            # Ask OpenRouter to return usage accounting so cost_usd is authoritative.
            "usage": {"include": True},
        }
        if cfg.get("reasoning"):
            body["reasoning"] = {"effort": cfg["reasoning"]}
        if tools:
            body["tools"] = tools
        if response_format is not None:
            body["response_format"] = response_format
        if max_tokens is not None:
            body["max_tokens"] = max_tokens

        key = runtime.env(self.KEY_ENV)
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            self.endpoint,
            data=data,
            method="POST",
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
        )
        last_err = "unknown"
        for attempt in range(_MAX_ATTEMPTS):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                return self._parse(payload)
            except urllib.error.HTTPError as e:  # pragma: no cover - network path
                # 429 / 5xx are transient (rate-limit / upstream blip) -> retry; other 4xx are terminal.
                if e.code in (429, 500, 502, 503, 504) and attempt < _MAX_ATTEMPTS - 1:
                    last_err = f"HTTP {e.code}"
                    time.sleep(_RETRY_BACKOFF_S * (attempt + 1))
                    continue
                detail = e.read().decode("utf-8", "replace") if hasattr(e, "read") else str(e)
                return ModelResponse(content="", finish_reason="error", cost_usd=0.0,
                                     raw={"error": f"HTTP {e.code}", "detail": detail})
            except (urllib.error.URLError, http.client.HTTPException,
                    ConnectionError, OSError, json.JSONDecodeError) as e:  # pragma: no cover - net path
                # IncompleteRead / dropped connection / timeout / truncated-garbled body: all transient.
                last_err = f"{type(e).__name__}: {e}"
                if attempt < _MAX_ATTEMPTS - 1:
                    time.sleep(_RETRY_BACKOFF_S * (attempt + 1))
                    continue
                return ModelResponse(content="", finish_reason="error", cost_usd=0.0,
                                     raw={"error": last_err})
        return ModelResponse(content="", finish_reason="error", cost_usd=0.0,
                             raw={"error": last_err})  # pragma: no cover - loop always returns above

    @staticmethod
    def _parse(payload: dict) -> ModelResponse:
        """Map an OpenAI-shape chat completion payload into a :class:`ModelResponse`."""
        choices = payload.get("choices") or [{}]
        choice = choices[0] or {}
        message = choice.get("message") or {}
        content = message.get("content") or ""
        finish_reason = choice.get("finish_reason") or "stop"

        tool_calls: list[ToolCall] = []
        for tc in message.get("tool_calls") or []:
            fn = tc.get("function") or {}
            raw_args = fn.get("arguments")
            if isinstance(raw_args, dict):
                args = raw_args
            else:
                try:
                    args = json.loads(raw_args) if raw_args else {}
                except (json.JSONDecodeError, TypeError):
                    args = {"_raw": raw_args}
            kwargs = {"name": fn.get("name") or "", "args": args}
            if tc.get("id"):
                kwargs["id"] = tc["id"]
            tool_calls.append(ToolCall(**kwargs))

        if tool_calls and finish_reason not in ("tool_calls", "error"):
            finish_reason = "tool_calls"

        usage = payload.get("usage") or {}
        cost_usd = usage.get("cost")
        try:
            cost_usd = float(cost_usd) if cost_usd is not None else 0.0
        except (TypeError, ValueError):
            cost_usd = 0.0

        return ModelResponse(
            content=content,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            cost_usd=cost_usd,
            raw=payload,
        )
