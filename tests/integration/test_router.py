"""Roteador ligado ao fluxo (Etapa 6; RF12, RF13, RF18, RNF04, RNF09).

PostgreSQL real (testcontainers) e o `FakeRouterLLM` (D10); nenhuma chamada à Anthropic.
"""

import asyncio
import json
from collections.abc import AsyncIterator, Iterator

import psycopg
import pytest
import structlog
from psycopg_pool import AsyncConnectionPool

from issue_triage.adapters import triage_store
from issue_triage.adapters.database import close_pool, create_pool, open_pool
from issue_triage.domain.messages import Outcome
from tests.conftest import TestData
from tests.fakes import (
    FakeRouterLLM,
    FakeSpecialistRunner,
    SpecialistTurn,
    decision,
    make_processor,
)
from tests.integration.test_analysis import attempts, case_row, initial, manual, outbox, proposals

ORDERS = "Yaane-h/mechanical-hub-orders"


@pytest.fixture
async def pool(database_url: str) -> AsyncIterator[AsyncConnectionPool]:
    pool = create_pool(database_url)
    await open_pool(pool)
    try:
        yield pool
    finally:
        await close_pool(pool)


@pytest.fixture
def db(database_url: str) -> Iterator[psycopg.Connection]:
    with psycopg.connect(database_url, autocommit=True) as conn:
        yield conn


def _limit_turn() -> SpecialistTurn:
    """Especialista que estoura o limite de turnos: proposta INVALID, sem segunda chance."""
    return SpecialistTurn(stop_reason="limit_exceeded:turns")


def body(message: dict) -> bytes:
    return json.dumps(message).encode()


def route_decisions(db: psycopg.Connection, event_id: str) -> list[tuple]:
    return db.execute(
        "SELECT a.attempt, rd.category, rd.reason, rd.evidence, rd.llm_calls, "
        "rd.prompt_version, rd.model "
        "FROM triage.route_decisions rd JOIN triage.analysis_attempts a ON a.id = rd.attempt_id "
        "WHERE a.event_id = %s ORDER BY a.attempt",
        (event_id,),
    ).fetchall()


def attempt_errors(db: psycopg.Connection, event_id: str) -> list[str | None]:
    return [
        row[0]
        for row in db.execute(
            "SELECT error FROM triage.analysis_attempts WHERE event_id = %s ORDER BY attempt",
            (event_id,),
        ).fetchall()
    ]


@pytest.mark.parametrize(
    ("title", "description"),
    [
        ("Problema", "Não está legal."),
        ("Várias coisas", "O status não muda, quero exportar em CSV e mudar a cor do botão."),
        ("Ordem", "Alguma coisa com a ordem."),
    ],
    ids=["vague", "mixed-requests", "no-signal"],
)
async def test_indeterminado_ends_in_review_without_proposal(
    pool, committed_data: TestData, db, title: str, description: str
) -> None:
    # Casos H01 a H03: o roteador não consegue decidir a categoria.
    origin = committed_data.origin_event(repository=ORDERS, title=title, description=description)
    router = FakeRouterLLM([decision("indeterminado", "", reason="Texto sem pedido claro.")])
    processor = make_processor(pool, router=router)

    with structlog.testing.capture_logs() as logs:
        outcome = await processor.process(body(initial(origin)))

    assert outcome is Outcome.ACK
    assert case_row(db, origin.event_id) == {
        "status": "NEEDS_REVIEW",
        "status_reason": "insufficient_content",
        "current_attempt": 1,
        "manual_attempts": 0,
        "category": "indeterminado",
    }
    assert attempts(db, origin.event_id) == [(1, "initial", None, "DONE", "NEEDS_REVIEW")]
    assert attempt_errors(db, origin.event_id) == [None]
    assert route_decisions(db, origin.event_id) == [
        (1, "indeterminado", "Texto sem pedido claro.", "", 1, "router-v1", "fake-router-model")
    ]
    assert proposals(db, origin.event_id) == []
    assert outbox(db, origin.event_id) == []
    routed = [entry for entry in logs if entry["event"] == "triage.routed"]
    assert [(e["category"], e["llm_calls"]) for e in routed] == [("indeterminado", 1)]
    assert description not in repr(logs)


async def test_category_is_saved_with_the_decision_and_redelivery_reuses_it(
    pool, committed_data: TestData, db
) -> None:
    origin = committed_data.origin_event(
        repository=ORDERS, title="Exportar relatório", description="Adicionar exportação em CSV."
    )
    router = FakeRouterLLM([decision("melhoria", "exportação em CSV")])
    # A primeira entrega cai no especialista (processo derrubado; a mensagem volta).
    specialist = FakeSpecialistRunner([asyncio.CancelledError(), _limit_turn()])
    processor = make_processor(pool, router=router, specialist=specialist)

    with pytest.raises(asyncio.CancelledError):
        await processor.process(body(initial(origin)))
    case = case_row(db, origin.event_id)
    assert (case["status"], case["status_reason"], case["category"]) == (
        "ANALYZING",
        "initial",
        "melhoria",
    )
    assert attempts(db, origin.event_id) == [(1, "initial", None, "IN_PROGRESS", None)]

    again = await processor.process(body(initial(origin)))  # a mensagem voltou para a fila

    assert again is Outcome.ACK
    assert len(router.calls) == 1
    assert len(specialist.calls) == 2
    assert [row[1:5] for row in route_decisions(db, origin.event_id)] == [
        ("melhoria", "Motivo da categoria.", "exportação em CSV", 1)
    ]
    assert case_row(db, origin.event_id)["category"] == "melhoria"


async def test_resume_with_stored_indeterminado_decision_does_not_call_the_router(
    pool, committed_data: TestData, db, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin = committed_data.origin_event(repository=ORDERS, description="Não está legal.")
    # O processo caiu depois do commit 2: decisão gravada e tentativa ainda IN_PROGRESS.
    crashed = FakeRouterLLM([decision("indeterminado", "")])
    processor = make_processor(pool, router=crashed)
    with monkeypatch.context() as patch:
        patch.setattr(triage_store, "finish_attempt", _crash)
        with pytest.raises(SystemExit):
            await processor.process(body(initial(origin)))
    assert attempts(db, origin.event_id) == [(1, "initial", None, "IN_PROGRESS", None)]
    assert case_row(db, origin.event_id)["category"] == "indeterminado"

    untouched = FakeRouterLLM()  # sem respostas: qualquer chamada falha o teste
    processor = make_processor(pool, router=untouched)
    with structlog.testing.capture_logs() as logs:
        outcome = await processor.process(body(initial(origin)))

    assert outcome is Outcome.ACK
    assert untouched.calls == []
    assert case_row(db, origin.event_id)["status_reason"] == "insufficient_content"
    assert attempts(db, origin.event_id) == [(1, "initial", None, "DONE", "NEEDS_REVIEW")]
    events = [entry["event"] for entry in logs]
    assert "analysis.resumed" in events
    assert "triage.routed" not in events


async def _crash(*_args, **_kwargs):
    """Simula a queda do processo depois do commit 2 e antes do commit 3."""
    raise SystemExit("crash")


async def test_router_failing_twice_needs_review_with_error(
    pool, committed_data: TestData, db
) -> None:
    origin = committed_data.origin_event(
        repository=ORDERS, title="Erro 500", description="Ao enviar o formulário, recebo erro 500."
    )
    router = FakeRouterLLM([decision("bug", "banco caiu"), decision("bug", "timeout no banco")])
    processor = make_processor(pool, router=router)

    with structlog.testing.capture_logs() as logs:
        outcome = await processor.process(body(initial(origin)))
        again = await processor.process(body(initial(origin)))  # reentrega

    assert (outcome, again) == (Outcome.ACK, Outcome.ACK)
    assert len(router.calls) == 2
    assert case_row(db, origin.event_id) == {
        "status": "NEEDS_REVIEW",
        "status_reason": "invalid_proposal",
        "current_attempt": 1,
        "manual_attempts": 0,
        "category": None,
    }
    assert attempts(db, origin.event_id) == [(1, "initial", None, "DONE", "NEEDS_REVIEW")]
    assert attempt_errors(db, origin.event_id) == [
        "router: ungrounded_claim: evidence não aparece no título nem na descrição"
    ]
    assert route_decisions(db, origin.event_id) == []
    assert proposals(db, origin.event_id) == []
    assert outbox(db, origin.event_id) == []
    events = [entry["event"] for entry in logs]
    assert "triage.router_retry" in events
    invalid = [entry for entry in logs if entry["event"] == "triage.proposal_invalid"]
    assert [entry["validation_error"] for entry in invalid] == ["ungrounded_claim"]
    assert "analysis.skipped_duplicate" in events


async def test_router_retry_succeeds_with_two_calls(pool, committed_data: TestData, db) -> None:
    origin = committed_data.origin_event(
        repository=ORDERS, title="Como configuro?", description="Como configuro o token?"
    )
    router = FakeRouterLLM([{"category": "duvida"}, decision("duvida", "configuro o token")])
    specialist = FakeSpecialistRunner([_limit_turn()])
    processor = make_processor(pool, router=router, specialist=specialist)

    outcome = await processor.process(body(initial(origin)))

    assert outcome is Outcome.ACK
    assert specialist.calls[0].specialist == "dominio"
    assert [row[1:5] for row in route_decisions(db, origin.event_id)] == [
        ("duvida", "Motivo da categoria.", "configuro o token", 2)
    ]
    assert case_row(db, origin.event_id)["category"] == "duvida"


async def test_router_failure_at_the_manual_limit_closes_the_case(
    pool, committed_data: TestData, db
) -> None:
    origin = committed_data.origin_event(repository=ORDERS, description="Não está legal.")
    enrich = committed_data.review(
        origin,
        action="enrich",
        target_attempt=2,
        enrichment_description="A ordem 7 não sai de IN_PROGRESS.",
    )
    router = FakeRouterLLM(
        [decision("indeterminado", ""), {}, {}]  # tentativa 1 e as duas chamadas da tentativa 2
    )
    processor = make_processor(pool, router=router, max_manual_attempts=1)

    first = await processor.process(body(initial(origin)))
    second = await processor.process(body(manual(origin, "enrich", 2, enrich.id)))

    assert (first, second) == (Outcome.ACK, Outcome.ACK)
    # O enriquecimento entra no bloco próprio do roteador (D14).
    assert "<enriquecimento>\nA ordem 7 não sai de IN_PROGRESS.\n</enriquecimento>" in (
        router.calls[1].user
    )
    case = case_row(db, origin.event_id)
    assert (case["status"], case["status_reason"]) == (
        "MAX_RETRIES_REACHED",
        "manual_attempts_exhausted",
    )
    assert attempt_errors(db, origin.event_id) == [
        None,
        "router: schema_invalid: a resposta não chamou a ferramenta route_decision",
    ]


async def test_evidence_quoting_html_from_the_issue_is_accepted(
    pool, committed_data: TestData, db
) -> None:
    # Correção da D21: a evidência copiada de uma issue que cita `<div>` é aprovada (RF13).
    origin = committed_data.origin_event(
        repository=ORDERS,
        title="Formulário quebrado",
        description="Ao salvar a ordem, o <div> do formulário some da tela.",
    )
    router = FakeRouterLLM([decision("bug", "o &lt;div&gt; do formulário some")])
    specialist = FakeSpecialistRunner([_limit_turn()])
    processor = make_processor(pool, router=router, specialist=specialist)

    await processor.process(body(initial(origin)))

    assert len(router.calls) == 1
    assert [row[1:5] for row in route_decisions(db, origin.event_id)] == [
        ("bug", "Motivo da categoria.", "o &lt;div&gt; do formulário some", 1)
    ]


async def test_router_failing_twice_on_reprocess_keeps_the_last_category(
    pool, committed_data: TestData, db
) -> None:
    # Correção da D21 (item 2): a falha do roteador numa tentativa manual não apaga a categoria.
    origin = committed_data.origin_event(
        repository=ORDERS, title="Erro 500", description="Ao enviar o formulário, recebo erro 500."
    )
    router = FakeRouterLLM([decision("bug", "recebo erro 500"), decision("bug", "banco caiu"), {}])
    # O especialista da tentativa 1 estoura um limite: o caso vai para a revisão e o revisor
    # pede o reprocessamento.
    specialist = FakeSpecialistRunner([_limit_turn()])
    processor = make_processor(pool, router=router, specialist=specialist)
    assert await processor.process(body(initial(origin))) is Outcome.ACK
    reprocess = committed_data.review(
        origin, action="reprocess", target_attempt=2, from_reason="invalid_proposal"
    )

    outcome = await processor.process(body(manual(origin, "reprocess", 2, reprocess.id)))

    assert outcome is Outcome.ACK
    assert len(router.calls) == 3
    assert case_row(db, origin.event_id) == {
        "status": "NEEDS_REVIEW",
        "status_reason": "invalid_proposal",
        "current_attempt": 2,
        "manual_attempts": 1,
        "category": "bug",
    }
    assert attempts(db, origin.event_id) == [
        (1, "initial", None, "DONE", "NEEDS_REVIEW"),
        (2, "reprocess", str(reprocess.id), "DONE", "NEEDS_REVIEW"),
    ]
    assert attempt_errors(db, origin.event_id)[1] == (
        "router: schema_invalid: a resposta não chamou a ferramenta route_decision"
    )
    assert [row[0] for row in route_decisions(db, origin.event_id)] == [1]
