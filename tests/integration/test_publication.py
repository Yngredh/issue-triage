"""Saída para publicação de ponta a ponta (Etapa 9; RF19, RF21, RNF09, RNF10).

Mensagem na entrada do processador com `FakeRouterLLM` e `FakeSpecialistRunner`, PostgreSQL e
RabbitMQ reais (testcontainers) e o relay da outbox: a proposta chega à fila `publish`. Nenhuma
chamada à Anthropic.
"""

import json
from collections.abc import AsyncIterator, Callable, Iterator
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


# Casos E01 a E04 da documentação "Casos de teste — Roteador e especialistas" (D27). O plano só
# descreve cada caso (E01: bug, R01, erro 500 no login, responsável @ana-teste; E02: bug, R04,
# erro 500 no Safari; E03: melhoria, R02, exportação CSV; E04: domínio, R09, reembolso parcial,
# responsável @bruno-teste); os textos da issue, o repositório e o trecho do catálogo abaixo são
# representativos. Em E04 o catálogo não cobre o ponto: a resposta diz que não há informação,
# aponta a documentação da API e o responsável, com evidência do catálogo (checagem 5).
CASES = {
    "E01": (
        "bug",
        "Erro 500 no login",
        "Ao entrar com usuário e senha corretos o login devolve erro 500.",
        "o login devolve erro 500",
        specialist_output(
            "bug",
            summary="O login devolve erro 500 com credenciais corretas.",
            evidence=[_issue_evidence("o login devolve erro 500"), CATALOG],
        ),
        "@ana-teste",
    ),
    "E02": (
        "bug",
        "Erro 500 no Safari",
        "No Safari a tela de ordens abre com erro 500, no Chrome funciona.",
        "No Safari a tela de ordens abre com erro 500",
        specialist_output(
            "bug",
            summary="A tela de ordens devolve erro 500 só no Safari.",
            evidence=[_issue_evidence("No Safari a tela de ordens abre com erro 500"), CATALOG],
        ),
        "@Yaane-h",
    ),
    "E03": (
        "melhoria",
        "Exportar ordens em CSV",
        "Seria útil exportar a lista de ordens em CSV.",
        "exportar a lista de ordens em CSV",
        specialist_output(
            "melhoria",
            summary="Pedido de exportação da lista de ordens em CSV.",
            evidence=[_issue_evidence("exportar a lista de ordens em CSV"), CATALOG],
        ),
        "@Yaane-h",
    ),
    "E04": (
        "duvida",
        "Reembolso parcial",
        "O sistema permite reembolso parcial de uma ordem já paga?",
        "reembolso parcial de uma ordem já paga",
        specialist_output(
            "duvida",
            summary="Dúvida sobre reembolso parcial de uma ordem paga.",
            conclusion="sem_informacao",
            conclusion_reason="A documentação do projeto não trata de reembolso parcial.",
            answer=(
                "A documentação do projeto não tem informação sobre reembolso parcial. "
                "Consulte a documentação da API ou fale com @bruno-teste."
            ),
            evidence=[_issue_evidence("reembolso parcial de uma ordem já paga"), CATALOG],
        ),
        "@bruno-teste",
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


@pytest.fixture
def set_responsible(database_url: str) -> Iterator[Callable[[str], None]]:
    """Troca o responsável do projeto de `ORDERS` e restaura o da seed ao fim do teste."""
    with psycopg.connect(database_url, autocommit=True) as conn:
        original = conn.execute(
            "SELECT responsible FROM catalog.projects WHERE repository = %s", (ORDERS,)
        ).fetchone()[0]

        def change(responsible: str) -> None:
            conn.execute(
                "UPDATE catalog.projects SET responsible = %s WHERE repository = %s",
                (responsible, ORDERS),
            )

        try:
            yield change
        finally:
            change(original)


@pytest.mark.parametrize("case", list(CASES), ids=list(CASES))
async def test_triage_reaches_the_publish_queue(
    case: str, pool, database_url, rabbitmq_url, committed_data: TestData, set_responsible
) -> None:
    category, title, description, evidence, output, responsible = CASES[case]
    set_responsible(responsible)
    origin = committed_data.origin_event(repository=ORDERS, title=title, description=description)

    ids, messages, specialist = await _publish_to_queue(
        pool,
        database_url,
        rabbitmq_url,
        origin,
        category=category,
        evidence=evidence,
        output=output,
        tool_calls=READ,
    )

    [proposal_id] = ids  # a mensagem repetida não gera segunda proposta
    [message] = messages
    assert json.loads(message.body) == {"proposalId": proposal_id, "revision": 1}
    assert len(specialist.calls) == 1
    comment = _comment(database_url, proposal_id)
    assert comment.count("<!-- triage:") == 1
    assert comment.endswith(f"<!-- triage:{origin.event_id}:1 -->")
    assert f"**Responsável:** {responsible}" in comment
    assert comment.startswith("## Triagem automática: ")
    if case == "E04":
        assert "não tem informação sobre reembolso parcial" in comment
        assert "documentação da API" in comment
        assert "@bruno-teste" in comment


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
