"""Validador da proposta do especialista (Etapa 8; Issue Triage Spec, seção 3; D24, D25)."""

from pathlib import Path
from typing import Any

import pytest

from issue_triage.adapters.catalog_tools import CatalogTools
from issue_triage.domain.specialist import SpecialistOutput
from issue_triage.domain.validator import Rejection, ValidationContext, check_schema, validate
from tests.fakes import specialist_output

TOKEN = "0123456789abcdef"
DOC = "status.md"
DOC_TEXT = "# Status\n\nA ordem passa de   RECEIVED para\nIN_DIAGNOSIS quando o mecânico começa.\n"


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    (tmp_path / DOC).write_text(DOC_TEXT, encoding="utf-8")
    (tmp_path / "outro.md").write_text("Outro documento sobre pagamento.", encoding="utf-8")
    return tmp_path


def _ctx(folder: Path, specialist: str = "bug", category: str = "bug") -> ValidationContext:
    return ValidationContext(
        specialist=specialist,
        category=category,
        title="Ordem não muda de status",
        description="Ao mudar a ordem 12 para IN_DIAGNOSIS a API devolve <div>erro</div>.",
        enrichments=("O revisor confirmou o erro 500 na ordem 12.",),
        docs_dir=folder,
        responsible="@Yaane-h",
        token=TOKEN,
    )


def _calls(folder: Path, *calls: tuple[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Executa as ferramentas de verdade e devolve o registro de `tool_calls`."""
    tools = CatalogTools(folder, max_tool_calls=8, max_read_chars=20000)
    for name, arguments in calls:
        tools.call(name, arguments)
    return [{**item, "call": 1} for item in tools.tool_calls()]


CATALOG_EVIDENCE = {"source": "catalog", "ref": DOC, "quote": "A ordem passa de RECEIVED para"}
ISSUE_EVIDENCE = {"source": "issue", "ref": "title", "quote": "não muda de status"}


def _output(**changes: Any) -> dict[str, Any]:
    changes.setdefault("evidence", [ISSUE_EVIDENCE, CATALOG_EVIDENCE])
    return specialist_output(**changes)


def _read(folder: Path) -> list[dict[str, Any]]:
    return _calls(folder, ("read_doc", {"path": DOC}))


def _code(result: SpecialistOutput | Rejection) -> str | None:
    return result.code if isinstance(result, Rejection) else None


def test_valid_output_passes(folder: Path) -> None:
    result = validate(_output(), ctx=_ctx(folder), tool_calls=_read(folder))

    assert isinstance(result, SpecialistOutput)


# Checagem 1


@pytest.mark.parametrize("raw", [None, {}, {"category": "bug"}, "texto", []])
def test_missing_or_malformed_output_is_schema_invalid(folder: Path, raw: Any) -> None:
    # D23: saída nula (sem saída estruturada) é `schema_invalid`.
    assert _code(validate(raw, ctx=_ctx(folder), tool_calls=[])) == "schema_invalid"


def test_null_output_detail() -> None:
    rejection = check_schema(None, "bug")

    assert rejection == Rejection("schema_invalid", "o especialista terminou sem saída estruturada")
    assert rejection.error == (
        "specialist: schema_invalid: o especialista terminou sem saída estruturada"
    )


def test_answer_outside_domain_is_schema_invalid(folder: Path) -> None:
    result = validate(_output(answer="Resposta."), ctx=_ctx(folder), tool_calls=_read(folder))

    assert result == Rejection(
        "schema_invalid", "campo answer preenchido fora do especialista de domínio"
    )


def test_null_answer_in_domain_is_schema_invalid(folder: Path) -> None:
    ctx = _ctx(folder, "dominio", "duvida")

    result = validate(_output(category="duvida"), ctx=ctx, tool_calls=_read(folder))

    assert _code(result) == "schema_invalid"


def test_issue_ref_other_than_title_or_description_is_schema_invalid(folder: Path) -> None:
    evidence = [{"source": "issue", "ref": "comentario", "quote": "não muda"}, CATALOG_EVIDENCE]

    result = validate(_output(evidence=evidence), ctx=_ctx(folder), tool_calls=_read(folder))

    assert _code(result) == "schema_invalid"


# Checagem 2


def test_category_different_from_the_router_is_category_mismatch(folder: Path) -> None:
    result = validate(_output(category="melhoria"), ctx=_ctx(folder), tool_calls=_read(folder))

    assert _code(result) == "category_mismatch"


# Checagem 3


@pytest.mark.parametrize(
    "evidence",
    [
        {"source": "issue", "ref": "title", "quote": "a API devolve"},  # está na descrição
        {"source": "issue", "ref": "description", "quote": "o banco caiu"},
        {"source": "issue", "ref": "description", "quote": "Ordem não muda de status"},
    ],
    ids=["title-from-description", "nonexistent", "description-from-title"],
)
def test_issue_evidence_not_found_is_ungrounded(folder: Path, evidence: dict) -> None:
    output = _output(evidence=[evidence, CATALOG_EVIDENCE])

    assert _code(validate(output, ctx=_ctx(folder), tool_calls=_read(folder))) == (
        "ungrounded_claim"
    )


@pytest.mark.parametrize(
    "evidence",
    [
        {"source": "issue", "ref": "title", "quote": "ORDEM   NÃO muda"},
        {"source": "issue", "ref": "description", "quote": "devolve &lt;div&gt;erro"},
        {"source": "issue", "ref": "description", "quote": "devolve <div>erro"},
        {"source": "issue", "ref": "description", "quote": "confirmou o erro 500"},
    ],
    ids=["spaces-and-case", "escaped-html", "html", "enrichment"],
)
def test_issue_evidence_uses_the_router_normalization(folder: Path, evidence: dict) -> None:
    # D24 (item 6): espaços, maiúsculas, `&lt;`/`&gt;`; `description` inclui os enriquecimentos.
    output = _output(evidence=[evidence, CATALOG_EVIDENCE])

    assert isinstance(
        validate(output, ctx=_ctx(folder), tool_calls=_read(folder)), SpecialistOutput
    )


# Checagem 4


def test_catalog_evidence_from_a_file_not_read_is_ungrounded(folder: Path) -> None:
    calls = _calls(folder, ("read_doc", {"path": "outro.md"}))

    result = validate(_output(), ctx=_ctx(folder), tool_calls=calls)

    assert result == Rejection(
        "ungrounded_claim", "evidence.1: o arquivo citado não foi lido nesta análise"
    )


def test_catalog_evidence_from_a_failed_read_is_ungrounded(folder: Path) -> None:
    calls = [{**_read(folder)[0], "error": "limit_exceeded", "paths": []}]

    assert _code(validate(_output(), ctx=_ctx(folder), tool_calls=calls)) == "ungrounded_claim"


def test_catalog_evidence_from_a_search_snippet_counts_as_read(folder: Path) -> None:
    calls = _calls(folder, ("search_docs", {"query": "received"}))

    result = validate(_output(), ctx=_ctx(folder), tool_calls=calls)

    assert isinstance(result, SpecialistOutput)


def test_catalog_quote_is_searched_in_the_whole_file_with_normalization(folder: Path) -> None:
    # D25 (b): o trecho cruza linhas e espaços repetidos do arquivo; maiúsculas não importam.
    quote = "received para in_diagnosis quando"
    evidence = [ISSUE_EVIDENCE, {"source": "catalog", "ref": DOC, "quote": quote}]

    result = validate(_output(evidence=evidence), ctx=_ctx(folder), tool_calls=_read(folder))

    assert isinstance(result, SpecialistOutput)


def test_catalog_quote_not_in_the_file_is_ungrounded(folder: Path) -> None:
    evidence = [ISSUE_EVIDENCE, {"source": "catalog", "ref": DOC, "quote": "status inventado"}]

    result = validate(_output(evidence=evidence), ctx=_ctx(folder), tool_calls=_read(folder))

    assert result == Rejection("ungrounded_claim", "evidence.1 não aparece no arquivo citado")


def test_catalog_evidence_with_path_outside_the_folder_is_ungrounded(folder: Path) -> None:
    # Mesmo que o registro diga que o arquivo foi lido, o caminho precisa ser da pasta do caso.
    calls = [{**_read(folder)[0], "paths": ["../status.md"]}]
    evidence = [ISSUE_EVIDENCE, {"source": "catalog", "ref": "../status.md", "quote": "Status"}]

    result = validate(_output(evidence=evidence), ctx=_ctx(folder), tool_calls=calls)

    assert _code(result) == "ungrounded_claim"


# Checagem 5


def test_procede_without_catalog_evidence_is_rejected(folder: Path) -> None:
    result = validate(
        _output(evidence=[ISSUE_EVIDENCE]), ctx=_ctx(folder), tool_calls=_read(folder)
    )

    assert _code(result) == "no_catalog_evidence"


def test_sem_informacao_without_catalog_evidence_passes(folder: Path) -> None:
    # Caso E04: a documentação não cobre o ponto.
    output = _output(conclusion="sem_informacao", evidence=[ISSUE_EVIDENCE])

    assert isinstance(validate(output, ctx=_ctx(folder), tool_calls=[]), SpecialistOutput)


def test_answer_without_catalog_evidence_is_rejected_even_with_sem_informacao(
    folder: Path,
) -> None:
    ctx = _ctx(folder, "dominio", "duvida")
    output = _output(
        category="duvida",
        conclusion="sem_informacao",
        answer="A ordem muda sozinha.",
        evidence=[ISSUE_EVIDENCE],
    )

    result = validate(output, ctx=ctx, tool_calls=[])

    assert result == Rejection("no_catalog_evidence", "answer preenchida sem evidência do catálogo")


# Checagem 6


@pytest.mark.parametrize(
    ("field", "text"),
    [
        ("summary", "O erro vem de src/orders/StatusService.java."),
        ("next_step", "Conferir o arquivo config/application.yml."),
        ("conclusion_reason", "Ver o `OrderStatusMachine`."),
        ("summary", "Avisar @fulano sobre o erro."),
    ],
    ids=["invented-path", "invented-config", "invented-code-name", "third-party-mention"],
)
def test_free_text_claims_without_evidence_are_ungrounded(
    folder: Path, field: str, text: str
) -> None:
    # Caminho inventado e `@menção` de terceiro (caso I05).
    result = validate(_output(**{field: text}), ctx=_ctx(folder), tool_calls=_read(folder))

    assert _code(result) == "ungrounded_claim"


def test_free_text_claims_backed_by_evidence_pass(folder: Path) -> None:
    output = _output(
        summary="A ordem sai de `RECEIVED`, como descrito em status.md.",
        next_step="@yaane-h deve conferir a ordem 12.",
    )

    assert isinstance(
        validate(output, ctx=_ctx(folder), tool_calls=_read(folder)), SpecialistOutput
    )


def test_answer_is_checked_for_free_text_claims(folder: Path) -> None:
    ctx = _ctx(folder, "dominio", "duvida")
    output = _output(category="duvida", answer="Veja docs/manual.md.")

    assert _code(validate(output, ctx=ctx, tool_calls=_read(folder))) == "ungrounded_claim"


# Checagem 7


def test_tool_outside_the_specialist_list_is_rejected(folder: Path) -> None:
    # O especialista de bug não tem `list_docs`.
    calls = _calls(folder, ("list_docs", {}), ("read_doc", {"path": DOC}))

    result = validate(_output(), ctx=_ctx(folder), tool_calls=calls)

    assert result == Rejection("tool_not_allowed", "ferramenta fora da lista do especialista bug")


# Checagem 8


def test_token_in_the_output_is_prompt_leak(folder: Path) -> None:
    output = _output(next_step=f"Código {TOKEN}.")

    result = validate(output, ctx=_ctx(folder), tool_calls=_read(folder))

    assert _code(result) == "prompt_leak"
    assert TOKEN not in result.detail


def test_checks_stop_at_the_first_failure(folder: Path) -> None:
    # Categoria errada (2) e vazamento (8): vale a primeira.
    output = _output(category="melhoria", summary=f"Token {TOKEN}")

    assert _code(validate(output, ctx=_ctx(folder), tool_calls=[])) == "category_mismatch"
