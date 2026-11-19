"""Mensagem da fila `analysis`, produzida pelo issue-analyst-api ("Contratos").

O `close` (resolução manual de encerramento, P1) não abre tentativa: `triggerRef` é o id da
linha de `inbound.issue_review_history`.
"""

from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, model_validator

Trigger = Literal["initial", "author_edit", "enrich", "reprocess", "close"]

TRIGGERS_WITH_REF = frozenset({"author_edit", "enrich", "reprocess", "close"})


def _is_positive_id(value: str | None) -> bool:
    """Só dígitos ASCII com valor positivo (D30)."""
    return value is not None and value.isascii() and value.isdigit() and int(value) > 0


class Outcome(StrEnum):
    """O que o consumidor faz com a mensagem depois do processamento."""

    ACK = "ack"
    REQUEUE = "requeue"


class AnalysisMessage(BaseModel):
    """Pedido de análise de uma issue; `eventId` é sempre o `origin_event_id` do caso."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: StrictStr = Field(alias="eventId", min_length=1)
    attempt: StrictInt | None = Field(ge=1)
    trigger: Trigger
    trigger_ref: StrictStr | None = Field(alias="triggerRef")

    @model_validator(mode="after")
    def _check_trigger_fields(self) -> Self:
        """`attempt` nulo em `author_edit` e `close`; `triggerRef` obrigatório fora do `initial`."""
        if self.trigger in ("author_edit", "close"):
            if self.attempt is not None:
                raise ValueError(f"attempt must be null for {self.trigger}")
        elif self.attempt is None:
            raise ValueError("attempt is required")
        if self.trigger in TRIGGERS_WITH_REF and not self.trigger_ref:
            raise ValueError("triggerRef is required")
        if self.trigger == "close" and not _is_positive_id(self.trigger_ref):
            raise ValueError("triggerRef must be the numeric id of a review")
        return self
