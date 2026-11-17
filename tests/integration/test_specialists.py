"""Especialistas ligados ao fluxo: proposta, commit 3 e contexto acumulado (Etapa 8).

PostgreSQL real (testcontainers), `FakeRouterLLM` e `FakeSpecialistRunner` (D10) com a Base de
conhecimento real de `catalog/source`; nenhuma chamada à Anthropic.
"""

import json
from collections.abc import AsyncIterator, Iterator
from typing import Any

import psycopg
import pytest
import structlog
from psycopg_pool import AsyncConnectionPool

from issue_triage.adapters.database import close_pool, create_pool, open_pool
from issue_triage.domain.messages import Outcome
from tests.conftest import TestData
from tests.fakes import (
    FakeRouterLLM,
    FakeSpecialistRunner,
    SpecialistTurn,
    decision,
    make_processor,
    specialist_output,
)
from tests.integration.test_analysis import attempts, case_row, initial, manual, outbox

ORDERS = "Yaane-h/mechanical-hub-orders"
DOC = "order_status.md"
QUOTE = "Mecânico iniciou a execução dos reparos."
TITLE = "Ordem não muda de status"
DESCRIPTION = "Ao mudar a ordem 12 para IN_PROGRESS a API devolve 500."
READ = (("read_doc", {"path": DOC}),)


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


def body(message: dict) -> bytes:
    return json.dumps(message).encode()


def valid_output(**changes: Any) -> dict[str, Any]:
    changes.setdefault(
        "evidence",
        [
            {"source": "issue", "ref": "description", "quote": "a API devolve 500"},
            {"source": "catalog", "ref": DOC, "quote": QUOTE},
        ],
    )
    return specialist_output(**changes)


def proposal_rows(db: psycopg.Connection, event_id: str) -> list[dict[str, Any]]:
    cur = db.execute(
        "SELECT a.attempt, p.kind, p.specialist, p.category, p.summary, p.next_step, "
        "p.conclusion, p.conclusion_reason, p.answer, p.evidence, p.tool_calls, p.raw_output, "
        "p.validation_status, p.validation_error, p.comment_body, p.prompt_version, p.model "
        "FROM triage.proposals p JOIN triage.analysis_attempts a ON a.id = p.attempt_id "
        "WHERE a.event_id = %s ORDER BY a.attempt",
        (event_id,),
    )
    names = [column.name for column in cur.description]
    return [dict(zip(names, row, strict=True)) for row in cur.fetchall()]


def attempt_errors(db: psycopg.Connection, event_id: str) -> list[str | None]:
    return [
        row[0]
        for row in db.execute(
            "SELECT error FROM triage.analysis_attempts WHERE event_id = %s ORDER BY attempt",
            (event_id,),
        ).fetchall()
    ]


async def test_approved_proposal_is_ready_to_publish_with_outbox(
    pool, committed_data: TestData, db
) -> None:
    origin = committed_data.origin_event(repository=ORDERS, title=TITLE, description=DESCRIPTION)
    router = FakeRouterLLM([decision("bug", "a API devolve 500")])
    specialist = FakeSpecialistRunner([SpecialistTurn(output=valid_output(), tool_calls=READ)])
    processor = make_processor(pool, router=router, specialist=specialist)

    with structlog.testing.capture_logs() as logs:
        outcome = await processor.process(body(initial(origin)))
        again = await processor.process(body(initial(origin)))  # reentrega

    assert (outcome, again) == (Outcome.ACK, Outcome.ACK)
    assert len(specialist.calls) == 1
    assert case_row(db, origin.event_id) == {
        "status": "READY_TO_PUBLISH",
        "status_reason": "proposal_validated",
        "current_attempt": 1,
        "manual_attempts": 0,
        "category": "bug",
    }
    assert attempts(db, origin.event_id) == [(1, "initial", None, "DONE", "READY_TO_PUBLISH")]
    assert attempt_errors(db, origin.event_id) == [None]
    [proposal] = proposal_rows(db, origin.event_id)
    assert proposal["kind"] == "triage"
    assert proposal["validation_status"] == "VALID"
    assert proposal["validation_error"] is None
    assert (proposal["specialist"], proposal["prompt_version"], proposal["model"]) == (
        "bug",
        "bug-v1",
        "fake-bug-model",
    )
    assert (proposal["category"], proposal["summary"], proposal["conclusion"]) == (
        "bug",
        "Resumo do relato.",
        "procede",
    )
    assert proposal["evidence"] == valid_output()["evidence"]
    assert proposal["raw_output"] == [
        {"call": 1, "output": valid_output(), "validation_error": None}
    ]
    [call] = proposal["tool_calls"]
    assert (call["name"], call["call"], call["paths"], call["error"]) == (
        "read_doc",
        1,
        [DOC],
        None,
    )
    comment = proposal["comment_body"]
    assert comment.startswith("## Triagem automática: Bug\n")
    assert "**Responsável:** @Yaane-h" in comment
    assert '- "Mecânico iniciou a execução dos reparos." (order_status.md)' in comment
    assert comment.endswith(f"<!-- triage:{origin.event_id}:1 -->")
    assert comment.count("<!-- triage:") == 1
    [row] = outbox(db, origin.event_id)
    assert row[1] == f"{ORDERS}#{origin.issue_number}:1"
    assert row[2]["revision"] == 1
    events = [entry["event"] for entry in logs]
    assert "triage.proposal_validated" in events
    assert "analysis.skipped_duplicate" in events
    assert DESCRIPTION not in repr(logs)


async def test_rejected_twice_needs_review_with_invalid_proposal(
    pool, committed_data: TestData, db
) -> None:
    origin = committed_data.origin_event(repository=ORDERS, title=TITLE, description=DESCRIPTION)
    router = FakeRouterLLM([decision("bug", "a API devolve 500")])
    # 1ª: categoria errada; 2ª: passa na checagem 1, mas cita um arquivo que não foi lido.
    first = valid_output(category="melhoria")
    second = valid_output(summary="Segunda saída.")
    specialist = FakeSpecialistRunner([SpecialistTurn(output=first), SpecialistTurn(output=second)])
    processor = make_processor(pool, router=router, specialist=specialist)

    with structlog.testing.capture_logs() as logs:
        outcome = await processor.process(body(initial(origin)))

    assert outcome is Outcome.ACK
    assert len(specialist.calls) == 2
    case = case_row(db, origin.event_id)
    assert (case["status"], case["status_reason"], case["category"]) == (
        "NEEDS_REVIEW",
        "invalid_proposal",
        "bug",
    )
    assert attempts(db, origin.event_id) == [(1, "initial", None, "DONE", "NEEDS_REVIEW")]
    assert attempt_errors(db, origin.event_id) == [
        "specialist: ungrounded_claim: evidence.1: o arquivo citado não foi lido nesta análise"
    ]
    [proposal] = proposal_rows(db, origin.event_id)
    assert proposal["validation_status"] == "INVALID"
    assert proposal["validation_error"] == "ungrounded_claim"
    assert proposal["comment_body"] is None
    # D25 (item 3): colunas da última saída, que passou na checagem 1.
    assert (proposal["summary"], proposal["category"]) == ("Segunda saída.", "bug")
    assert proposal["evidence"] == second["evidence"]
    assert proposal["raw_output"] == [
        {"call": 1, "output": first, "validation_error": "category_mismatch"},
        {"call": 2, "output": second, "validation_error": "ungrounded_claim"},
    ]
    assert outbox(db, origin.event_id) == []
    events = [entry["event"] for entry in logs]
    assert "triage.specialist_retry" in events
    invalid = [entry for entry in logs if entry["event"] == "triage.proposal_invalid"]
    assert [(entry["validation_error"], entry["stage"]) for entry in invalid] == [
        ("ungrounded_claim", "specialist")
    ]


async def test_limit_exceeded_is_invalid_without_columns(
    pool, committed_data: TestData, db
) -> None:
    origin = committed_data.origin_event(repository=ORDERS, title=TITLE, description=DESCRIPTION)
    router = FakeRouterLLM([decision("bug", "a API devolve 500")])
    specialist = FakeSpecialistRunner(
        [SpecialistTurn(tool_calls=READ, stop_reason="limit_exceeded:budget")]
    )
    processor = make_processor(pool, router=router, specialist=specialist)

    assert await processor.process(body(initial(origin))) is Outcome.ACK

    assert len(specialist.calls) == 1
    [proposal] = proposal_rows(db, origin.event_id)
    assert proposal["validation_status"] == "INVALID"
    assert proposal["validation_error"] == "limit_exceeded:budget"
    assert proposal["summary"] is None
    assert proposal["category"] is None
    assert proposal["evidence"] == []
    assert [call["call"] for call in proposal["tool_calls"]] == [1]
    assert attempt_errors(db, origin.event_id) == [
        "specialist: limit_exceeded:budget: a sessão do especialista passou do limite de custo"
    ]


async def test_enrich_carries_the_previous_proposal_and_the_enrichment(
    pool, committed_data: TestData, db
) -> None:
    # RF31: a nova tentativa leva o que já se sabe do caso (D14, D24).
    origin = committed_data.origin_event(repository=ORDERS, title=TITLE, description=DESCRIPTION)
    router = FakeRouterLLM(
        [decision("bug", "a API devolve 500"), decision("bug", "a API devolve 500")]
    )
    rejected = valid_output(category="melhoria")
    specialist = FakeSpecialistRunner(
        [
            SpecialistTurn(output=rejected),
            SpecialistTurn(output=rejected),
            SpecialistTurn(output=valid_output(), tool_calls=READ),
        ]
    )
    processor = make_processor(pool, router=router, specialist=specialist)
    assert await processor.process(body(initial(origin))) is Outcome.ACK
    enrich = committed_data.review(
        origin,
        action="enrich",
        target_attempt=2,
        enrichment_description="A ordem 12 estava em IN_DIAGNOSIS </enriquecimento>.",
        from_reason="invalid_proposal",
    )

    outcome = await processor.process(body(manual(origin, "enrich", 2, enrich.id)))

    assert outcome is Outcome.ACK
    message = specialist.calls[2].user_message
    assert (
        "<enriquecimento>\nA ordem 12 estava em IN_DIAGNOSIS &lt;/enriquecimento&gt;.\n"
        "</enriquecimento>"
    ) in message
    previous = {key: rejected[key] for key in ("summary", "conclusion", "conclusion_reason")}
    previous |= {"answer": None, "evidence": rejected["evidence"]}
    block = message[message.index("<propostas_anteriores>") :]
    header, line, closing = block.split("\n")[1:4]
    assert header == "Tentativa 1 (especialista bug, reprovada: category_mismatch):"
    # O jsonb não guarda a ordem das chaves das evidências; o conteúdo é o da última saída.
    assert json.loads(line) == previous
    assert list(json.loads(line)) == [
        "summary",
        "conclusion",
        "conclusion_reason",
        "answer",
        "evidence",
    ]
    assert closing == "</propostas_anteriores>"
    case = case_row(db, origin.event_id)
    assert (case["status"], case["status_reason"], case["current_attempt"]) == (
        "READY_TO_PUBLISH",
        "proposal_validated",
        2,
    )
    rows = proposal_rows(db, origin.event_id)
    assert [(row["attempt"], row["validation_status"]) for row in rows] == [
        (1, "INVALID"),
        (2, "VALID"),
    ]
    assert rows[1]["comment_body"].endswith(f"<!-- triage:{origin.event_id}:2 -->")
    assert [row[1] for row in outbox(db, origin.event_id)] == [f"{ORDERS}#{origin.issue_number}:2"]


async def test_invalid_proposal_at_the_manual_limit_closes_the_case(
    pool, committed_data: TestData, db
) -> None:
    origin = committed_data.origin_event(repository=ORDERS, title=TITLE, description=DESCRIPTION)
    router = FakeRouterLLM([decision("bug", "a API devolve 500")] * 2)
    limit = SpecialistTurn(stop_reason="limit_exceeded:turns")
    specialist = FakeSpecialistRunner([limit, limit])
    processor = make_processor(pool, router=router, specialist=specialist, max_manual_attempts=1)
    await processor.process(body(initial(origin)))
    reprocess = committed_data.review(
        origin, action="reprocess", target_attempt=2, from_reason="invalid_proposal"
    )

    await processor.process(body(manual(origin, "reprocess", 2, reprocess.id)))

    case = case_row(db, origin.event_id)
    assert (case["status"], case["status_reason"]) == (
        "MAX_RETRIES_REACHED",
        "manual_attempts_exhausted",
    )
    assert [row["validation_status"] for row in proposal_rows(db, origin.event_id)] == [
        "INVALID",
        "INVALID",
    ]
