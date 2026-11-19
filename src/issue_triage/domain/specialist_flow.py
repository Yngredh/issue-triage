"""Chamada do especialista, contexto acumulado e segunda chance (Issue Triage Spec; D24, D25).

Monta o prompt de sistema e a mensagem do usuário com o texto literal de "Contratos >
Especialistas > Texto dos especialistas", chama a porta `SpecialistRunner` (D10), valida a
saída e faz no máximo uma segunda chamada, em sessão nova, com a proposta recusada como dado.
Estouro de limite vai direto para `INVALID`, sem segunda chance. A persistência fica no
processamento da mensagem (`issue_triage.analysis`).
"""

import json
import re
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

from issue_triage.domain.catalog import Project, TriageGuideline
from issue_triage.domain.issue import IssueText
from issue_triage.domain.prompts import (
    ANSWER_RULE_DOMAIN,
    ANSWER_RULE_OTHERS,
    SPECIALIST_PROMPT_VERSIONS,
    SPECIALIST_RETRY_TEMPLATE,
    SPECIALIST_ROLES,
    SPECIALIST_SYSTEM_PROMPT,
    SPECIALIST_USER_TEMPLATE,
)
from issue_triage.domain.router import guidelines_lines
from issue_triage.domain.specialist import (
    LIMIT_BUDGET,
    LIMIT_READ_CHARS,
    LIMIT_TOOL_CALLS,
    LIMIT_TURNS,
    SPECIALIST_TOOLS,
    SpecialistName,
    SpecialistOutput,
    SpecialistRequest,
    SpecialistResult,
    SpecialistRunner,
)
from issue_triage.domain.validator import Rejection, ValidationContext, check_schema, validate

logger = structlog.get_logger()

# Especialista de cada categoria do roteador ("Processamento", passo 6).
SPECIALIST_BY_CATEGORY: dict[str, SpecialistName] = {
    "bug": "bug",
    "melhoria": "melhoria",
    "duvida": "dominio",
}

# Marcações dos blocos de dados dos especialistas; dentro dos textos elas são neutralizadas (D25).
_BLOCK_TAG = re.compile(
    r"<(\s*/?\s*(?:issue|enriquecimento|propostas_anteriores|proposta_recusada)\s*)>",
    re.IGNORECASE,
)

# Campos da última saída de cada proposta anterior que entram no contexto acumulado (D24).
_PREVIOUS_FIELDS = ("summary", "conclusion", "conclusion_reason", "answer", "evidence")

_LIMIT_DETAILS = {
    LIMIT_TURNS: "a sessão do especialista passou do limite de turnos",
    LIMIT_TOOL_CALLS: "a sessão do especialista passou do limite de chamadas de ferramentas",
    LIMIT_BUDGET: "a sessão do especialista passou do limite de custo",
    LIMIT_READ_CHARS: "a sessão do especialista passou do limite de caracteres lidos",
}


def neutralize(text: str) -> str:
    """Neutraliza as marcações de abertura e fechamento dos blocos de dados (D25).

    Só `<issue>`, `<enriquecimento>`, `<propostas_anteriores>` e `<proposta_recusada>` (e os
    fechamentos), sem diferenciar maiúsculas, viram `&lt;...&gt;`; o resto do texto fica igual.
    """
    return _BLOCK_TAG.sub(r"&lt;\1&gt;", text)


def _one_line_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


@dataclass(frozen=True)
class PreviousProposal:
    """Proposta de uma tentativa anterior do caso, para o contexto acumulado (RF31, D14)."""

    attempt: int
    kind: str
    specialist: str | None
    validation_status: str
    validation_error: str | None
    raw_output: Any


def _last_output(raw_output: Any) -> Any:
    """Saída do modelo na última chamada da tentativa (último item de `raw_output`)."""
    if isinstance(raw_output, list) and raw_output and isinstance(raw_output[-1], dict):
        return raw_output[-1].get("output")
    return None


def _previous_item(proposal: PreviousProposal) -> str:
    if proposal.kind == "missing_info":
        return f"Tentativa {proposal.attempt} (pedido de informações: a issue estava sem descrição)"
    if proposal.validation_status == "VALID":
        verdict = "aprovada"
    else:
        verdict = f"reprovada: {proposal.validation_error}"
    output = _last_output(proposal.raw_output)
    if isinstance(output, dict):
        content: Any = {name: output.get(name) for name in _PREVIOUS_FIELDS}
    else:
        content = None
    header = f"Tentativa {proposal.attempt} (especialista {proposal.specialist}, {verdict}):"
    return f"{header}\n{neutralize(_one_line_json(content))}"


def previous_proposals_block(proposals: Sequence[PreviousProposal]) -> str:
    """Bloco `<propostas_anteriores>` (RF31, D24 e D25), vazio sem propostas anteriores."""
    if not proposals:
        return ""
    items = sorted(proposals, key=lambda item: item.attempt)
    body = "\n\n".join(_previous_item(item) for item in items)
    return f"<propostas_anteriores>\n{body}\n</propostas_anteriores>"


def _enrichment_block(enrichments: Sequence[str]) -> str:
    if not enrichments:
        return ""
    body = "\n\n".join(neutralize(text) for text in enrichments)
    return f"<enriquecimento>\n{body}\n</enriquecimento>"


def build_system_prompt(specialist: SpecialistName, *, category: str, token: str) -> str:
    """Prompt de sistema do especialista (`SPECIALIST_SYSTEM_PROMPT`), com o token (D12)."""
    answer_rule = ANSWER_RULE_DOMAIN if specialist == "dominio" else ANSWER_RULE_OTHERS
    return SPECIALIST_SYSTEM_PROMPT.substitute(
        **SPECIALIST_ROLES[specialist],
        categoria=category,
        regra_answer=answer_rule,
        token=token,
    )


def build_user_message(
    *,
    project: Project,
    category: str,
    text: IssueText,
    guidelines: Sequence[TriageGuideline],
    previous: Sequence[PreviousProposal],
) -> str:
    """Mensagem do usuário do especialista (`SPECIALIST_USER_TEMPLATE`).

    Título, descrição, enriquecimentos e propostas anteriores são dados (RNF09): só as
    marcações dos blocos são neutralizadas antes (D25).
    """
    return SPECIALIST_USER_TEMPLATE.substitute(
        nome_projeto=project.name,
        repository=project.repository,
        descricao_projeto=project.description,
        responsavel=project.responsible,
        orientacoes=guidelines_lines(guidelines),
        categoria=category,
        title=neutralize(text.title),
        description=neutralize(text.description),
        bloco_enriquecimento=_enrichment_block(text.enrichments),
        bloco_propostas_anteriores=previous_proposals_block(previous),
    )


def retry_message(user: str, rejected_output: Any, rejection: Rejection) -> str:
    """Mensagem da segunda chance: a original mais `<proposta_recusada>` e o motivo (D24)."""
    extra = SPECIALIST_RETRY_TEMPLATE.substitute(
        json_recusado=neutralize(_one_line_json(rejected_output)),
        codigo=rejection.code,
        detalhe=rejection.detail,
    )
    original = user.rstrip("\n")
    return f"{original}\n{extra}"


@dataclass(frozen=True)
class SpecialistOutcome:
    """Resultado do especialista numa tentativa, pronto para gravar em `proposals` (D24, D25).

    `output` é a saída aprovada (`None` se reprovada). `rejection` é o motivo final da
    reprovação. `last_checked` é a última saída se ela passou na checagem 1, usada nas colunas
    da proposta `INVALID` (D25). `raw_output` é a lista por chamada e `tool_calls` junta as
    chamadas das duas sessões, com o campo `call`.
    """

    specialist: SpecialistName
    model: str
    prompt_version: str
    output: SpecialistOutput | None
    rejection: Rejection | None
    last_checked: SpecialistOutput | None
    raw_output: list[dict[str, Any]] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    llm_calls: int = 1

    @property
    def valid(self) -> bool:
        return self.output is not None


async def run_specialist(
    runner: SpecialistRunner,
    *,
    category: str,
    models: Mapping[str, str],
    project: Project,
    text: IssueText,
    guidelines: Sequence[TriageGuideline],
    previous: Sequence[PreviousProposal],
    docs_dir: Path,
) -> SpecialistOutcome:
    """Roda o especialista da categoria com no máximo duas chamadas (Issue Triage Spec, seção 3).

    Reprovada uma vez, faz uma segunda chamada em sessão nova, com o mesmo prompt de sistema
    (o mesmo token) e a proposta recusada como dado (log `triage.specialist_retry`). Estouro de
    limite termina na hora, sem segunda chance (log `triage.limit_exceeded`).
    """
    specialist = SPECIALIST_BY_CATEGORY[category]
    model = models[specialist]
    token = secrets.token_hex(8)
    system = build_system_prompt(specialist, category=category, token=token)
    user = build_user_message(
        project=project, category=category, text=text, guidelines=guidelines, previous=previous
    )
    ctx = ValidationContext(
        specialist=specialist,
        category=category,
        title=text.title,
        description=text.description,
        enrichments=text.enrichments,
        docs_dir=docs_dir,
        responsible=project.responsible,
        token=token,
    )
    raw_output: list[dict[str, Any]] = []
    tool_calls: list[dict[str, Any]] = []

    def finish(
        output: SpecialistOutput | None, rejection: Rejection | None, calls: int
    ) -> SpecialistOutcome:
        last = raw_output[-1]["output"]
        checked = check_schema(last, specialist) if last is not None else None
        return SpecialistOutcome(
            specialist=specialist,
            model=model,
            prompt_version=SPECIALIST_PROMPT_VERSIONS[specialist],
            output=output,
            rejection=rejection,
            last_checked=checked if isinstance(checked, SpecialistOutput) else None,
            raw_output=raw_output,
            tool_calls=tool_calls,
            llm_calls=calls,
        )

    async def attempt_call(call: int, message: str) -> SpecialistOutput | Rejection:
        logger.info("triage.specialist_started", specialist=specialist, call=call)
        result: SpecialistResult = await runner.run(
            SpecialistRequest(
                specialist=specialist,
                model=model,
                system_prompt=system,
                user_message=message,
                docs_dir=docs_dir,
                tools=SPECIALIST_TOOLS[specialist],
            )
        )
        tool_calls.extend({**item, "call": call} for item in result.tool_calls)
        if result.limit_exceeded:
            detail = _LIMIT_DETAILS.get(result.stop_reason, "a sessão passou de um limite")
            entry = {"call": call, "output": None, "validation_error": result.stop_reason}
            raw_output.append(entry)
            logger.info(
                "triage.limit_exceeded", specialist=specialist, call=call, reason=result.stop_reason
            )
            return Rejection(result.stop_reason, detail)
        verdict = validate(result.raw_output, ctx=ctx, tool_calls=tool_calls)
        code = verdict.code if isinstance(verdict, Rejection) else None
        raw_output.append({"call": call, "output": result.raw_output, "validation_error": code})
        return verdict

    first = await attempt_call(1, user)
    if isinstance(first, SpecialistOutput):
        return finish(first, None, 1)
    if first.code.startswith("limit_exceeded:"):
        return finish(None, first, 1)

    logger.info("triage.specialist_retry", specialist=specialist, code=first.code)
    rejected = raw_output[-1]["output"]
    second = await attempt_call(2, retry_message(user, rejected, first))
    if isinstance(second, SpecialistOutput):
        return finish(second, None, 2)
    return finish(None, second, 2)
