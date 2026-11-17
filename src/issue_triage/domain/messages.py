"""Mensagem da fila `analysis`, produzida pelo issue-analyst-api ("Contratos").

O `close` (resolução manual de encerramento) fica de fora até a decisão do P1 (Etapa 11).
"""

from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, model_validator

Trigger = Literal["initial", "author_edit", "enrich", "reprocess"]

TRIGGERS_WITH_REF = frozenset({"author_edit", "enrich", "reprocess"})


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
        """`attempt` é nulo só em `author_edit`; `triggerRef` é obrigatório fora do `initial`."""
        if self.trigger == "author_edit":
            if self.attempt is not None:
                raise ValueError("attempt must be null for author_edit")
        elif self.attempt is None:
            raise ValueError("attempt is required")
        if self.trigger in TRIGGERS_WITH_REF and not self.trigger_ref:
            raise ValueError("triggerRef is required")
        return self
