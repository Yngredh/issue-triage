"""Portas falsas da IA (D10), em memória, com respostas programáveis por chamada.

O `FakeSpecialistRunner` executa as ferramentas do catálogo de verdade (só leitura de arquivos).

Nenhum teste chama a Anthropic de verdade.
"""

from dataclasses import dataclass, field
from typing import Any

from issue_triage.adapters.catalog_tools import CatalogTools
from issue_triage.domain.specialist import (
    STOP_COMPLETED,
    SpecialistRequest,
    SpecialistResult,
)


@dataclass(frozen=True)
class RouterCall:
    """Chamada recebida pelo `FakeRouterLLM`."""

    system: str
    user: str
    schema: dict[str, Any]


@dataclass
class FakeRouterLLM:
    """`RouterLLM` falso: devolve as respostas programadas, uma por chamada, em ordem.

    Uma resposta que é exceção é levantada. Chamada além das programadas falha o teste.
    """

    responses: list[dict[str, Any] | BaseException] = field(default_factory=list)
    model: str = "fake-router-model"
    calls: list[RouterCall] = field(default_factory=list)

    async def structured(self, system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(RouterCall(system, user, schema))
        if len(self.calls) > len(self.responses):
            raise AssertionError(f"unexpected router call #{len(self.calls)}")
        response = self.responses[len(self.calls) - 1]
        if isinstance(response, BaseException):
            raise response
        return response


def decision(category: str, evidence: str, reason: str = "Motivo da categoria.") -> dict[str, Any]:
    """Resposta da ferramenta `route_decision`."""
    return {"category": category, "reason": reason, "evidence": evidence}


@dataclass(frozen=True)
class SpecialistTurn:
    """Resposta programada do `FakeSpecialistRunner`.

    `tool_calls` são executadas de verdade, em ordem, numa sessão `CatalogTools` presa à pasta
    do pedido (qualquer ferramenta, para simular também uma fora da lista). Se uma delas estourar
    um limite, a sessão termina na hora e `output` é ignorada, como no adaptador real.
    `stop_reason` simula o fim por turnos ou custo (`limit_exceeded:turns` ou `:budget`).
    """

    output: Any = None
    tool_calls: tuple[tuple[str, dict[str, Any]], ...] = ()
    stop_reason: str = STOP_COMPLETED
    cost_usd: float | None = 0.001


@dataclass
class FakeSpecialistRunner:
    """`SpecialistRunner` falso: uma resposta programada por chamada, em ordem.

    Uma resposta que é exceção é levantada. Chamada além das programadas falha o teste.
    """

    responses: list[SpecialistTurn | BaseException] = field(default_factory=list)
    max_tool_calls: int = 8
    max_read_chars: int = 20000
    calls: list[SpecialistRequest] = field(default_factory=list)

    async def run(self, spec: SpecialistRequest) -> SpecialistResult:
        self.calls.append(spec)
        if len(self.calls) > len(self.responses):
            raise AssertionError(f"unexpected specialist call #{len(self.calls)}")
        response = self.responses[len(self.calls) - 1]
        if isinstance(response, BaseException):
            raise response
        tools = CatalogTools(
            spec.docs_dir, max_tool_calls=self.max_tool_calls, max_read_chars=self.max_read_chars
        )
        for name, arguments in response.tool_calls:
            tools.call(name, arguments)
            if tools.limit is not None:
                return SpecialistResult(
                    raw_output=None,
                    tool_calls=tools.tool_calls(),
                    cost_usd=None,
                    stop_reason=f"limit_exceeded:{tools.limit}",
                )
        limited = response.stop_reason != STOP_COMPLETED
        return SpecialistResult(
            raw_output=None if limited else response.output,
            tool_calls=tools.tool_calls(),
            cost_usd=response.cost_usd,
            stop_reason=response.stop_reason,
        )
