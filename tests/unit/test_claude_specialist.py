"""Adaptador real do especialista: montagem das opções e laço da sessão com um cliente falso.

Nenhuma chamada à Anthropic nem ao CLI do Claude Code.
"""

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import ClaudeAgentOptions, ResultError, ResultMessage
from pydantic import SecretStr

from issue_triage.adapters import claude_specialist
from issue_triage.adapters.catalog_tools import CatalogTools
from issue_triage.adapters.claude_specialist import ClaudeSpecialistRunner, sdk_tool
from issue_triage.domain.specialist import (
    SPECIALIST_TOOLS,
    SpecialistOutput,
    SpecialistRequest,
)

API_KEY = "sk-ant-test-key-never-logged"


def _request(folder: Path, specialist: str = "bug") -> SpecialistRequest:
    return SpecialistRequest(
        specialist=specialist,
        model="claude-haiku-5-5",
        system_prompt="prompt de sistema",
        user_message="<issue>dados</issue>",
        docs_dir=folder,
        tools=SPECIALIST_TOOLS[specialist],
    )


def _runner(client_factory: Any = None, *, timeout_s: float = 90) -> ClaudeSpecialistRunner:
    return ClaudeSpecialistRunner(
        SecretStr(API_KEY),
        max_turns=6,
        max_tool_calls=2,
        max_budget_usd=0.05,
        max_read_chars=20000,
        timeout_s=timeout_s,
        client_factory=client_factory or FakeClient,
    )


def _result(subtype: str = "success", **fields: Any) -> ResultMessage:
    return ResultMessage(
        subtype=subtype,
        duration_ms=10,
        duration_api_ms=8,
        is_error=fields.pop("is_error", subtype != "success"),
        num_turns=2,
        session_id="session",
        **fields,
    )


OUTPUT = {
    "category": "bug",
    "summary": "Resumo.",
    "next_step": "Próximo passo.",
    "conclusion": "procede",
    "conclusion_reason": "Motivo.",
    "answer": None,
    "evidence": [{"source": "catalog", "ref": "a.md", "quote": "trecho"}],
}


@pytest.mark.parametrize("specialist", ["bug", "melhoria", "dominio"])
def test_options_expose_only_the_specialist_tools_with_the_limits(
    tmp_path: Path, specialist: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    servers: list[dict[str, Any]] = []
    original = claude_specialist.create_sdk_mcp_server

    def capture(name: str, tools: list) -> Any:
        servers.append({"name": name, "tools": [item.name for item in tools]})
        return original(name=name, tools=tools)

    monkeypatch.setattr(claude_specialist, "create_sdk_mcp_server", capture)
    runner = _runner()
    spec = _request(tmp_path, specialist)

    options = runner.build_options(spec, runner.new_tools(spec))

    assert servers == [{"name": "catalog", "tools": list(SPECIALIST_TOOLS[specialist])}]
    assert list(options.mcp_servers) == ["catalog"]
    assert options.mcp_servers["catalog"]["type"] == "sdk"
    assert options.tools == []  # ferramentas embutidas desligadas
    assert options.allowed_tools == [
        f"mcp__catalog__{name}" for name in SPECIALIST_TOOLS[specialist]
    ]
    assert options.strict_mcp_config is True
    assert options.setting_sources == []
    assert options.permission_mode == "dontAsk"
    assert options.model == "claude-haiku-5-5"
    assert options.system_prompt == "prompt de sistema"
    assert options.max_turns == 6
    assert options.max_budget_usd == 0.05
    assert options.output_format == {
        "type": "json_schema",
        "schema": SpecialistOutput.model_json_schema(),
    }
    assert options.env == {"ANTHROPIC_API_KEY": API_KEY}


def test_tools_per_specialist_follow_the_spec() -> None:
    assert SPECIALIST_TOOLS == {
        "bug": ("search_docs", "read_doc"),
        "melhoria": ("list_docs", "read_doc"),
        "dominio": ("list_docs", "search_docs", "read_doc"),
    }


def test_runner_repr_hides_the_key() -> None:
    assert API_KEY not in repr(_runner())


async def test_sdk_tool_calls_the_session_and_keeps_the_short_name(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("conteúdo", encoding="utf-8")
    tools = CatalogTools(tmp_path, max_tool_calls=8, max_read_chars=20000)
    read_doc = sdk_tool(tools, "read_doc")

    result = await read_doc.handler({"path": "a.md"})

    assert read_doc.name == "read_doc"
    assert read_doc.input_schema["required"] == ["path"]
    assert json.loads(result["content"][0]["text"])["content"] == "conteúdo"
    assert [call["name"] for call in tools.tool_calls()] == ["read_doc"]


class FakeClient:
    """Cliente falso do SDK: `script` roda dentro de `receive_response`."""

    script: Any = None

    def __init__(self, options: ClaudeAgentOptions) -> None:
        self.options = options
        self.prompts: list[str] = []
        self.connected = False
        self.disconnected = False
        FakeClient.last = self

    async def connect(self) -> None:
        self.connected = True

    async def query(self, prompt: str) -> None:
        self.prompts.append(prompt)

    async def receive_response(self) -> Any:
        async for message in type(self).script(self):
            yield message

    async def disconnect(self) -> None:
        self.disconnected = True


def _client_with(script: Any) -> type[FakeClient]:
    return type("ScriptedClient", (FakeClient,), {"script": staticmethod(script)})


async def test_success_returns_the_structured_output_and_cost(tmp_path: Path) -> None:
    async def script(client: FakeClient) -> Any:
        yield _result(structured_output=OUTPUT, total_cost_usd=0.012)

    client_cls = _client_with(script)
    runner = _runner(client_cls)

    result = await runner.run(_request(tmp_path))

    assert result.raw_output == OUTPUT
    assert result.stop_reason == "completed"
    assert result.cost_usd == 0.012
    assert result.tool_calls == []
    assert client_cls.last.prompts == ["<issue>dados</issue>"]
    assert client_cls.last.disconnected is True


@pytest.mark.parametrize(
    ("subtype", "stop_reason"),
    [
        ("error_max_turns", "limit_exceeded:turns"),
        ("error_max_budget_usd", "limit_exceeded:budget"),
    ],
)
async def test_sdk_limits_map_to_limit_exceeded(
    tmp_path: Path, subtype: str, stop_reason: str
) -> None:
    async def script(client: FakeClient) -> Any:
        yield _result(subtype, total_cost_usd=0.06)

    result = await _runner(_client_with(script)).run(_request(tmp_path))

    assert result.stop_reason == stop_reason
    assert result.limit_exceeded is True
    assert result.raw_output is None


async def test_structured_output_retries_returns_null_output(tmp_path: Path) -> None:
    # D23: falha do modelo, não do provedor; o validador reprova com `schema_invalid`.
    async def script(client: FakeClient) -> Any:
        yield _result("error_max_structured_output_retries", total_cost_usd=0.02)

    result = await _runner(_client_with(script)).run(_request(tmp_path))

    assert result.raw_output is None
    assert result.stop_reason == "completed"
    assert result.limit_exceeded is False
    assert result.cost_usd == 0.02


async def test_provider_error_result_raises(tmp_path: Path) -> None:
    async def script(client: FakeClient) -> Any:
        yield _result("success", is_error=True, api_error_status=529, result="API Error")

    with pytest.raises(ResultError) as error:
        await _runner(_client_with(script)).run(_request(tmp_path))

    assert error.value.api_error_status == 529


class LimitRunner(ClaudeSpecialistRunner):
    """Guarda a sessão das ferramentas para o cliente falso chamar as ferramentas."""

    def new_tools(self, spec: SpecialistRequest) -> CatalogTools:
        self.tools = super().new_tools(spec)
        return self.tools


async def test_tool_limit_ends_the_session_at_once_ignoring_the_model(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("A", encoding="utf-8")
    runner: LimitRunner

    async def script(client: FakeClient) -> Any:
        for _ in range(3):  # max_tool_calls = 2: a terceira estoura
            runner.tools.call("read_doc", {"path": "a.md"})
            await asyncio.sleep(0)
        await asyncio.sleep(3600)  # o modelo continuaria; a sessão precisa terminar antes
        yield _result(structured_output=OUTPUT)

    client_cls = _client_with(script)
    runner = LimitRunner(
        SecretStr(API_KEY),
        max_turns=6,
        max_tool_calls=2,
        max_budget_usd=0.05,
        max_read_chars=20000,
        timeout_s=5,
        client_factory=client_cls,
    )

    result = await runner.run(_request(tmp_path))

    assert result.stop_reason == "limit_exceeded:tool_calls"
    assert result.raw_output is None
    assert [call["error"] for call in result.tool_calls] == [None, None, "limit_exceeded"]
    assert client_cls.last.disconnected is True


async def test_timeout_raises_and_disconnects(tmp_path: Path) -> None:
    async def script(client: FakeClient) -> Any:
        await asyncio.sleep(3600)
        yield _result(structured_output=OUTPUT)

    client_cls = _client_with(script)

    with pytest.raises(TimeoutError):
        await _runner(client_cls, timeout_s=0.05).run(_request(tmp_path))

    assert client_cls.last.disconnected is True
