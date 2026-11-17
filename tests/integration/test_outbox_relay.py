"""Relay da `publish_outbox` com PostgreSQL e RabbitMQ do testcontainers (Etapa 5; RF21, RNF05)."""

import asyncio
import json
import socket
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator

import aio_pika
import psycopg
import pytest
from aio_pika.abc import AbstractIncomingMessage
from aio_pika.exceptions import ChannelPreconditionFailed
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool
from testcontainers.community.rabbitmq import RabbitMqContainer

from issue_triage.adapters.database import close_pool, create_pool, open_pool
from issue_triage.adapters.outbox_relay import PUBLISH_QUEUE, OutboxRelay
from issue_triage.analysis import AnalysisProcessor
from issue_triage.domain.messages import Outcome
from tests.conftest import TestData

ORDERS = "Yaane-h/mechanical-hub-orders"
TIMEOUT_S = 30.0


@pytest.fixture
async def pool(database_url: str) -> AsyncIterator[AsyncConnectionPool]:
    pool = create_pool(database_url)
    await open_pool(pool)
    try:
        yield pool
    finally:
        await close_pool(pool)


@pytest.fixture
def outbox_cleanup(committed_data: TestData) -> Iterator[None]:
    """Linhas gravadas direto na outbox são apagadas no fim do teste pela `committed_data`."""
    yield


def _relay(pool: AsyncConnectionPool, rabbitmq_url: str, **kwargs: float) -> OutboxRelay:
    params: dict = {
        "poll_interval_s": 0.1,
        "batch_size": 100,
        "backoff_initial_s": 0.2,
        "backoff_max_s": 1.0,
    }
    params.update(kwargs)
    return OutboxRelay(pool, rabbitmq_url, **params)


def _insert_outbox(database_url: str, count: int) -> list[str]:
    """Grava `count` linhas pendentes na outbox, em ordem de `id`, e devolve as `dedup_key`."""
    keys: list[str] = []
    with psycopg.connect(database_url, autocommit=True) as conn:
        for index in range(count):
            event_id = f"relay-{uuid.uuid4()}"
            dedup_key = f"{ORDERS}#{index + 1}-{uuid.uuid4().hex[:8]}:1"
            conn.execute(
                "INSERT INTO triage.publish_outbox (event_id, topic, dedup_key, payload) "
                "VALUES (%s, 'publish', %s, %s)",
                (event_id, dedup_key, Jsonb({"proposalId": index + 1, "revision": 1})),
            )
            keys.append(dedup_key)
    return keys


def _published(database_url: str, keys: list[str]) -> set[str]:
    with psycopg.connect(database_url) as conn:
        rows = conn.execute(
            "SELECT dedup_key FROM triage.publish_outbox "
            "WHERE dedup_key = ANY(%s) AND published_at IS NOT NULL",
            (keys,),
        ).fetchall()
    return {row[0] for row in rows}


async def _wait_until(condition: Callable[[], bool], timeout_s: float = TIMEOUT_S) -> None:
    deadline = time.monotonic() + timeout_s
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached before timeout")
        await asyncio.sleep(0.1)


async def _drain(rabbitmq_url: str) -> list[AbstractIncomingMessage]:
    """Consome tudo o que está na fila `publish`, em ordem."""
    messages: list[AbstractIncomingMessage] = []
    connection = await aio_pika.connect(rabbitmq_url)
    async with connection:
        channel = await connection.channel()
        queue = await channel.declare_queue(PUBLISH_QUEUE, durable=True)
        while (message := await queue.get(no_ack=True, fail=False)) is not None:
            messages.append(message)
    return messages


async def _with_relays(relays: list[OutboxRelay], body: Callable[[], Awaitable[None]]) -> None:
    for relay in relays:
        relay.start()
    try:
        await body()
    finally:
        for relay in relays:
            await relay.stop()


async def _run_until_all_published(
    relays: list[OutboxRelay], database_url: str, keys: list[str]
) -> None:
    await _with_relays(
        relays, lambda: _wait_until(lambda: _published(database_url, keys) == set(keys))
    )


async def test_h05_missing_info_reaches_publish_queue(
    pool, database_url, rabbitmq_url, committed_data: TestData
) -> None:
    await _drain(rabbitmq_url)
    # Caso H05: issue sem descrição gera a proposta `missing_info` e a linha na outbox.
    origin = committed_data.origin_event(repository=ORDERS, description="")
    body = {"eventId": origin.event_id, "attempt": 1, "trigger": "initial", "triggerRef": None}
    processor = AnalysisProcessor(pool, max_manual_attempts=2)
    assert await processor.process(json.dumps(body).encode()) is Outcome.ACK

    with psycopg.connect(database_url) as conn:
        [(proposal_id,)] = conn.execute(
            "SELECT pr.id FROM triage.proposals pr "
            "JOIN triage.analysis_attempts a ON a.id = pr.attempt_id WHERE a.event_id = %s",
            (origin.event_id,),
        ).fetchall()
    dedup_key = f"{ORDERS}#{origin.issue_number}:1"

    await _run_until_all_published([_relay(pool, rabbitmq_url)], database_url, [dedup_key])

    [message] = [m for m in await _drain(rabbitmq_url) if m.message_id == dedup_key]
    assert json.loads(message.body) == {"proposalId": proposal_id, "revision": 1}
    assert message.content_type == "application/json"
    assert message.delivery_mode == aio_pika.DeliveryMode.PERSISTENT


async def test_rows_are_published_in_order_across_batches(
    pool, database_url, rabbitmq_url, outbox_cleanup
) -> None:
    await _drain(rabbitmq_url)
    keys = _insert_outbox(database_url, 5)

    await _run_until_all_published([_relay(pool, rabbitmq_url, batch_size=2)], database_url, keys)

    messages = [m for m in await _drain(rabbitmq_url) if m.message_id in keys]
    assert [m.message_id for m in messages] == keys
    assert [json.loads(m.body) for m in messages] == [
        {"proposalId": n, "revision": 1} for n in range(1, 6)
    ]


async def test_publish_queue_is_declared_durable_without_arguments(
    pool, database_url, rabbitmq_url, outbox_cleanup
) -> None:
    keys = _insert_outbox(database_url, 1)
    await _run_until_all_published([_relay(pool, rabbitmq_url)], database_url, keys)

    connection = await aio_pika.connect(rabbitmq_url)
    async with connection:
        # A mesma declaração (durável, sem argumentos) é aceita: os parâmetros batem (D6).
        channel = await connection.channel()
        await channel.declare_queue(PUBLISH_QUEUE, durable=True)
        with pytest.raises(ChannelPreconditionFailed):
            await channel.declare_queue(PUBLISH_QUEUE, durable=False)
        channel = await connection.channel()
        with pytest.raises(ChannelPreconditionFailed):
            await channel.declare_queue(
                PUBLISH_QUEUE, durable=True, arguments={"x-dead-letter-exchange": "publish.dlx"}
            )


async def test_two_relays_do_not_publish_the_same_row_twice(
    pool, database_url, rabbitmq_url, outbox_cleanup
) -> None:
    await _drain(rabbitmq_url)
    keys = _insert_outbox(database_url, 200)
    relays = [_relay(pool, rabbitmq_url, batch_size=5) for _ in range(2)]

    await _run_until_all_published(relays, database_url, keys)

    delivered = [m.message_id for m in await _drain(rabbitmq_url) if m.message_id in keys]
    assert sorted(delivered) == sorted(keys)


class _FailOnceBeforeMarking(OutboxRelay):
    """Simula a queda entre a confirmação do broker e a marcação de `published_at`."""

    failed = False

    async def _mark_published(self, conn: psycopg.AsyncConnection, ids: list[int]) -> None:
        if not self.failed:
            self.failed = True
            raise ConnectionError("simulated crash after publishing")
        await super()._mark_published(conn, ids)


async def test_failure_during_batch_rolls_back_and_republishes(
    pool, database_url, rabbitmq_url, outbox_cleanup
) -> None:
    await _drain(rabbitmq_url)
    keys = _insert_outbox(database_url, 1)
    relay = _FailOnceBeforeMarking(
        pool, rabbitmq_url, poll_interval_s=0.1, batch_size=100, backoff_initial_s=0.2
    )

    await _run_until_all_published([relay], database_url, keys)

    assert relay.failed
    delivered = [m.message_id for m in await _drain(rabbitmq_url) if m.message_id in keys]
    # At-least-once (D7): a mensagem confirmada e não marcada é publicada de novo.
    assert delivered == [keys[0], keys[0]]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def test_broker_down_keeps_rows_pending_until_it_returns(
    pool, database_url, outbox_cleanup
) -> None:
    # Porta fixa no host: o broker volta no mesmo endereço depois de parado.
    broker = RabbitMqContainer("rabbitmq:3-management").with_bind_ports(5672, _free_port())
    with broker:
        host = broker.get_container_host_ip()
        port = broker.get_exposed_port(broker.port)
        url = f"amqp://{broker.username}:{broker.password}@{host}:{port}/"
        relay = _relay(pool, url)
        relay.start()
        try:
            first = _insert_outbox(database_url, 1)
            await _wait_until(lambda: _published(database_url, first) == set(first))

            await asyncio.to_thread(broker.get_wrapped_container().stop)
            pending = _insert_outbox(database_url, 3)
            await asyncio.sleep(2)
            assert _published(database_url, pending) == set()

            await asyncio.to_thread(broker.get_wrapped_container().start)
            await _wait_until(lambda: _published(database_url, pending) == set(pending), 90)
        finally:
            await relay.stop()

        delivered = [m.message_id for m in await _drain(url) if m.message_id in pending]
        assert delivered == pending
