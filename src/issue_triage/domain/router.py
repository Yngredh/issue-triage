"""Roteador de issues (Spec D; "Contratos > Roteador" do plano).

Monta a entrada com o texto da issue no canal de dados (RNF09), chama a porta `RouterLLM`
(D10), valida a resposta sem LLM e faz no máximo uma nova chamada com o erro no prompt. A
persistência e o despacho ficam no processamento da mensagem (`issue_triage.analysis`).
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

import structlog
from pydantic import BaseModel, Field, ValidationError

from issue_triage.domain.catalog import TriageGuideline
from issue_triage.domain.prompts import (
    ROUTER_RETRY_TEMPLATE,
    ROUTER_SYSTEM_PROMPT,
    ROUTER_USER_TEMPLATE,
)

logger = structlog.get_logger()

Category = Literal["bug", "melhoria", "duvida", "indeterminado"]

SCHEMA_INVALID = "schema_invalid"
UNGROUNDED_CLAIM = "ungrounded_claim"

# Marcadores dos blocos de dados; dentro dos textos eles são neutralizados (caso I02).
_BLOCK_TAG = re.compile(r"<(\s*/?\s*(?:issue|enriquecimento)\s*)>", re.IGNORECASE)


class RouteDecision(BaseModel):
    """Saída estruturada do roteador (Spec D), sem campo de confiança."""

    category: Category
    reason: str = Field(max_length=300)
    evidence: str


class RouterLLM(Protocol):
    """Porta do modelo do roteador (D10).

    `structured` devolve a entrada da ferramenta `route_decision` chamada pelo modelo, ou um
    dicionário vazio se a resposta não chamou a ferramenta. Erros do provedor sobem como exceção.
    """

    model: str

    async def structured(self, system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
        """Chama o modelo com tool use forçado e devolve a entrada da ferramenta."""
        ...


@dataclass(frozen=True)
class Rejection:
    """Resposta recusada pela validação: `code` é `schema_invalid` ou `ungrounded_claim`."""

    code: str
    detail: str

    @property
    def error(self) -> str:
        """Texto gravado em `analysis_attempts.error` (D20)."""
        return f"router: {self.code}: {self.detail}"


@dataclass(frozen=True)
class Routed:
    """Decisão aprovada; `llm_calls` é 2 quando houve nova chamada."""

    decision: RouteDecision
    llm_calls: int


@dataclass(frozen=True)
class RouterFailed:
    """O roteador foi reprovado nas duas chamadas; `rejection` é o motivo da segunda."""

    rejection: Rejection
    llm_calls: int = 2


def neutralize(text: str) -> str:
    """Neutraliza `<issue>`, `</issue>`, `<enriquecimento>` e `</enriquecimento>` no texto.

    Os sinais `<` e `>` desses marcadores viram `&lt;` e `&gt;`, para que o texto não feche nem
    abra um bloco de dados; o resto do texto fica igual.
    """
    return _BLOCK_TAG.sub(r"&lt;\1&gt;", text)


def _guidelines_lines(guidelines: Sequence[TriageGuideline]) -> str:
    if not guidelines:
        return "- nenhuma"
    return "\n".join(
        f"- [{item.category}] {item.guideline}" if item.category else f"- {item.guideline}"
        for item in guidelines
    )


def _enrichment_block(enrichments: Sequence[str]) -> str:
    if not enrichments:
        return ""
    body = "\n\n".join(neutralize(text) for text in enrichments)
    return f"<enriquecimento>\n{body}\n</enriquecimento>"


def build_user_message(
    *,
    repository: str,
    title: str,
    description: str,
    enrichments: Sequence[str],
    guidelines: Sequence[TriageGuideline],
) -> str:
    """Monta a mensagem do usuário do roteador (`ROUTER_USER_TEMPLATE`).

    Título, descrição e enriquecimentos são dados (RNF09) e são neutralizados antes; o bloco
    `<enriquecimento>` só entra quando houver texto de `enrich` (D14).
    """
    return ROUTER_USER_TEMPLATE.substitute(
        repository=repository,
        orientacoes=_guidelines_lines(guidelines),
        title=neutralize(title),
        description=neutralize(description),
        bloco_enriquecimento=_enrichment_block(enrichments),
    )


def retry_message(user: str, rejection: Rejection) -> str:
    """Mensagem do usuário da nova chamada: a original mais a linha com o motivo da recusa."""
    line = ROUTER_RETRY_TEMPLATE.substitute(codigo=rejection.code, detalhe=rejection.detail)
    original = user.rstrip("\n")
    return f"{original}\n\n{line}"


def _normalize(text: str) -> str:
    return " ".join(text.split()).casefold()


_SCHEMA_DETAILS = {
    "missing": "campo {field} ausente",
    "literal_error": "campo {field} fora da lista bug, melhoria, duvida, indeterminado",
    "string_too_long": "campo {field} acima de 300 caracteres",
    "string_type": "campo {field} não é texto",
}


def _schema_detail(raw: dict[str, Any], exc: ValidationError) -> str:
    if not raw:
        return "a resposta não chamou a ferramenta route_decision"
    details = []
    for error in exc.errors():
        field = ".".join(str(part) for part in error["loc"]) or "resposta"
        template = _SCHEMA_DETAILS.get(error["type"], "campo {field} inválido")
        details.append(template.format(field=field))
    return "; ".join(details)


def validate_decision(
    raw: dict[str, Any], *, title: str, description: str
) -> RouteDecision | Rejection:
    """Valida a resposta sem LLM, em ordem: schema, tamanho de `reason` e evidência literal.

    A evidência precisa aparecer no título ou na descrição depois de normalizar espaços e
    maiúsculas; só em `indeterminado` ela pode ficar vazia.
    """
    try:
        decision = RouteDecision.model_validate(raw)
    except ValidationError as exc:
        return Rejection(SCHEMA_INVALID, _schema_detail(raw, exc))

    evidence = _normalize(decision.evidence)
    if not evidence:
        if decision.category == "indeterminado":
            return decision
        return Rejection(UNGROUNDED_CLAIM, "evidence vazia fora de indeterminado")
    if evidence not in _normalize(title) and evidence not in _normalize(description):
        return Rejection(UNGROUNDED_CLAIM, "evidence não aparece no título nem na descrição")
    return decision


async def route(
    llm: RouterLLM,
    *,
    repository: str,
    title: str,
    description: str,
    enrichments: Sequence[str],
    guidelines: Sequence[TriageGuideline],
) -> Routed | RouterFailed:
    """Roteia a issue com no máximo duas chamadas ao modelo (Spec D).

    Falha de schema ou de evidência gera uma nova chamada, com o erro no prompt (log
    `triage.router_retry`); reprovada de novo, devolve `RouterFailed`.
    """
    user = build_user_message(
        repository=repository,
        title=title,
        description=description,
        enrichments=enrichments,
        guidelines=guidelines,
    )
    schema = RouteDecision.model_json_schema()

    raw = await llm.structured(ROUTER_SYSTEM_PROMPT, user, schema)
    result = validate_decision(raw, title=title, description=description)
    if isinstance(result, RouteDecision):
        return Routed(result, llm_calls=1)

    logger.info("triage.router_retry", code=result.code)
    raw = await llm.structured(ROUTER_SYSTEM_PROMPT, retry_message(user, result), schema)
    result = validate_decision(raw, title=title, description=description)
    if isinstance(result, RouteDecision):
        return Routed(result, llm_calls=2)
    return RouterFailed(result)
