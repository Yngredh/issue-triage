"""Adaptador real do roteador: só a montagem da chamada, com um cliente falso (sem API)."""

from types import SimpleNamespace
from typing import Any

import structlog
from pydantic import SecretStr

from issue_triage.adapters.anthropic_router import AnthropicRouterLLM
from issue_triage.domain.router import RouteDecision

API_KEY = "sk-ant-test-key-never-logged"


class FakeMessages:
    def __init__(self, content: list[Any]) -> None:
        self.content = content
        self.kwargs: dict[str, Any] = {}

    async def create(self, **kwargs: Any) -> SimpleNamespace:
        self.kwargs = kwargs
        return SimpleNamespace(content=self.content)


def _adapter(content: list[Any]) -> tuple[AnthropicRouterLLM, FakeMessages]:
    messages = FakeMessages(content)
    adapter = AnthropicRouterLLM(
        SecretStr(API_KEY),
        model="claude-haiku-5-5",
        max_tokens=300,
        timeout_s=20,
        client=SimpleNamespace(messages=messages),
    )
    return adapter, messages


async def test_forces_the_route_decision_tool_with_limits() -> None:
    tool_input = {"category": "bug", "reason": "r", "evidence": "e"}
    adapter, messages = _adapter(
        [
            SimpleNamespace(type="text", text="ignorado"),
            SimpleNamespace(type="tool_use", name="route_decision", input=tool_input),
        ]
    )
    schema = RouteDecision.model_json_schema()

    result = await adapter.structured("sistema", "usuário", schema)

    assert result == tool_input
    assert messages.kwargs == {
        "model": "claude-haiku-5-5",
        "max_tokens": 300,
        "system": "sistema",
        "messages": [{"role": "user", "content": "usuário"}],
        "tools": [{"name": "route_decision", "input_schema": schema}],
        "tool_choice": {"type": "tool", "name": "route_decision"},
        "timeout": 20,
    }


async def test_answer_without_the_tool_call_is_empty() -> None:
    adapter, _ = _adapter([SimpleNamespace(type="text", text="bug")])

    assert await adapter.structured("s", "u", {}) == {}


def test_real_client_is_built_without_retries_and_key_is_hidden() -> None:
    adapter = AnthropicRouterLLM(
        SecretStr(API_KEY), model="claude-haiku-5-5", max_tokens=300, timeout_s=20
    )

    assert adapter.model == "claude-haiku-5-5"
    assert adapter._client.max_retries == 0
    assert adapter._client.timeout == 20
    assert API_KEY not in repr(adapter)


async def test_logs_the_model_response_without_the_key() -> None:
    response = SimpleNamespace(
        content=[],
        usage=SimpleNamespace(input_tokens=12, output_tokens=3),
        stop_reason="tool_use",
        _request_id="req_abc",
    )
    adapter = AnthropicRouterLLM(
        SecretStr(API_KEY),
        model="claude-haiku-5-5",
        max_tokens=300,
        timeout_s=20,
        client=SimpleNamespace(messages=SimpleNamespace(create=lambda **_kwargs: _done(response))),
    )

    with structlog.testing.capture_logs() as logs:
        await adapter.structured("s", "u", {})

    [entry] = [e for e in logs if e["event"] == "triage.model_response"]
    assert entry["stage"] == "router"
    assert entry["model"] == "claude-haiku-5-5"
    assert entry["request_id"] == "req_abc"
    assert (entry["input_tokens"], entry["output_tokens"]) == (12, 3)
    assert API_KEY not in repr(logs)


async def _done(value: Any) -> Any:
    return value
