"""Adaptador real da porta `RouterLLM` com o SDK `anthropic` (D10, D16).

Saída estruturada por tool use forçado na ferramenta `route_decision`, com
`max_tokens = ROUTER_MAX_TOKENS` e tempo `ROUTER_TIMEOUT_S`. A chave fica só no cliente do SDK:
nunca vai para log, banco ou `repr`. Erros do provedor sobem como exceção; a classificação em
transitório e permanente e as novas tentativas ficam no consumidor (Issue Triage Spec, seção 6),
por isso o cliente não refaz chamadas sozinho (`max_retries = 0`).
"""

from typing import Any

import structlog
from anthropic import AsyncAnthropic
from pydantic import SecretStr

from issue_triage.domain.prompts import ROUTER_TOOL_NAME

logger = structlog.get_logger()


class AnthropicRouterLLM:
    """`RouterLLM` com a API de mensagens da Anthropic."""

    def __init__(
        self,
        api_key: SecretStr,
        *,
        model: str,
        max_tokens: int,
        timeout_s: float,
        client: Any | None = None,
    ) -> None:
        self.model = model
        self._max_tokens = max_tokens
        self._timeout_s = timeout_s
        self._client = client or AsyncAnthropic(
            api_key=api_key.get_secret_value(), timeout=timeout_s, max_retries=0
        )

    def __repr__(self) -> str:
        return f"AnthropicRouterLLM(model={self.model!r})"

    async def structured(self, system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
        """Chama o modelo forçando a ferramenta `route_decision` e devolve a entrada dela.

        Resposta sem a chamada da ferramenta devolve `{}`, que a validação recusa como
        `schema_invalid`.
        """
        response = await self._client.messages.create(
            model=self.model,
            max_tokens=self._max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            tools=[{"name": ROUTER_TOOL_NAME, "input_schema": schema}],
            tool_choice={"type": "tool", "name": ROUTER_TOOL_NAME},
            timeout=self._timeout_s,
        )
        usage = getattr(response, "usage", None)
        logger.info(
            "triage.model_response",
            stage="router",
            model=self.model,
            request_id=getattr(response, "_request_id", None),
            stop_reason=getattr(response, "stop_reason", None),
            input_tokens=getattr(usage, "input_tokens", None),
            output_tokens=getattr(usage, "output_tokens", None),
        )
        for block in response.content:
            if getattr(block, "type", None) == "tool_use" and block.name == ROUTER_TOOL_NAME:
                return block.input if isinstance(block.input, dict) else {}
        return {}
