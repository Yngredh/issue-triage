# ruff: noqa: F811  (as fixtures `pool` e `broker` vêm de test_analysis)
"""Resolução manual: encerramento (Etapa 11; RF29; P1).

RabbitMQ e PostgreSQL reais (testcontainers) e portas falsas (D10). A linha de review é inserida
no stub de `inbound` (D9) e a mensagem `close` é publicada direto na fila.
"""

import psycopg
import pytest
import structlog

from issue_triage.domain.messages import Outcome
from tests.conftest import IssueEvent, TestData
from tests.fakes import FakeRouterLLM
from tests.integration.test_analysis import (  # noqa: F401
    ORDERS,
    UNKNOWN_REPO,
    attempts,
    broker,
    case_row,
    consume,
    initial,
    outbox,
    pool,
    proposals,
)


@pytest.fixture
def db(database_url: str):
    with psycopg.connect(database_url, autocommit=True) as conn:
        yield conn


def close_message(origin: IssueEvent, review_ref: object) -> dict:
    return {
        "eventId": origin.event_id,
        "attempt": None,
        "trigger": "close",
        "triggerRef": str(review_ref),
    }


def set_case(
    db: psycopg.Connection, origin: IssueEvent, status: str, reason: str, manual: int = 0
) -> None:
    db.execute(
        "UPDATE triage.issue_cases SET status = %s, status_reason = %s, manual_attempts = %s "
        "WHERE origin_event_id = %s",
        (status, reason, manual, origin.event_id),
    )


def closing(db: psycopg.Connection, event_id: str) -> tuple:
    return db.execute(
        "SELECT status, status_reason, closed_by_review_id FROM triage.issue_cases "
        "WHERE origin_event_id = %s",
        (event_id,),
    ).fetchone()


async def open_case(pool, rabbitmq_url, broker, data: TestData) -> IssueEvent:
    """Caso criado pela mensagem `initial` (repositório fora do catálogo: sem IA)."""
    origin = data.origin_event(repository=UNKNOWN_REPO)
    assert await consume(pool, rabbitmq_url, broker, initial(origin)) == [Outcome.ACK]
    return origin


@pytest.mark.parametrize(
    ("status", "reason", "manual"),
    [
        ("NEEDS_REVIEW", "insufficient_content", 0),
        ("NEEDS_REVIEW", "invalid_proposal", 1),
        ("MAX_RETRIES_REACHED", "manual_attempts_exhausted", 2),
        ("READY_TO_PUBLISH", "proposal_validated", 0),  # falha de publicação
    ],
)
@pytest.mark.parametrize("final_status", ["CANCELLED", "FINISHED"])
async def test_close_ends_the_case_without_attempt_or_ai(
    pool, rabbitmq_url, broker, committed_data: TestData, db, status, reason, manual, final_status
) -> None:
    origin = await open_case(pool, rabbitmq_url, broker, committed_data)
    set_case(db, origin, status, reason, manual)
    before = attempts(db, origin.event_id)
    review = committed_data.review(
        origin, action="close", final_status=final_status, from_status=status, from_reason=reason
    )
    router = FakeRouterLLM()  # sem respostas: qualquer chamada falha o teste

    with structlog.testing.capture_logs() as logs:
        outcomes = await consume(
            pool, rabbitmq_url, broker, close_message(origin, review.id), router=router
        )

    assert outcomes == [Outcome.ACK]
    assert closing(db, origin.event_id) == (final_status, "closed_by_reviewer", review.id)
    assert case_row(db, origin.event_id)["current_attempt"] == 1
    assert attempts(db, origin.event_id) == before
    assert router.calls == []
    closed = [entry for entry in logs if entry["event"] == "triage.case_closed"]
    assert len(closed) == 1
    assert (closed[0]["status"], closed[0]["from_status"]) == (final_status, status)


async def test_repeated_close_changes_nothing(
    pool, rabbitmq_url, broker, committed_data: TestData, db
) -> None:
    origin = await open_case(pool, rabbitmq_url, broker, committed_data)
    review = committed_data.review(origin, action="close", final_status="FINISHED")
    message = close_message(origin, review.id)

    with structlog.testing.capture_logs() as logs:
        outcomes = await consume(pool, rabbitmq_url, broker, message, message)

    assert outcomes == [Outcome.ACK, Outcome.ACK]
    assert closing(db, origin.event_id) == ("FINISHED", "closed_by_reviewer", review.id)
    events = [entry["event"] for entry in logs]
    assert events.count("triage.case_closed") == 1
    assert events.count("analysis.skipped_duplicate") == 1


async def test_close_does_not_touch_proposals_or_outbox(
    pool, rabbitmq_url, broker, committed_data: TestData, db
) -> None:
    origin = committed_data.origin_event(repository=ORDERS, description="")
    await consume(pool, rabbitmq_url, broker, initial(origin))  # missing_info: READY_TO_PUBLISH
    assert case_row(db, origin.event_id)["status"] == "READY_TO_PUBLISH"
    review = committed_data.review(origin, action="close")
    before = (proposals(db, origin.event_id), outbox(db, origin.event_id))

    await consume(pool, rabbitmq_url, broker, close_message(origin, review.id))

    assert closing(db, origin.event_id) == ("CANCELLED", "closed_by_reviewer", review.id)
    assert (proposals(db, origin.event_id), outbox(db, origin.event_id)) == before


async def test_close_is_ignored_while_analyzing_or_when_closed_by_another_review(
    pool, rabbitmq_url, broker, committed_data: TestData, db
) -> None:
    analyzing = await open_case(pool, rabbitmq_url, broker, committed_data)
    set_case(db, analyzing, "ANALYZING", "initial")
    review_a = committed_data.review(analyzing, action="close")

    closed = await open_case(pool, rabbitmq_url, broker, committed_data)
    first = committed_data.review(closed, action="close", final_status="CANCELLED")
    await consume(pool, rabbitmq_url, broker, close_message(closed, first.id))
    # Outro review de close do mesmo evento: o índice único só permite um, então o primeiro sai.
    db.execute("DELETE FROM inbound.issue_review_history WHERE id = %s", (first.id,))
    other = committed_data.review(closed, action="close", final_status="FINISHED")

    with structlog.testing.capture_logs() as logs:
        outcomes = await consume(
            pool,
            rabbitmq_url,
            broker,
            close_message(analyzing, review_a.id),
            close_message(closed, other.id),
        )

    assert outcomes == [Outcome.ACK, Outcome.ACK]
    assert closing(db, analyzing.event_id) == ("ANALYZING", "initial", None)
    assert closing(db, closed.event_id) == ("CANCELLED", "closed_by_reviewer", first.id)
    ignored = [entry for entry in logs if entry["event"] == "analysis.close_ignored"]
    assert len(ignored) == 2


async def test_close_with_missing_or_foreign_review_is_discarded(
    pool, rabbitmq_url, broker, committed_data: TestData, db
) -> None:
    origin = await open_case(pool, rabbitmq_url, broker, committed_data)
    other = await open_case(pool, rabbitmq_url, broker, committed_data)
    foreign = committed_data.review(other, action="close")
    not_close = committed_data.review(origin, action="enrich", target_attempt=2)

    with structlog.testing.capture_logs() as logs:
        outcomes = await consume(
            pool,
            rabbitmq_url,
            broker,
            close_message(origin, 999999),  # inexistente
            close_message(origin, foreign.id),  # de outro evento
            close_message(origin, not_close.id),  # não é close
            close_message(origin, 2**63),  # além do bigint: não existe linha
        )

    assert outcomes == [Outcome.ACK] * 4
    discarded = [entry for entry in logs if entry["event"] == "analysis.discarded_invalid"]
    assert [entry["reason"] for entry in discarded] == ["review_not_found"] * 4
    assert closing(db, origin.event_id)[0] == "NEEDS_REVIEW"
    assert closing(db, other.event_id)[0] == "NEEDS_REVIEW"


async def test_close_for_a_case_that_does_not_exist_is_discarded(
    pool, rabbitmq_url, broker, committed_data: TestData, db
) -> None:
    origin = committed_data.origin_event(repository=UNKNOWN_REPO)  # sem mensagem initial
    review = committed_data.review(origin, action="close")

    with structlog.testing.capture_logs() as logs:
        outcomes = await consume(pool, rabbitmq_url, broker, close_message(origin, review.id))

    assert outcomes == [Outcome.ACK]
    assert case_row(db, origin.event_id) is None
    discarded = [entry for entry in logs if entry["event"] == "analysis.discarded_invalid"]
    assert [entry["reason"] for entry in discarded] == ["case_not_found"]


@pytest.mark.parametrize("ref", ["abc", "0", "-1", "1.5", " 7", "7 ", "0x1"])
async def test_close_with_non_numeric_or_non_positive_ref_is_invalid_schema(
    pool, rabbitmq_url, broker, committed_data: TestData, db, ref
) -> None:
    """D30: o modelo da mensagem recusa; o banco nem é consultado."""
    origin = await open_case(pool, rabbitmq_url, broker, committed_data)
    committed_data.review(origin, action="close")
    before = case_row(db, origin.event_id)

    with structlog.testing.capture_logs() as logs:
        outcomes = await consume(pool, rabbitmq_url, broker, close_message(origin, ref))

    assert outcomes == [Outcome.ACK]
    discarded = [entry for entry in logs if entry["event"] == "analysis.discarded_invalid"]
    assert [entry["reason"] for entry in discarded] == ["invalid_schema"]
    assert [entry["event"] for entry in logs] == ["analysis.discarded_invalid"]
    assert case_row(db, origin.event_id) == before
