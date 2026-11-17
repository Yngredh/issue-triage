"""Consumo da fila `analysis` e ciclo da tentativa, sem IA (Etapa 4).

RabbitMQ e PostgreSQL reais (testcontainers). O roteador ainda não existe: o fluxo que chegaria
a ele é conferido chamando o processador direto, porque a mensagem volta para a fila.
"""

import asyncio
import json
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager

import aio_pika
import psycopg
import pytest
import structlog
from psycopg_pool import AsyncConnectionPool

from issue_triage.adapters.analysis_consumer import AnalysisConsumer, declare_analysis_queue
from issue_triage.adapters.database import close_pool, create_pool, open_pool
from issue_triage.adapters.inbound import read_issue_text
from issue_triage.analysis import AnalysisProcessor
from issue_triage.domain.issue import OriginEvent
from issue_triage.domain.messages import Outcome
from issue_triage.domain.templates import render_missing_info
from tests.conftest import IssueEvent, TestData

UNKNOWN_REPO = "Yaane-h/mechanical-hub-auth"
ORDERS = "Yaane-h/mechanical-hub-orders"


def initial(event: IssueEvent) -> dict:
    return {"eventId": event.event_id, "attempt": 1, "trigger": "initial", "triggerRef": None}


def author_edit(origin: IssueEvent, edit: IssueEvent) -> dict:
    return {
        "eventId": origin.event_id,
        "attempt": None,
        "trigger": "author_edit",
        "triggerRef": edit.event_id,
    }


def manual(origin: IssueEvent, trigger: str, attempt: int, review_id: int) -> dict:
    return {
        "eventId": origin.event_id,
        "attempt": attempt,
        "trigger": trigger,
        "triggerRef": str(review_id),
    }


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


@pytest.fixture
async def broker(rabbitmq_url: str) -> AsyncIterator[aio_pika.abc.AbstractChannel]:
    """Canal de teste com a fila `analysis` declarada e vazia."""
    connection = await aio_pika.connect(rabbitmq_url)
    try:
        channel = await connection.channel()
        queue = await declare_analysis_queue(channel)
        await queue.purge()
        yield channel
    finally:
        await connection.close()


async def publish(channel: aio_pika.abc.AbstractChannel, *messages: dict | bytes) -> None:
    for message in messages:
        body = message if isinstance(message, bytes) else json.dumps(message).encode()
        await channel.default_exchange.publish(
            aio_pika.Message(body=body, delivery_mode=aio_pika.DeliveryMode.PERSISTENT),
            routing_key="analysis",
        )


@asynccontextmanager
async def running_consumer(
    process, rabbitmq_url: str
) -> AsyncIterator[asyncio.Queue[Outcome | BaseException]]:
    """Consumidor real rodando; a fila devolvida recebe o resultado de cada mensagem."""
    results: asyncio.Queue[Outcome | BaseException] = asyncio.Queue()

    async def recording(body: bytes) -> Outcome:
        try:
            outcome = await process(body)
        except BaseException as exc:
            results.put_nowait(exc)
            raise
        results.put_nowait(outcome)
        return outcome

    consumer = AnalysisConsumer(recording, rabbitmq_url, prefetch=1, backoff_initial_s=0.05)
    consumer.start()
    try:
        yield results
    finally:
        await consumer.stop()


async def consume(
    pool: AsyncConnectionPool,
    rabbitmq_url: str,
    channel: aio_pika.abc.AbstractChannel,
    *messages: dict | bytes,
    max_manual_attempts: int = 2,
) -> list[Outcome | BaseException]:
    """Publica as mensagens na fila e espera o consumidor processar todas, em ordem."""
    processor = AnalysisProcessor(pool, max_manual_attempts=max_manual_attempts)
    async with running_consumer(processor.process, rabbitmq_url) as results:
        await publish(channel, *messages)
        return [await asyncio.wait_for(results.get(), timeout=20) for _ in messages]


def case_row(db: psycopg.Connection, event_id: str) -> dict | None:
    cur = db.execute(
        "SELECT status, status_reason, current_attempt, manual_attempts, category "
        "FROM triage.issue_cases WHERE origin_event_id = %s",
        (event_id,),
    )
    row = cur.fetchone()
    if row is None:
        return None
    keys = ("status", "status_reason", "current_attempt", "manual_attempts", "category")
    return dict(zip(keys, row, strict=True))


def attempts(db: psycopg.Connection, event_id: str) -> list[tuple]:
    return db.execute(
        "SELECT attempt, trigger, trigger_ref, state, outcome FROM triage.analysis_attempts "
        "WHERE event_id = %s ORDER BY attempt",
        (event_id,),
    ).fetchall()


def proposals(db: psycopg.Connection, event_id: str) -> list[tuple]:
    return db.execute(
        "SELECT pr.id, a.attempt, pr.kind, pr.validation_status, pr.comment_body "
        "FROM triage.proposals pr JOIN triage.analysis_attempts a ON a.id = pr.attempt_id "
        "WHERE a.event_id = %s ORDER BY a.attempt",
        (event_id,),
    ).fetchall()


def outbox(db: psycopg.Connection, event_id: str) -> list[tuple]:
    return db.execute(
        "SELECT topic, dedup_key, payload, published_at FROM triage.publish_outbox "
        "WHERE event_id = %s ORDER BY id",
        (event_id,),
    ).fetchall()


async def test_same_message_delivered_twice_opens_one_attempt(
    pool, rabbitmq_url, broker, committed_data: TestData, db
) -> None:
    origin = committed_data.origin_event(repository=ORDERS, description="")

    with structlog.testing.capture_logs() as logs:
        outcomes = await consume(pool, rabbitmq_url, broker, initial(origin), initial(origin))

    assert outcomes == [Outcome.ACK, Outcome.ACK]
    assert attempts(db, origin.event_id) == [(1, "initial", None, "DONE", "READY_TO_PUBLISH")]
    assert len(proposals(db, origin.event_id)) == 1
    assert len(outbox(db, origin.event_id)) == 1
    assert "analysis.skipped_duplicate" in [entry["event"] for entry in logs]


async def test_unknown_repository_needs_review_without_comment(
    pool, rabbitmq_url, broker, committed_data: TestData, db
) -> None:
    # Caso H04: repositório fora do catálogo, mesmo com descrição vazia.
    origin = committed_data.origin_event(repository=UNKNOWN_REPO, description="")

    with structlog.testing.capture_logs() as logs:
        outcomes = await consume(pool, rabbitmq_url, broker, initial(origin))

    assert outcomes == [Outcome.ACK]
    assert case_row(db, origin.event_id) == {
        "status": "NEEDS_REVIEW",
        "status_reason": "unknown_repo",
        "current_attempt": 1,
        "manual_attempts": 0,
        "category": None,
    }
    assert attempts(db, origin.event_id) == [(1, "initial", None, "DONE", "NEEDS_REVIEW")]
    assert proposals(db, origin.event_id) == []
    assert outbox(db, origin.event_id) == []
    assert "triage.unknown_repo" in [entry["event"] for entry in logs]


@pytest.mark.parametrize(
    "description",
    ["", "   ", "\n\r\n\n", "\t\t", " \t\r\n "],
    ids=["H05-empty", "H06-spaces", "H06-newlines", "H06-tabs", "H06-mixed"],
)
async def test_empty_description_gets_missing_info_comment(
    pool, rabbitmq_url, broker, committed_data: TestData, db, description: str
) -> None:
    origin = committed_data.origin_event(repository=ORDERS, description=description)

    outcomes = await consume(pool, rabbitmq_url, broker, initial(origin))

    assert outcomes == [Outcome.ACK]
    assert case_row(db, origin.event_id)["status"] == "READY_TO_PUBLISH"
    assert case_row(db, origin.event_id)["status_reason"] == "empty_description"
    assert attempts(db, origin.event_id) == [(1, "initial", None, "DONE", "READY_TO_PUBLISH")]

    [(proposal_id, revision, kind, validation_status, comment_body)] = proposals(
        db, origin.event_id
    )
    assert (revision, kind, validation_status) == (1, "missing_info", "VALID")
    assert comment_body == render_missing_info(
        project="Ordens de Serviço",
        responsible="@Yaane-h",
        event_id=origin.event_id,
        revision=1,
    )
    assert comment_body.count(f"<!-- triage:{origin.event_id}:1 -->") == 1
    assert "@Yaane-h" in comment_body

    [(topic, dedup_key, payload, published_at)] = outbox(db, origin.event_id)
    assert topic == "publish"
    assert dedup_key == f"{ORDERS}#{origin.issue_number}:1"
    assert payload == {"proposalId": proposal_id, "revision": 1}
    assert published_at is None


async def test_author_edit_reopens_empty_description_case(
    pool, rabbitmq_url, broker, committed_data: TestData, db
) -> None:
    origin = committed_data.origin_event(repository=ORDERS, description="")
    edit = committed_data.edit_event(origin, description="  ")  # ainda sem descrição

    outcomes = await consume(pool, rabbitmq_url, broker, initial(origin), author_edit(origin, edit))

    assert outcomes == [Outcome.ACK, Outcome.ACK]
    assert attempts(db, origin.event_id) == [
        (1, "initial", None, "DONE", "READY_TO_PUBLISH"),
        (2, "author_edit", edit.event_id, "DONE", "READY_TO_PUBLISH"),
    ]
    case = case_row(db, origin.event_id)
    assert (case["status"], case["current_attempt"], case["manual_attempts"]) == (
        "READY_TO_PUBLISH",
        2,
        0,
    )
    assert [row[1] for row in proposals(db, origin.event_id)] == [1, 2]
    assert proposals(db, origin.event_id)[1][4].endswith(f"<!-- triage:{origin.event_id}:2 -->")
    assert [row[1] for row in outbox(db, origin.event_id)] == [
        f"{ORDERS}#{origin.issue_number}:1",
        f"{ORDERS}#{origin.issue_number}:2",
    ]


async def test_author_edit_with_description_goes_on_to_the_router(
    pool, rabbitmq_url, broker, committed_data: TestData, db
) -> None:
    origin = committed_data.origin_event(repository=ORDERS, description="")
    await consume(pool, rabbitmq_url, broker, initial(origin))
    # A edição chega depois da primeira análise (D14: o texto atual é o da última edição).
    edit = committed_data.edit_event(origin, description="A ordem 12 não muda de status.")
    processor = AnalysisProcessor(pool, max_manual_attempts=2)
    body = json.dumps(author_edit(origin, edit)).encode()

    with structlog.testing.capture_logs() as logs:
        first = await processor.process(body)
        again = await processor.process(body)  # a mensagem voltou para a fila

    # Sem roteador nesta etapa, a tentativa fica IN_PROGRESS e a mensagem volta para a fila.
    assert (first, again) == (Outcome.REQUEUE, Outcome.REQUEUE)
    assert attempts(db, origin.event_id) == [
        (1, "initial", None, "DONE", "READY_TO_PUBLISH"),
        (2, "author_edit", edit.event_id, "IN_PROGRESS", None),
    ]
    case = case_row(db, origin.event_id)
    assert (case["status"], case["status_reason"], case["current_attempt"]) == (
        "ANALYZING",
        "author_edit",
        2,
    )
    assert "analysis.resumed" in [entry["event"] for entry in logs]


async def test_author_edit_out_of_state_and_redelivered_edit_are_ignored(
    pool, rabbitmq_url, broker, committed_data: TestData, db
) -> None:
    out_of_state = committed_data.origin_event(repository=UNKNOWN_REPO, description="")
    edit_out = committed_data.edit_event(out_of_state)
    origin = committed_data.origin_event(repository=ORDERS, description="")
    edit = committed_data.edit_event(origin, description="")

    with structlog.testing.capture_logs() as logs:
        outcomes = await consume(
            pool,
            rabbitmq_url,
            broker,
            initial(out_of_state),
            author_edit(out_of_state, edit_out),
            initial(origin),
            author_edit(origin, edit),
            author_edit(origin, edit),
        )

    assert outcomes == [Outcome.ACK] * 5
    assert attempts(db, out_of_state.event_id) == [(1, "initial", None, "DONE", "NEEDS_REVIEW")]
    assert case_row(db, out_of_state.event_id)["status_reason"] == "unknown_repo"
    assert [row[0] for row in attempts(db, origin.event_id)] == [1, 2]
    assert len(proposals(db, origin.event_id)) == 2
    events = [entry["event"] for entry in logs]
    assert "analysis.edit_ignored" in events
    assert "analysis.skipped_duplicate" in events


async def test_manual_attempts_count_and_close_at_the_limit(
    pool, rabbitmq_url, broker, committed_data: TestData, db
) -> None:
    origin = committed_data.origin_event(repository=UNKNOWN_REPO)
    enrich = committed_data.review(
        origin, action="enrich", target_attempt=2, from_reason="unknown_repo"
    )
    reprocess = committed_data.review(
        origin, action="reprocess", target_attempt=3, from_reason="unknown_repo"
    )

    outcomes = await consume(
        pool,
        rabbitmq_url,
        broker,
        initial(origin),
        manual(origin, "enrich", 2, enrich.id),
        manual(origin, "reprocess", 3, reprocess.id),
    )

    assert outcomes == [Outcome.ACK] * 3
    assert attempts(db, origin.event_id) == [
        (1, "initial", None, "DONE", "NEEDS_REVIEW"),
        (2, "enrich", str(enrich.id), "DONE", "NEEDS_REVIEW"),
        (3, "reprocess", str(reprocess.id), "DONE", "MAX_RETRIES_REACHED"),
    ]
    assert case_row(db, origin.event_id) == {
        "status": "MAX_RETRIES_REACHED",
        "status_reason": "manual_attempts_exhausted",
        "current_attempt": 3,
        "manual_attempts": 2,
        "category": None,
    }


async def test_enrich_increments_manual_attempts_and_stale_attempt_is_skipped(
    pool, rabbitmq_url, broker, committed_data: TestData, db
) -> None:
    origin = committed_data.origin_event(repository=UNKNOWN_REPO)
    enrich = committed_data.review(origin, action="enrich", target_attempt=3)
    stale = committed_data.review(origin, action="reprocess", target_attempt=2)

    with structlog.testing.capture_logs() as logs:
        outcomes = await consume(
            pool,
            rabbitmq_url,
            broker,
            initial(origin),
            manual(origin, "enrich", 3, enrich.id),
            manual(origin, "enrich", 3, enrich.id),  # repetida
            manual(origin, "reprocess", 2, stale.id),  # obsoleta
        )

    assert outcomes == [Outcome.ACK] * 4
    assert [row[0] for row in attempts(db, origin.event_id)] == [1, 3]
    case = case_row(db, origin.event_id)
    assert (case["status"], case["current_attempt"], case["manual_attempts"]) == (
        "NEEDS_REVIEW",
        3,
        1,
    )
    events = [entry["event"] for entry in logs]
    assert "analysis.skipped_duplicate" in events
    assert "analysis.skipped_stale" in events


async def test_invalid_message_and_unknown_event_are_discarded(
    pool, rabbitmq_url, broker, db
) -> None:
    missing = {"eventId": "evt-does-not-exist", "attempt": 1, "trigger": "initial"}
    missing["triggerRef"] = None

    with structlog.testing.capture_logs() as logs:
        outcomes = await consume(
            pool,
            rabbitmq_url,
            broker,
            b"not json",
            {"eventId": "evt-x", "attempt": None, "trigger": "close", "triggerRef": "1"},
            missing,
        )

    assert outcomes == [Outcome.ACK] * 3
    assert case_row(db, "evt-does-not-exist") is None
    assert case_row(db, "evt-x") is None
    discarded = [entry for entry in logs if entry["event"] == "analysis.discarded_invalid"]
    assert [entry["reason"] for entry in discarded] == [
        "invalid_schema",
        "invalid_schema",
        "origin_not_found",
    ]


async def test_failed_processing_is_not_acked_and_comes_back(rabbitmq_url, broker) -> None:
    calls: list[bytes] = []

    async def flaky(body: bytes) -> Outcome:
        calls.append(body)
        if len(calls) == 1:
            raise psycopg.OperationalError("database is down")
        return Outcome.ACK

    async with running_consumer(flaky, rabbitmq_url) as results:
        await publish(broker, b'{"eventId": "evt-flaky"}')
        first = await asyncio.wait_for(results.get(), timeout=20)
        second = await asyncio.wait_for(results.get(), timeout=20)

    assert isinstance(first, psycopg.OperationalError)
    assert second is Outcome.ACK
    assert calls == [b'{"eventId": "evt-flaky"}'] * 2
    queue = await broker.declare_queue("analysis", durable=True, passive=True)
    assert queue.declaration_result.message_count == 0


async def test_current_text_is_the_last_edit_and_enrichments_keep_order(
    pool, committed_data: TestData
) -> None:
    origin_event = committed_data.origin_event(repository=ORDERS, description="original")
    committed_data.edit_event(origin_event, title="Título 1", description="primeira edição")
    committed_data.edit_event(origin_event, title="Título 2", description="  ")
    committed_data.review(
        origin_event, action="enrich", target_attempt=2, enrichment_description="primeiro"
    )
    committed_data.review(origin_event, action="reprocess", target_attempt=3)
    committed_data.review(
        origin_event, action="enrich", target_attempt=4, enrichment_description="segundo"
    )
    origin = OriginEvent(origin_event.event_id, origin_event.repository, origin_event.issue_number)

    async with pool.connection() as conn:
        text = await read_issue_text(conn, origin)

    assert (text.title, text.description, text.description_empty) == ("Título 2", "  ", True)
    assert text.enrichments == ("primeiro", "segundo")


async def test_origin_text_is_used_when_there_is_no_edit(pool, committed_data: TestData) -> None:
    origin_event = committed_data.origin_event(repository=ORDERS, description="só a origem")
    origin = OriginEvent(origin_event.event_id, origin_event.repository, origin_event.issue_number)

    async with pool.connection() as conn:
        text = await read_issue_text(conn, origin)

    assert (text.description, text.description_empty, text.enrichments) == (
        "só a origem",
        False,
        (),
    )
