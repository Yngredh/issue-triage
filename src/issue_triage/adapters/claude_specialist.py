"""Adaptador real da porta `SpecialistRunner` com o `claude-agent-sdk` (D10, D16, D22).

Cada chamada cria uma sessão das ferramentas (`CatalogTools`) e um servidor MCP em processo
chamado `catalog` com só as ferramentas da lista do especialista; as ferramentas embutidas do SDK
ficam desligadas e as configurações do sistema de arquivos não são carregadas. A saída
estruturada vem do `output_format` gerado de `SpecialistOutput`. Limites: `max_turns`,
`max_budget_usd` (do SDK), chamadas e caracteres lidos (das ferramentas) e `SPECIALIST_TIMEOUT_S`.

A chave vai só no ambiente do subprocesso do SDK: nunca para log, banco ou `repr`.
"""

import asyncio
import dataclasses
from collections.abc import Callable
from typing import Any

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultError,
    ResultMessage,
    SdkMcpTool,
    create_sdk_mcp_server,
    tool,
)
from pydantic import SecretStr

from issue_triage.adapters.catalog_tools import CatalogTools
from issue_triage.domain.specialist import (
    LIMIT_BUDGET,
    LIMIT_TURNS,
    LIST_DOCS,
    READ_DOC,
    SEARCH_DOCS,
    STOP_COMPLETED,
    SpecialistOutput,
    SpecialistRequest,
    SpecialistResult,
)

SERVER_NAME = "catalog"

_DESCRIPTIONS = {
    LIST_DOCS: "Lista os documentos .md da base de conhecimento do projeto (até 50 nomes).",
    SEARCH_DOCS: (
        "Busca linhas dos documentos do projeto que contêm todos os termos da consulta, sem "
        "diferenciar maiúsculas nem acentos. Devolve até 10 trechos com arquivo e linha."
    ),
    READ_DOC: (
        "Lê até 4.000 caracteres de um documento .md do projeto a partir de offset; use "
        "next_offset para continuar."
    ),
}

_SCHEMAS: dict[str, dict[str, Any]] = {
    LIST_DOCS: {"type": "object", "properties": {}, "additionalProperties": False},
    SEARCH_DOCS: {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
        "additionalProperties": False,
    },
    READ_DOC: {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "offset": {"type": "integer", "minimum": 0, "default": 0},
        },
        "required": ["path"],
        "additionalProperties": False,
    },
}

# Motivos de parada do SDK que são estouro de limite (Issue Triage Spec, seção 2).
_LIMIT_SUBTYPES = {"error_max_turns": LIMIT_TURNS, "error_max_budget_usd": LIMIT_BUDGET}


def mcp_tool_name(name: str) -> str:
    """Nome com que a ferramenta aparece para o modelo (`mcp__catalog__<nome>`)."""
    return f"mcp__{SERVER_NAME}__{name}"


def output_format() -> dict[str, Any]:
    """`output_format` do SDK gerado de `SpecialistOutput` (D22)."""
    return {"type": "json_schema", "schema": SpecialistOutput.model_json_schema()}


def sdk_tool(tools: CatalogTools, name: str) -> SdkMcpTool[Any]:
    """Ferramenta do servidor MCP `catalog` que chama a sessão `tools`."""

    async def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        return tools.call(name, arguments)

    return tool(name, _DESCRIPTIONS[name], _SCHEMAS[name])(handler)


class ClaudeSpecialistRunner:
    """`SpecialistRunner` com o Claude Agent SDK."""

    def __init__(
        self,
        api_key: SecretStr,
        *,
        max_turns: int,
        max_tool_calls: int,
        max_budget_usd: float,
        max_read_chars: int,
        timeout_s: float,
        client_factory: Callable[[ClaudeAgentOptions], Any] = ClaudeSDKClient,
    ) -> None:
        self._api_key = api_key
        self._max_turns = max_turns
        self._max_tool_calls = max_tool_calls
        self._max_budget_usd = max_budget_usd
        self._max_read_chars = max_read_chars
        self._timeout_s = timeout_s
        self._client_factory = client_factory

    def __repr__(self) -> str:
        return f"ClaudeSpecialistRunner(max_turns={self._max_turns})"

    def new_tools(self, spec: SpecialistRequest) -> CatalogTools:
        """Sessão das ferramentas de uma chamada, presa à pasta do caso."""
        return CatalogTools(
            spec.docs_dir,
            max_tool_calls=self._max_tool_calls,
            max_read_chars=self._max_read_chars,
        )

    def build_options(self, spec: SpecialistRequest, tools: CatalogTools) -> ClaudeAgentOptions:
        """Opções do SDK: só as ferramentas da lista, embutidas desligadas e os limites."""
        server = create_sdk_mcp_server(
            name=SERVER_NAME, tools=[sdk_tool(tools, name) for name in spec.tools]
        )
        return ClaudeAgentOptions(
            model=spec.model,
            system_prompt=spec.system_prompt,
            tools=[],
            mcp_servers={SERVER_NAME: server},
            strict_mcp_config=True,
            allowed_tools=[mcp_tool_name(name) for name in spec.tools],
            permission_mode="dontAsk",
            setting_sources=[],
            max_turns=self._max_turns,
            max_budget_usd=self._max_budget_usd,
            output_format=output_format(),
            env={"ANTHROPIC_API_KEY": self._api_key.get_secret_value()},
        )

    async def run(self, spec: SpecialistRequest) -> SpecialistResult:
        """Roda o especialista; estouro de limite das ferramentas encerra a sessão na hora.

        Tempo acima de `SPECIALIST_TIMEOUT_S` levanta `TimeoutError`; resultado de erro do SDK
        que não é limite (falha do provedor) levanta `ResultError`.
        """
        tools = self.new_tools(spec)
        client = self._client_factory(self.build_options(spec, tools))
        async with asyncio.timeout(self._timeout_s):
            await client.connect()
            try:
                await client.query(spec.user_message)
                message = await self._wait_result(client, tools)
            finally:
                await client.disconnect()

        if tools.limit is not None:
            # A saída do modelo é ignorada (D22).
            return SpecialistResult(
                raw_output=None,
                tool_calls=tools.tool_calls(),
                cost_usd=None,
                stop_reason=f"limit_exceeded:{tools.limit}",
            )
        if message is None:
            raise RuntimeError("specialist session ended without a result")
        if message.subtype in _LIMIT_SUBTYPES:
            return SpecialistResult(
                raw_output=None,
                tool_calls=tools.tool_calls(),
                cost_usd=message.total_cost_usd,
                stop_reason=_LIMIT_SUBTYPES[message.subtype],
            )
        if message.is_error:
            raise ResultError(
                f"specialist run failed: {message.subtype}",
                data=dataclasses.asdict(message),
            )
        return SpecialistResult(
            raw_output=message.structured_output,
            tool_calls=tools.tool_calls(),
            cost_usd=message.total_cost_usd,
            stop_reason=STOP_COMPLETED,
        )

    @staticmethod
    async def _wait_result(client: Any, tools: CatalogTools) -> ResultMessage | None:
        """Espera o `ResultMessage` ou o estouro de um limite das ferramentas, o que vier antes."""

        async def result() -> ResultMessage | None:
            async for message in client.receive_response():
                if isinstance(message, ResultMessage):
                    return message
            return None

        receiving = asyncio.create_task(result())
        limit = asyncio.create_task(tools.limit_reached.wait())
        try:
            await asyncio.wait({receiving, limit}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (receiving, limit):
                task.cancel()
            await asyncio.gather(receiving, limit, return_exceptions=True)
        if tools.limit is not None:
            return None
        return receiving.result()
