"""Validador da proposta do especialista (Issue Triage Spec, seção 3; D24 e D25).

Oito checagens sem LLM, em ordem, parando na primeira que reprova:

1. schema (`SpecialistOutput`, `answer` só no domínio e `ref` de `issue` só `title` ou
   `description`) → `schema_invalid`;
2. `category` igual à do roteador → `category_mismatch`;
3. evidência `issue` literal no título, ou na descrição e nos enriquecimentos → `ungrounded_claim`;
4. evidência `catalog` literal no arquivo citado, lido nesta análise → `ungrounded_claim`;
5. evidência do catálogo exigida → `no_catalog_evidence`;
6. caminhos, nomes de código e `@menções` dos textos livres → `ungrounded_claim`;
7. ferramentas da lista do especialista → `tool_not_allowed`;
8. token do prompt de sistema fora da saída → `prompt_leak`.

As comparações usam a normalização do roteador (D21): desfaz `&lt;` e `&gt;`, colapsa espaços e
ignora maiúsculas. Os detalhes da recusa não repetem texto do modelo, porque voltam ao prompt da
segunda chance fora dos blocos de dados.
"""

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from issue_triage.adapters.catalog_tools import ToolError, read_doc_text
from issue_triage.domain.router import normalize_match
from issue_triage.domain.specialist import (
    READ_DOC,
    SEARCH_DOCS,
    SPECIALIST_TOOLS,
    SpecialistName,
    SpecialistOutput,
)

SCHEMA_INVALID = "schema_invalid"
CATEGORY_MISMATCH = "category_mismatch"
UNGROUNDED_CLAIM = "ungrounded_claim"
NO_CATALOG_EVIDENCE = "no_catalog_evidence"
TOOL_NOT_ALLOWED = "tool_not_allowed"
PROMPT_LEAK = "prompt_leak"

ISSUE_REFS = ("title", "description")

# Checagem 6 (D24): caminho de arquivo, nome de código entre crases e `@menção`.
FILE_PATH = re.compile(r"[\w./-]+\.(md|py|java|ts|js|sql|ya?ml|json|xml|properties)\b", re.I)
CODE_NAME = re.compile(r"`([^`]+)`")
MENTION = re.compile(r"@[\w-]+")

_FREE_TEXT_FIELDS = ("summary", "next_step", "conclusion_reason", "answer")


@dataclass(frozen=True)
class Rejection:
    """Proposta recusada: `code` é o `validation_error` e `detail` explica o motivo."""

    code: str
    detail: str

    @property
    def error(self) -> str:
        """Texto gravado em `analysis_attempts.error` (D24)."""
        return f"specialist: {self.code}: {self.detail}"


@dataclass(frozen=True)
class ValidationContext:
    """O que o validador compara com a saída do especialista numa tentativa.

    `title`, `description` e `enrichments` são o texto analisado na tentativa (D14);
    `docs_dir` é a pasta do caso; `token` é o código de verificação do prompt de sistema.
    """

    specialist: SpecialistName
    category: str
    title: str
    description: str
    enrichments: tuple[str, ...]
    docs_dir: Path
    responsible: str
    token: str


_SCHEMA_DETAILS = {
    "missing": "campo {field} ausente",
    "literal_error": "campo {field} com valor fora da lista permitida",
    "string_too_long": "campo {field} acima do tamanho máximo",
    "string_too_short": "campo {field} vazio",
    "too_long": "campo {field} com itens demais",
    "string_type": "campo {field} não é texto",
    "list_type": "campo {field} não é lista",
    "extra_forbidden": "campo {field} não previsto",
}


def _schema_detail(exc: ValidationError) -> str:
    details = []
    for error in exc.errors():
        field = ".".join(str(part) for part in error["loc"]) or "resposta"
        template = _SCHEMA_DETAILS.get(error["type"], "campo {field} inválido")
        details.append(template.format(field=field))
    return "; ".join(dict.fromkeys(details))


def check_schema(raw: Any, specialist: SpecialistName) -> SpecialistOutput | Rejection:
    """Checagem 1: schema, `answer` conforme o especialista e `ref` das evidências `issue`.

    Saída nula (o modelo terminou sem saída estruturada, inclusive com
    `error_max_structured_output_retries`) é `schema_invalid` (D23).
    """
    if raw is None:
        return Rejection(SCHEMA_INVALID, "o especialista terminou sem saída estruturada")
    try:
        output = SpecialistOutput.model_validate(raw)
    except ValidationError as exc:
        return Rejection(SCHEMA_INVALID, _schema_detail(exc))
    if specialist == "dominio" and output.answer is None:
        return Rejection(SCHEMA_INVALID, "campo answer nulo no especialista de domínio")
    if specialist != "dominio" and output.answer is not None:
        return Rejection(SCHEMA_INVALID, "campo answer preenchido fora do especialista de domínio")
    for index, item in enumerate(output.evidence):
        if item.source == "issue" and item.ref not in ISSUE_REFS:
            return Rejection(
                SCHEMA_INVALID, f"evidence.{index}.ref de issue diferente de title e description"
            )
    return output


def _contains(haystacks: Sequence[str], needle: str) -> bool:
    target = normalize_match(needle)
    if not target:  # quote só de espaços não ancora nada (D26)
        return False
    return any(target in normalize_match(text) for text in haystacks)


def _check_issue_evidence(output: SpecialistOutput, ctx: ValidationContext) -> Rejection | None:
    for index, item in enumerate(output.evidence):
        if item.source != "issue":
            continue
        if item.ref == "title":
            if not _contains([ctx.title], item.quote):
                return Rejection(UNGROUNDED_CLAIM, f"evidence.{index} não aparece no título")
        elif not _contains([ctx.description, *ctx.enrichments], item.quote):
            return Rejection(
                UNGROUNDED_CLAIM,
                f"evidence.{index} não aparece na descrição nem nos enriquecimentos",
            )
    return None


def files_read(tool_calls: Sequence[dict[str, Any]]) -> set[str]:
    """Arquivos lidos na análise: `paths` de `read_doc` e `search_docs` sem erro (D24, D25)."""
    read: set[str] = set()
    for call in tool_calls:
        if call.get("name") in (READ_DOC, SEARCH_DOCS) and call.get("error") is None:
            read.update(call.get("paths") or [])
    return read


def _check_catalog_evidence(
    output: SpecialistOutput, ctx: ValidationContext, tool_calls: Sequence[dict[str, Any]]
) -> Rejection | None:
    read = files_read(tool_calls)
    for index, item in enumerate(output.evidence):
        if item.source != "catalog":
            continue
        if item.ref not in read:
            return Rejection(
                UNGROUNDED_CLAIM, f"evidence.{index}: o arquivo citado não foi lido nesta análise"
            )
        try:
            text = read_doc_text(ctx.docs_dir, item.ref)
        except ToolError:
            return Rejection(
                UNGROUNDED_CLAIM, f"evidence.{index}: o arquivo citado não está na pasta do projeto"
            )
        if not _contains([text], item.quote):
            return Rejection(UNGROUNDED_CLAIM, f"evidence.{index} não aparece no arquivo citado")
    return None


def _check_catalog_required(output: SpecialistOutput) -> Rejection | None:
    has_catalog = any(item.source == "catalog" for item in output.evidence)
    if has_catalog:
        return None
    if output.answer is not None:
        return Rejection(NO_CATALOG_EVIDENCE, "answer preenchida sem evidência do catálogo")
    if output.conclusion != "sem_informacao":
        return Rejection(
            NO_CATALOG_EVIDENCE, f"nenhuma evidência do catálogo com conclusion {output.conclusion}"
        )
    return None


def _same_mention(mention: str, responsible: str) -> bool:
    return mention.lstrip("@").casefold() == responsible.strip().lstrip("@").casefold()


def _check_free_text(output: SpecialistOutput, ctx: ValidationContext) -> Rejection | None:
    grounds = [item.quote for item in output.evidence] + [item.ref for item in output.evidence]
    for field in _FREE_TEXT_FIELDS:
        text = getattr(output, field)
        if text is None:
            continue
        for match in FILE_PATH.finditer(text):
            if not _contains(grounds, match.group(0)):
                return Rejection(
                    UNGROUNDED_CLAIM,
                    f"{field} cita um caminho de arquivo que não está nas evidências",
                )
        for match in CODE_NAME.finditer(text):
            if not _contains(grounds, match.group(1)):
                return Rejection(
                    UNGROUNDED_CLAIM, f"{field} cita um nome de código que não está nas evidências"
                )
        for match in MENTION.finditer(text):
            if not _same_mention(match.group(0), ctx.responsible):
                return Rejection(
                    UNGROUNDED_CLAIM,
                    f"{field} menciona com @ alguém que não é o responsável do projeto",
                )
    return None


def _check_tools(ctx: ValidationContext, tool_calls: Sequence[dict[str, Any]]) -> Rejection | None:
    allowed = SPECIALIST_TOOLS[ctx.specialist]
    for call in tool_calls:
        if call.get("name") not in allowed:
            return Rejection(
                TOOL_NOT_ALLOWED, f"ferramenta fora da lista do especialista {ctx.specialist}"
            )
    return None


def _check_token(raw: Any, ctx: ValidationContext) -> Rejection | None:
    if ctx.token in json.dumps(raw, ensure_ascii=False):
        return Rejection(PROMPT_LEAK, "a saída repete o código de verificação da análise")
    return None


def validate(
    raw: Any, *, ctx: ValidationContext, tool_calls: Sequence[dict[str, Any]]
) -> SpecialistOutput | Rejection:
    """Aplica as oito checagens em ordem e devolve a saída aprovada ou a primeira recusa.

    `tool_calls` são as chamadas de ferramenta de todas as chamadas do especialista na
    tentativa (checagens 4 e 7, D24 e D25).
    """
    output = check_schema(raw, ctx.specialist)
    if isinstance(output, Rejection):
        return output
    if output.category != ctx.category:
        return Rejection(CATEGORY_MISMATCH, "category diferente da categoria do roteador")
    for check in (
        lambda: _check_issue_evidence(output, ctx),
        lambda: _check_catalog_evidence(output, ctx, tool_calls),
        lambda: _check_catalog_required(output),
        lambda: _check_free_text(output, ctx),
        lambda: _check_tools(ctx, tool_calls),
        lambda: _check_token(raw, ctx),
    ):
        rejection = check()
        if rejection is not None:
            return rejection
    return output
