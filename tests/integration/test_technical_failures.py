"""Falhas técnicas ligadas ao fluxo (Etapa 10; RNF08; D28).

PostgreSQL real (testcontainers) e portas falsas (D10); atrasos curtos no lugar de 10 s e 30 s.
Nenhuma chamada à Anthropic.
"""

import asyncio
import json
from collections.abc import AsyncIterator, Iterator

import anthropic
import httpx2
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
)
from tests.integration.test_analysis import attempts, case_row, initial, outbox
from tests.integration.test_specialists import (
    DESCRIPTION,
    ORDERS,
    READ,
    TITLE,
    attempt_errors,
    proposal_rows,
    valid_output,
)

REQUEST = httpx2.Request("POST", "https://api.invalid/v1/messages")
DELAYS = (0.01, 0.01)


def provider_error(status: int) -> anthropic.APIStatusError:
    response = httpx2.Response(status, request=REQUEST)
    return anthropic.APIStatusError("texto do provedor", response=response, body=None)


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


def good_turn() -> SpecialistTurn:
    return SpecialistTurn(output=valid_output(), tool_calls=READ)


def new_origin(committed_data: TestData):
    return committed_data.origin_event(repository=ORDERS, title=TITLE, description=DESCRIPTION)


async def test_503_503_then_success_ends_in_a_valid_proposal(
    pool, committed_data: TestData, db
) -> None:
    origin = new_origin(committed_data)
    router = FakeRouterLLM([decision("bug", "a API devolve 500")])
    specialist = FakeSpecialistRunner([provider_error(503), provider_error(503), good_turn()])
    processor = make_processor(pool, router=router, specialist=specialist, retry_delays_s=DELAYS)

    with structlog.testing.capture_logs() as logs:
        outcome = await processor.process(body(initial(origin)))

    assert outcome is Outcome.ACK
    assert len(router.calls) == 1  # a nova tentativa repete só o especialista
    assert len(specialist.calls) == 3
    assert case_row(db, origin.event_id)["status"] == "READY_TO_PUBLISH"
    assert attempt_errors(db, origin.event_id) == [None]
    assert len(outbox(db, origin.event_id)) == 1
    scheduled = [e for e in logs if e["event"] == "triage.retry_scheduled"]
    assert [(e["stage"], e["error"], e["delay_s"]) for e in scheduled] == [
        ("specialist", "provider: server_error: 503", 0.01)
    ] * 2
    assert "texto do provedor" not in repr(logs)


async def test_router_transient_failure_retries_only_the_router(
    pool, committed_data: TestData, db
) -> None:
    origin = new_origin(committed_data)
    router = FakeRouterLLM(
        [anthropic.APITimeoutError(request=REQUEST), decision("bug", "a API devolve 500")]
    )
    specialist = FakeSpecialistRunner([good_turn()])
    processor = make_processor(pool, router=router, specialist=specialist, retry_delays_s=DELAYS)

    with structlog.testing.capture_logs() as logs:
        await processor.process(body(initial(origin)))

    assert (len(router.calls), len(specialist.calls)) == (2, 1)
    assert case_row(db, origin.event_id)["status"] == "READY_TO_PUBLISH"
    [scheduled] = [e for e in logs if e["event"] == "triage.retry_scheduled"]
    assert (scheduled["stage"], scheduled["error"]) == ("router", "provider: timeout")


async def test_three_503_end_in_technical_failure(pool, committed_data: TestData, db) -> None:
    origin = new_origin(committed_data)
    router = FakeRouterLLM([decision("bug", "a API devolve 500")])
    specialist = FakeSpecialistRunner([provider_error(503)] * 3)
    processor = make_processor(pool, router=router, specialist=specialist, retry_delays_s=DELAYS)

    with structlog.testing.capture_logs() as logs:
        outcome = await processor.process(body(initial(origin)))

    assert outcome is Outcome.ACK
    assert len(specialist.calls) == 3
    case = case_row(db, origin.event_id)
    assert (case["status"], case["status_reason"]) == ("NEEDS_REVIEW", "technical_failure")
    assert attempts(db, origin.event_id) == [(1, "initial", None, "DONE", "NEEDS_REVIEW")]
    assert attempt_errors(db, origin.event_id) == ["provider: server_error: 503"]
    assert proposal_rows(db, origin.event_id) == []
    assert outbox(db, origin.event_id) == []
    events = [e["event"] for e in logs]
    assert events.count("triage.retry_scheduled") == 2
    [failure] = [e for e in logs if e["event"] == "triage.technical_failure"]
    assert failure["error"] == "provider: server_error: 503"


async def test_retries_are_shared_by_the_stages_of_the_message(
    pool, committed_data: TestData, db
) -> None:
    origin = new_origin(committed_data)
    router = FakeRouterLLM([provider_error(429), decision("bug", "a API devolve 500")])
    specialist = FakeSpecialistRunner([provider_error(529), provider_error(529)])
    processor = make_processor(pool, router=router, specialist=specialist, retry_delays_s=DELAYS)

    await processor.process(body(initial(origin)))

    assert (len(router.calls), len(specialist.calls)) == (2, 2)
    assert attempt_errors(db, origin.event_id) == ["provider: overloaded: 529"]


async def test_401_is_technical_failure_without_waiting(
    pool, committed_data: TestData, db, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin = new_origin(committed_data)
    router = FakeRouterLLM([provider_error(401)])
    processor = make_processor(pool, router=router, retry_delays_s=DELAYS)

    async def no_sleep(_delay: float) -> None:
        raise AssertionError("must not wait on a permanent failure")

    monkeypatch.setattr(asyncio, "sleep", no_sleep)

    with structlog.testing.capture_logs() as logs:
        outcome = await processor.process(body(initial(origin)))

    assert outcome is Outcome.ACK
    assert len(router.calls) == 1
    case = case_row(db, origin.event_id)
    assert (case["status"], case["status_reason"], case["category"]) == (
        "NEEDS_REVIEW",
        "technical_failure",
        None,
    )
    assert attempt_errors(db, origin.event_id) == ["provider: auth: 401"]
    assert "triage.retry_scheduled" not in [e["event"] for e in logs]
    [failure] = [e for e in logs if e["event"] == "triage.technical_failure"]
    assert (failure["stage"], failure["error"]) == ("router", "provider: auth: 401")


async def test_unexpected_exception_is_internal_technical_failure_with_ack(
    pool, committed_data: TestData, db
) -> None:
    origin = new_origin(committed_data)
    router = FakeRouterLLM([decision("bug", "a API devolve 500")])
    specialist = FakeSpecialistRunner([RuntimeError("segredo")])
    processor = make_processor(pool, router=router, specialist=specialist, retry_delays_s=DELAYS)

    with structlog.testing.capture_logs() as logs:
        outcome = await processor.process(body(initial(origin)))

    assert outcome is Outcome.ACK
    assert len(specialist.calls) == 1
    case = case_row(db, origin.event_id)
    assert (case["status"], case["status_reason"]) == ("NEEDS_REVIEW", "technical_failure")
    assert attempt_errors(db, origin.event_id) == ["internal: RuntimeError"]
    assert "segredo" not in repr(logs)


async def test_database_error_is_not_a_technical_failure(
    pool, committed_data: TestData, db, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin = new_origin(committed_data)
    router = FakeRouterLLM([decision("bug", "a API devolve 500")])
    specialist = FakeSpecialistRunner([good_turn()])
    processor = make_processor(pool, router=router, specialist=specialist, retry_delays_s=DELAYS)

    async def broken(*_args, **_kwargs):
        raise psycopg.OperationalError("database is down")

    monkeypatch.setattr("issue_triage.analysis.triage_store.finish_attempt", broken)

    with pytest.raises(psycopg.OperationalError):
        await processor.process(body(initial(origin)))
    assert attempts(db, origin.event_id) == [(1, "initial", None, "IN_PROGRESS", None)]


async def test_crash_with_attempt_in_progress_resumes_without_calling_the_router(
    pool, committed_data: TestData, db
) -> None:
    origin = new_origin(committed_data)
    router = FakeRouterLLM([decision("bug", "a API devolve 500")])
    crashing = FakeSpecialistRunner([asyncio.CancelledError()])
    first = make_processor(pool, router=router, specialist=crashing, retry_delays_s=DELAYS)

    with pytest.raises(asyncio.CancelledError):
        await first.process(body(initial(origin)))
    assert attempts(db, origin.event_id) == [(1, "initial", None, "IN_PROGRESS", None)]

    resumed_specialist = FakeSpecialistRunner([provider_error(503), good_turn()])
    second = make_processor(
        pool, router=router, specialist=resumed_specialist, retry_delays_s=DELAYS
    )
    outcome = await second.process(body(initial(origin)))

    assert outcome is Outcome.ACK
    assert len(router.calls) == 1
    assert case_row(db, origin.event_id)["status"] == "READY_TO_PUBLISH"
