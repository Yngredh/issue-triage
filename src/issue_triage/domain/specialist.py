"""Saída, pedido, resultado e porta dos especialistas (Issue Triage Spec, seções 1, 2 e 5).

D10 e D22.

O modelo `SpecialistOutput` é a cópia literal de "Contratos > Especialistas > Contrato das
ferramentas e da saída": é usado no `output_format` do Agent SDK e na checagem 1 do validador.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

SpecialistName = Literal["bug", "melhoria", "dominio"]

LIST_DOCS = "list_docs"
SEARCH_DOCS = "search_docs"
READ_DOC = "read_doc"

# Ferramentas de cada especialista (Issue Triage Spec, seção 2).
SPECIALIST_TOOLS: dict[SpecialistName, tuple[str, ...]] = {
    "bug": (SEARCH_DOCS, READ_DOC),
    "melhoria": (LIST_DOCS, READ_DOC),
    "dominio": (LIST_DOCS, SEARCH_DOCS, READ_DOC),
}

# Motivos de parada (`SpecialistResult.stop_reason`).
STOP_COMPLETED = "completed"
LIMIT_TURNS = "limit_exceeded:turns"
LIMIT_TOOL_CALLS = "limit_exceeded:tool_calls"
LIMIT_BUDGET = "limit_exceeded:budget"
LIMIT_READ_CHARS = "limit_exceeded:read_chars"


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: Literal["issue", "catalog"]
    # issue: "title" ou "description"; catalog: nome do arquivo .md
    ref: str = Field(min_length=1)
    quote: str = Field(min_length=1, max_length=300)


class SpecialistOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: Literal["bug", "melhoria", "duvida"]
    summary: str = Field(min_length=1, max_length=500)
    next_step: str = Field(min_length=1, max_length=500)
    conclusion: Literal["procede", "nao_procede", "sem_informacao"]
    conclusion_reason: str = Field(min_length=1, max_length=500)
    # obrigatório no JSON; null fora do especialista de domínio
    answer: str | None = Field(max_length=1000)
    evidence: list[Evidence] = Field(max_length=10)


@dataclass(frozen=True)
class SpecialistRequest:
    """Uma chamada a um especialista.

    `docs_dir` é a pasta `CATALOG_SOURCE_DIR/<repositório>` do caso, a única que as ferramentas
    enxergam; `tools` são os nomes curtos das ferramentas liberadas (`SPECIALIST_TOOLS`). O
    texto da issue entra só em `user_message`, no canal de dados (RNF09).
    """

    specialist: SpecialistName
    model: str
    system_prompt: str
    user_message: str
    docs_dir: Path
    tools: tuple[str, ...]


@dataclass(frozen=True)
class SpecialistResult:
    """Resultado de uma chamada a um especialista.

    `raw_output` é a saída estruturada bruta (vai para `proposals.raw_output` mesmo reprovada);
    é nula quando a sessão terminou por limite ou quando o modelo terminou sem saída estruturada
    (inclusive `error_max_structured_output_retries`, D23). `tool_calls` é a lista gravada em
    `proposals.tool_calls`. `stop_reason` é `completed` ou `limit_exceeded:<motivo>`.
    """

    raw_output: Any
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    cost_usd: float | None = None
    stop_reason: str = STOP_COMPLETED

    @property
    def limit_exceeded(self) -> bool:
        """Se a sessão terminou por estouro de turnos, ferramentas, custo ou leitura."""
        return self.stop_reason.startswith("limit_exceeded:")


class SpecialistRunner(Protocol):
    """Porta dos especialistas (D10).

    Os limites (`SPECIALIST_MAX_*` e `SPECIALIST_TIMEOUT_S`) são do adaptador. Erros do provedor
    e tempo esgotado sobem como exceção; a classificação fica no consumidor (Etapa 10).
    """

    async def run(self, spec: SpecialistRequest) -> SpecialistResult:
        """Roda o especialista com as ferramentas do catálogo e devolve o resultado."""
        ...
