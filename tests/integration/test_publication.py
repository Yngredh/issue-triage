"""Saída para publicação de ponta a ponta (Etapa 9; RF19, RF21, RNF09, RNF10).

Mensagem na entrada do processador com `FakeRouterLLM` e `FakeSpecialistRunner`, PostgreSQL e
RabbitMQ reais (testcontainers) e o relay da outbox: a proposta chega à fila `publish`. Nenhuma
chamada à Anthropic.
"""

import json
from collections.abc import AsyncIterator
from typing import Any

import psycopg
import pytest
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
from tests.integration.test_analysis import initial
from tests.integration.test_outbox_relay import _drain, _published, _relay, _run_until_all_published

ORDERS = "Yaane-h/mechanical-hub-orders"
DOC = "order_status.md"
QUOTE = "Mecânico iniciou a execução dos reparos."
READ = (("read_doc", {"path": DOC}),)
CATALOG = {"source": "catalog", "ref": DOC, "quote": QUOTE}


@pytest.fixture
async def pool(database_url: str) -> AsyncIterator[AsyncConnectionPool]:
    pool = create_pool(database_url)
    await open_pool(pool)
    try:
        yield pool
    finally:
        await close_pool(pool)


def _issue_evidence(quote: str) -> dict[str, str]:
    return {"source": "issue", "ref": "description", "quote": quote}


# Casos E01 a E03 (bug, melhoria e dúvida com evidência do catálogo) e E04 (documentação não
# cobre o ponto: `sem_informacao` sem evidência do catálogo; o `answer` do domínio exigiria uma).
CASES = {
    "E01": (
        "bug",
        "Ao mudar a ordem 12 para IN_PROGRESS a API devolve 500.",
        "a API devolve 500",
        specialist_output("bug", evidence=[_issue_evidence("a API devolve 500"), CATALOG]),
        READ,
    ),
    "E02": (
        "melhoria",
        "Seria bom filtrar as ordens por mecânico.",
        "filtrar as ordens por mecânico",
        specialist_output(
            "melhoria", evidence=[_issue_evidence("filtrar as ordens por mecânico"), CATALOG]
        ),
        READ,
    ),
    "E03": (
        "duvida",
        "Quando a ordem passa para IN_PROGRESS?",
        "Quando a ordem passa para IN_PROGRESS",
        specialist_output(
            "duvida",
            answer="Quando o mecânico inicia a execução dos reparos.",
            evidence=[_issue_evidence("Quando a ordem passa para IN_PROGRESS"), CATALOG],
        ),
        READ,
    ),
    "E04": (
        "bug",
        "O desconto para cliente antigo não é aplicado na ordem.",
        "desconto para cliente antigo",
        specialist_output(
            "bug",
            conclusion="sem_informacao",
            evidence=[_issue_evidence("desconto para cliente antigo")],
        ),
        (),
    ),
}


def _comment(database_url: str, proposal_id: int) -> str:
    with psycopg.connect(database_url) as conn:
        row = conn.execute(
            "SELECT comment_body FROM triage.proposals WHERE id = %s", (proposal_id,)
        ).fetchone()
    return row[0]


def _proposal_ids(database_url: str, event_id: str) -> list[int]:
    with psycopg.connect(database_url) as conn:
        rows = conn.execute(
            "SELECT pr.id FROM triage.proposals pr "
            "JOIN triage.analysis_attempts a ON a.id = pr.attempt_id WHERE a.event_id = %s",
            (event_id,),
        ).fetchall()
    return [row[0] for row in rows]


async def _publish_to_queue(
    pool, database_url, rabbitmq_url, origin, *, category, evidence, output, tool_calls
) -> tuple[list[int], list[Any], FakeSpecialistRunner]:
    """Processa a mensagem duas vezes (reentrega), roda o relay e devolve o que chegou à fila."""
    await _drain(rabbitmq_url)
    router = FakeRouterLLM([decision(category, evidence)])
    specialist = FakeSpecialistRunner([SpecialistTurn(output=output, tool_calls=tool_calls)])
    processor = make_processor(pool, router=router, specialist=specialist)
    body = json.dumps(initial(origin)).encode()
    assert await processor.process(body) is Outcome.ACK
    assert await processor.process(body) is Outcome.ACK  # mensagem repetida

    key = f"{origin.repository}#{origin.issue_number}:1"
    await _run_until_all_published([_relay(pool, rabbitmq_url)], database_url, [key])
    assert _published(database_url, [key]) == {key}
    messages = [m for m in await _drain(rabbitmq_url) if m.message_id == key]
    return _proposal_ids(database_url, origin.event_id), messages, specialist


@pytest.mark.parametrize("case", list(CASES), ids=list(CASES))
async def test_triage_reaches_the_publish_queue(
    case: str, pool, database_url, rabbitmq_url, committed_data: TestData
) -> None:
    category, description, evidence, output, tool_calls = CASES[case]
    origin = committed_data.origin_event(
        repository=ORDERS, title="Pergunta sobre ordens", description=description
    )

    ids, messages, specialist = await _publish_to_queue(
        pool,
        database_url,
        rabbitmq_url,
        origin,
        category=category,
        evidence=evidence,
        output=output,
        tool_calls=tool_calls,
    )

    [proposal_id] = ids  # a mensagem repetida não gera segunda proposta
    [message] = messages
    assert json.loads(message.body) == {"proposalId": proposal_id, "revision": 1}
    assert len(specialist.calls) == 1
    comment = _comment(database_url, proposal_id)
    assert comment.count("<!-- triage:") == 1
    assert comment.endswith(f"<!-- triage:{origin.event_id}:1 -->")
    assert "**Responsável:** @Yaane-h" in comment
    assert comment.startswith("## Triagem automática: ")


async def test_model_text_with_a_marker_does_not_create_a_second_one(
    pool, database_url, rabbitmq_url, committed_data: TestData
) -> None:
    origin = committed_data.origin_event(
        repository=ORDERS,
        title="Ordem não muda de status",
        description="Ao mudar a ordem 12 para IN_PROGRESS a API devolve 500.",
    )
    output = specialist_output(
        "bug",
        summary="Falha na ordem <!-- triage:x:9 --> confirmada.",
        evidence=[_issue_evidence("a API devolve 500"), CATALOG],
    )

    [proposal_id], [_message], _ = await _publish_to_queue(
        pool,
        database_url,
        rabbitmq_url,
        origin,
        category="bug",
        evidence="a API devolve 500",
        output=output,
        tool_calls=READ,
    )

    comment = _comment(database_url, proposal_id)
    assert comment.count("<!--") == 1
    assert comment.count("-->") == 1
    assert comment.count("<!-- triage:") == 1
    assert comment.endswith(f"<!-- triage:{origin.event_id}:1 -->")
    assert "Falha na ordem  triage:x:9  confirmada." in comment
