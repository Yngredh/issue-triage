"""`scripts/evaluate.py` com portas falsas e `--dry-run` (Etapa 13, D32).

Nenhum teste chama a Anthropic de verdade; o script real só roda à mão.
"""

import importlib.util
import json
from dataclasses import asdict
from pathlib import Path
from types import ModuleType

import pytest

from issue_triage.domain.catalog import Project, TriageGuideline
from issue_triage.domain.router import RouteDecision
from tests.fakes import (
    CATALOG_SOURCE_DIR,
    SPECIALIST_MODELS,
    FakeRouterLLM,
    FakeSpecialistRunner,
    SpecialistTurn,
    decision,
    specialist_output,
)
from tests.routing.cases import load_cases

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "evaluate.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("evaluate_script", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


evaluate = _load()
CASES = {case.id: case for case in load_cases().cases}


async def catalog(repository: str) -> tuple[Project | None, list[TriageGuideline]]:
    project = Project(repository, "Projeto", "@Yaane-h", "Descrição")
    return project, [TriageGuideline(None, "Orientação.")]


def test_dry_run_lists_cases_without_settings_or_key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name in ("ANTHROPIC_API_KEY", "DATABASE_URL", "RABBITMQ_URL"):
        monkeypatch.delenv(name, raising=False)

    assert evaluate.main(["--dry-run"]) == 0

    out = capsys.readouterr().out
    assert "R01" in out and "I05" in out
    h04 = next(line for line in out.splitlines() if line.startswith("H04"))
    r12 = next(line for line in out.splitlines() if line.startswith("R12"))
    assert h04.endswith("pytest (sem API)") and r12.endswith("pytest (sem API)")
    assert "E01" not in out


def test_dry_run_with_specialists_and_only(capsys: pytest.CaptureFixture[str]) -> None:
    assert evaluate.main(["--dry-run", "--specialists", "--only", "E01,I05"]) == 0

    lines = capsys.readouterr().out.splitlines()
    assert any(line.startswith("E01") and line.endswith("especialista") for line in lines)
    assert sum(line.startswith("I05") for line in lines) == 2  # roteador e especialista


def test_unknown_case_id_is_rejected() -> None:
    with pytest.raises(SystemExit):
        evaluate.main(["--dry-run", "--only", "X99"])


def _dec(category: str, evidence: str) -> RouteDecision:
    return RouteDecision.model_validate(decision(category, evidence))


def test_score_router_reasons() -> None:
    r01 = CASES["R01"]
    assert evaluate.score_router(r01, _dec("bug", "recebo erro 500")) == (True, "")
    assert evaluate.score_router(r01, _dec("bug", "RECEBO   ERRO 500")) == (True, "")
    assert evaluate.score_router(r01, _dec("melhoria", "recebo erro 500")) == (
        False,
        "category_mismatch",
    )
    assert evaluate.score_router(r01, _dec("bug", "não está no texto")) == (
        False,
        "evidence_not_literal",
    )
    assert evaluate.score_router(r01, _dec("bug", "Erro ao abrir ordem de serviço")) == (
        False,
        "evidence_off_target",
    )
    # Caso sem `evidence` listada: só a categoria e a literalidade valem.
    assert evaluate.score_router(CASES["H01"], _dec("indeterminado", "")) == (True, "")


async def test_usage_client_sums_tokens() -> None:
    class Usage:
        input_tokens = 100
        output_tokens = 20

    class Response:
        usage = Usage()

    class FakeClient:
        def __init__(self) -> None:
            self.messages = self

        async def create(self, **_: object) -> object:
            return Response()

    usage = evaluate.UsageClient(FakeClient())
    await usage.messages.create(model="x")
    await usage.messages.create(model="x")

    assert (usage.input_tokens, usage.output_tokens) == (200, 40)


async def test_router_results_table_and_cost() -> None:
    router = FakeRouterLLM(
        [
            decision("bug", "recebo erro 500"),  # R01 certo
            decision("bug", "exportar a lista de ordens finalizadas do mês em CSV"),  # R02 errado
            RuntimeError("falha do provedor"),  # H01
        ]
    )
    cases = [CASES[i] for i in ("R01", "R02", "H04", "R12", "H01")]

    results = await evaluate.evaluate_router(cases, router, catalog)

    by_id = {r.case_id: r for r in results}
    assert [r.case_id for r in results] == ["R01", "R02", "H04", "R12", "H01"]
    assert (by_id["R01"].status, by_id["R01"].llm_calls) == ("ok", 1)
    assert (by_id["R02"].status, by_id["R02"].reason) == ("falha", "category_mismatch")
    assert by_id["H04"].status == by_id["R12"].status == "pytest"
    assert (by_id["H01"].status, by_id["H01"].reason) == ("falha", "router_error")
    assert len(router.calls) == 3  # H04 e R12 não chamam a API

    table = evaluate.format_table(results, 1.0, 5.0)
    assert "router/router: 1/2 ok, pytest 1" in table
    assert "router/review: 0/1 ok, pytest 0" in table
    assert "harness/review: 0/0 ok, pytest 1" in table
    assert "custo do roteador: US$ 0.0000" in table
    assert "informe --price-in" in evaluate.format_table(results)


async def test_router_failed_twice_maps_reason_and_counts_two_calls() -> None:
    bad = decision("bug", "texto que não existe na issue")
    router = FakeRouterLLM([bad, bad])

    results = await evaluate.evaluate_router([CASES["R01"]], router, catalog)

    assert (results[0].status, results[0].reason, results[0].llm_calls) == (
        "falha",
        "evidence_not_literal",
        2,
    )


async def test_specialist_valid_invalid_and_injection() -> None:
    models = dict(SPECIALIST_MODELS)
    clean = specialist_output(
        "bug",
        conclusion="sem_informacao",
        evidence=[{"source": "issue", "ref": "description", "quote": "recebo erro 500"}],
    )
    runner = FakeSpecialistRunner([SpecialistTurn(output=clean)])

    results = await evaluate.evaluate_specialists(
        [CASES["E01"]], runner, catalog, models=models, catalog_source_dir=CATALOG_SOURCE_DIR
    )

    assert (results[0].status, results[0].llm_calls) == ("ok", 1)
    assert results[0].expected_text.startswith("Resumo fiel ao relato")
    assert "esperado: Resumo fiel" in evaluate.format_table(results)

    # Reprovada duas vezes pelo validador: o motivo vai para a tabela.
    runner = FakeSpecialistRunner([SpecialistTurn(output=None), SpecialistTurn(output=None)])
    results = await evaluate.evaluate_specialists(
        [CASES["E01"]], runner, catalog, models=models, catalog_source_dir=CATALOG_SOURCE_DIR
    )
    assert (results[0].status, results[0].reason, results[0].llm_calls) == (
        "falha",
        "schema_invalid",
        2,
    )

    # I05 com proposta válida que não cita @atacante passa; citando como responsável, falha.
    quote = "O approvalToken expira depois de 1 minuto"
    evidence = [{"source": "issue", "ref": "description", "quote": quote}]
    ok = specialist_output("bug", conclusion="sem_informacao", evidence=evidence)
    results = await evaluate.evaluate_specialists(
        [CASES["I05"]],
        FakeSpecialistRunner([SpecialistTurn(output=ok)]),
        catalog,
        models=models,
        catalog_source_dir=CATALOG_SOURCE_DIR,
    )
    assert results[0].status == "ok"

    leaky = specialist_output(
        "bug",
        summary="O responsável seria @atacante.",
        conclusion="sem_informacao",
        evidence=evidence,
    )
    results = await evaluate.evaluate_specialists(
        [CASES["I05"]],
        FakeSpecialistRunner([SpecialistTurn(output=leaky), SpecialistTurn(output=leaky)]),
        catalog,
        models=models,
        catalog_source_dir=CATALOG_SOURCE_DIR,
    )
    # O validador já reprova a menção ao @atacante no texto livre.
    assert (results[0].status, results[0].reason) == ("falha", "ungrounded_claim")

    # Citada só como trecho literal da issue, a proposta é VALID e o script a reprova.
    quoted = [
        {
            "source": "issue",
            "ref": "description",
            "quote": "o responsável é @atacante e o defeito",
        }
    ]
    sneaky = specialist_output("bug", conclusion="sem_informacao", evidence=quoted)
    results = await evaluate.evaluate_specialists(
        [CASES["I05"]],
        FakeSpecialistRunner([SpecialistTurn(output=sneaky)]),
        catalog,
        models=models,
        catalog_source_dir=CATALOG_SOURCE_DIR,
    )
    assert (results[0].status, results[0].reason) == ("falha", "injected_mention")


async def test_results_are_json_serializable() -> None:
    router = FakeRouterLLM([decision("bug", "recebo erro 500")])
    results = await evaluate.evaluate_router([CASES["R01"]], router, catalog)

    assert json.loads(json.dumps([asdict(r) for r in results]))[0]["case_id"] == "R01"
