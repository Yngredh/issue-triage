# ruff: noqa: F811  (a fixture `pool` vem de test_analysis)
"""Observabilidade (Etapa 12; RNF14): contexto dos logs e o que nunca aparece neles.

PostgreSQL real (testcontainers) e portas falsas (D10). `capture_logs` recebe `merge_contextvars`
para que o contexto ligado durante a mensagem apareça em cada entrada, como no JSON real.
"""

import json
import re

import structlog

from issue_triage.config import Settings
from issue_triage.domain.messages import Outcome
from issue_triage.domain.prompts import ROUTER_SYSTEM_PROMPT
from issue_triage.logging import configure_logging
from tests.conftest import TestData
from tests.fakes import (
    FakeRouterLLM,
    FakeSpecialistRunner,
    SpecialistTurn,
    decision,
    make_processor,
)
from tests.integration.test_analysis import UNKNOWN_REPO, initial, pool  # noqa: F401
from tests.integration.test_specialists import (
    DESCRIPTION,
    ORDERS,
    READ,
    TITLE,
    body,
    valid_output,
)
from tests.integration.test_technical_failures import provider_error

QUOTE = "a API devolve 500"
# Eventos que só existem depois de a tentativa abrir: levam também `attempt`.
WITH_ATTEMPT = {
    "triage.unknown_repo",
    "triage.empty_description",
    "triage.routed",
    "triage.router_retry",
    "triage.specialist_started",
    "triage.tool_call",
    "triage.specialist_retry",
    "triage.limit_exceeded",
    "triage.proposal_validated",
    "triage.proposal_invalid",
    "triage.technical_failure",
    "triage.retry_scheduled",
}
# Antes de o evento de origem ser lido, só o `event_id` é conhecido.
EVENT_ID_ONLY = {"analysis.received"}


def capture():
    return structlog.testing.capture_logs(processors=[structlog.contextvars.merge_contextvars])


def new_origin(data: TestData, **kwargs):
    kwargs.setdefault("repository", ORDERS)
    kwargs.setdefault("title", TITLE)
    kwargs.setdefault("description", DESCRIPTION)
    return data.origin_event(**kwargs)


def assert_context(logs: list[dict], origin) -> set[str]:
    """Todo log da mensagem leva o contexto ligado até então; devolve os eventos vistos."""
    assert logs
    for entry in logs:
        event = entry["event"]
        assert entry["event_id"] == origin.event_id, event
        if event not in EVENT_ID_ONLY:
            assert entry["repository"] == origin.repository, event
            assert entry["issue_number"] == origin.issue_number, event
        if event in WITH_ATTEMPT:
            assert entry["attempt"] == 1, event
    return {entry["event"] for entry in logs}


async def test_happy_path_events_carry_the_message_context(pool, committed_data: TestData) -> None:
    origin = new_origin(committed_data)
    router = FakeRouterLLM([decision("bug", QUOTE)])
    specialist = FakeSpecialistRunner([SpecialistTurn(output=valid_output(), tool_calls=READ)])
    processor = make_processor(pool, router=router, specialist=specialist)

    with capture() as logs:
        assert await processor.process(body(initial(origin))) is Outcome.ACK
        assert await processor.process(body(initial(origin))) is Outcome.ACK  # reentrega

    events = assert_context(logs, origin)
    assert {
        "analysis.received",
        "triage.routed",
        "triage.specialist_started",
        "triage.tool_call",
        "triage.proposal_validated",
        "analysis.skipped_duplicate",
    } <= events
    [routed] = [e for e in logs if e["event"] == "triage.routed"]
    assert (routed["category"], routed["llm_calls"]) == ("bug", 1)
    [tool] = [e for e in logs if e["event"] == "triage.tool_call"]
    assert {"tool", "duration_ms", "result_chars", "truncated"} <= tool.keys()


async def test_context_does_not_leak_after_the_message(pool, committed_data: TestData) -> None:
    origin = new_origin(committed_data, repository=UNKNOWN_REPO)

    await make_processor(pool).process(body(initial(origin)))

    leaked = structlog.contextvars.get_contextvars()
    assert not {"event_id", "repository", "issue_number", "attempt"} & leaked.keys()


async def test_unknown_repo_and_empty_description(pool, committed_data: TestData) -> None:
    unknown = new_origin(committed_data, repository=UNKNOWN_REPO)
    empty = new_origin(committed_data, description="")
    processor = make_processor(pool)

    with capture() as first:
        await processor.process(body(initial(unknown)))
    with capture() as second:
        await processor.process(body(initial(empty)))

    assert "triage.unknown_repo" in assert_context(first, unknown)
    assert "triage.empty_description" in assert_context(second, empty)


async def test_router_retry_and_specialist_retry_with_invalid_proposal(
    pool, committed_data: TestData
) -> None:
    origin = new_origin(committed_data)
    router = FakeRouterLLM(
        [decision("bug", "trecho que não está na issue"), decision("bug", QUOTE)]
    )
    specialist = FakeSpecialistRunner(
        [
            SpecialistTurn(output=valid_output(category="melhoria")),
            SpecialistTurn(output=valid_output(summary="Segunda saída.")),
        ]
    )
    processor = make_processor(pool, router=router, specialist=specialist)

    with capture() as logs:
        await processor.process(body(initial(origin)))

    events = assert_context(logs, origin)
    assert {"triage.router_retry", "triage.specialist_retry", "triage.proposal_invalid"} <= events
    [routed] = [e for e in logs if e["event"] == "triage.routed"]
    assert routed["llm_calls"] == 2
    [retry] = [e for e in logs if e["event"] == "triage.router_retry"]
    assert retry["code"]


async def test_limit_exceeded_event(pool, committed_data: TestData) -> None:
    origin = new_origin(committed_data)
    router = FakeRouterLLM([decision("bug", QUOTE)])
    specialist = FakeSpecialistRunner(
        [SpecialistTurn(tool_calls=READ, stop_reason="limit_exceeded:budget")]
    )
    processor = make_processor(pool, router=router, specialist=specialist)

    with capture() as logs:
        await processor.process(body(initial(origin)))

    assert "triage.limit_exceeded" in assert_context(logs, origin)


async def test_retry_scheduled_and_technical_failure(pool, committed_data: TestData) -> None:
    origin = new_origin(committed_data)
    router = FakeRouterLLM([decision("bug", QUOTE)])
    specialist = FakeSpecialistRunner([provider_error(503)] * 3)
    processor = make_processor(pool, router=router, specialist=specialist)

    with capture() as logs:
        await processor.process(body(initial(origin)))

    events = assert_context(logs, origin)
    assert {"triage.retry_scheduled", "triage.technical_failure"} <= events
    scheduled = [e for e in logs if e["event"] == "triage.retry_scheduled"]
    assert all({"stage", "error", "delay_s"} <= e.keys() for e in scheduled)
    [failure] = [e for e in logs if e["event"] == "triage.technical_failure"]
    assert failure["log_level"] == "error"
    assert {"stage", "error"} <= failure.keys()


async def test_origin_not_found_logs_only_the_event_id(pool) -> None:
    message = {
        "eventId": "evt-desconhecido",
        "attempt": 1,
        "trigger": "initial",
        "triggerRef": None,
    }

    with capture() as logs:
        await make_processor(pool).process(body(message))

    discarded = [e for e in logs if e["event"] == "analysis.discarded_invalid"]
    assert [(e["event_id"], e["reason"]) for e in discarded] == [
        ("evt-desconhecido", "origin_not_found")
    ]
    assert "repository" not in discarded[0]


async def test_no_log_carries_secrets_prompts_or_the_issue_text(
    pool, committed_data: TestData
) -> None:
    description = "Texto completo da issue com um detalhe incomum: zebra-azul-9731. " + DESCRIPTION
    origin = new_origin(committed_data, description=description)
    router = FakeRouterLLM([decision("bug", "zebra-azul-9731")])
    # Duas saídas reprovadas, depois uma falha com mensagem do provedor: cobre os caminhos de erro.
    specialist = FakeSpecialistRunner(
        [
            SpecialistTurn(output=valid_output(category="melhoria"), tool_calls=READ),
            SpecialistTurn(output=valid_output(summary="Segunda saída.")),
        ]
    )
    failing = new_origin(committed_data, description=description)
    failing_router = FakeRouterLLM([decision("bug", "zebra-azul-9731")])
    failing_specialist = FakeSpecialistRunner([provider_error(503)] * 3)

    with capture() as logs:
        await make_processor(pool, router=router, specialist=specialist).process(
            body(initial(origin))
        )
        await make_processor(pool, router=failing_router, specialist=failing_specialist).process(
            body(initial(failing))
        )

    system_prompt = specialist.calls[0].system_prompt
    [token] = re.findall(r"Código de verificação desta análise: (\w+)", system_prompt)
    dumped = json.dumps(logs, default=str, ensure_ascii=False)
    assert ROUTER_SYSTEM_PROMPT[:80] not in dumped
    assert system_prompt[:80] not in dumped
    assert token not in dumped
    assert description not in dumped
    assert "texto do provedor" not in dumped
    assert "sk-ant" not in dumped
    assert "api_key" not in dumped.lower()


def test_logging_setup_keeps_context_merge() -> None:
    """O JSON real passa por `merge_contextvars`: é ele que leva o contexto a cada linha."""
    configure_logging(Settings(DATABASE_URL="postgresql://x", RABBITMQ_URL="amqp://x"))
    try:
        assert structlog.contextvars.merge_contextvars in structlog.get_config()["processors"]
    finally:
        structlog.reset_defaults()
        structlog.contextvars.clear_contextvars()


async def test_close_logs_carry_the_context(pool, committed_data: TestData) -> None:
    origin = new_origin(committed_data, repository=UNKNOWN_REPO)
    await make_processor(pool).process(body(initial(origin)))
    review = committed_data.review(origin, action="close", final_status="FINISHED")
    message = {
        "eventId": origin.event_id,
        "attempt": None,
        "trigger": "close",
        "triggerRef": str(review.id),
    }

    with capture() as logs:
        await make_processor(pool).process(body(message))

    assert "triage.case_closed" in assert_context(logs, origin)
