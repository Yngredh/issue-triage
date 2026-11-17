"""Portas falsas da IA (D10), em memória, com respostas programáveis por chamada.

Nenhum teste chama a Anthropic de verdade.
"""

from dataclasses import dataclass, field
from typing import Any


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
