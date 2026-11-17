"""Relay da `triage.publish_outbox`: publica na fila `publish` as mensagens gravadas junto com a
proposta (RF21, RNF05; decisões D6 e D7).

Roda em segundo plano, no mesmo processo do consumidor da fila `analysis` (D4). Lê as linhas
pendentes em ordem de `id` com `FOR UPDATE SKIP LOCKED`, publica cada uma com publisher confirms
e marca `published_at` na mesma transação da leitura, só depois da confirmação do broker. Se algo
falhar durante o lote, a transação é desfeita e as linhas são publicadas de novo na próxima volta
(entrega at-least-once; o consumidor do issue-publisher é idempotente).
"""

import asyncio
import contextlib
import json

import aio_pika
import psycopg
import structlog
from aio_pika.abc import AbstractChannel, AbstractConnection, AbstractQueue
from psycopg_pool import AsyncConnectionPool

PUBLISH_QUEUE = "publish"

# Backoff de reconexão ao broker (D7): começa em 1 s, dobra a cada falha e para em 30 s.
RECONNECT_BACKOFF_INITIAL_S = 1.0
RECONNECT_BACKOFF_MAX_S = 30.0

_SELECT_PENDING = """
SELECT id, event_id, topic, dedup_key, payload
FROM triage.publish_outbox
WHERE published_at IS NULL
ORDER BY id
LIMIT %(batch_size)s
FOR UPDATE SKIP LOCKED
"""

_MARK_PUBLISHED = """
UPDATE triage.publish_outbox SET published_at = now() WHERE id = ANY(%(ids)s)
"""

logger = structlog.get_logger()


async def declare_publish_queue(channel: AbstractChannel) -> AbstractQueue:
    """Declara a fila `publish` durável e sem argumentos, como o issue-publisher (D6)."""
    return await channel.declare_queue(PUBLISH_QUEUE, durable=True)


class OutboxRelay:
    """Tarefa em segundo plano que publica a `publish_outbox` no RabbitMQ."""

    def __init__(
        self,
        pool: AsyncConnectionPool,
        rabbitmq_url: str,
        *,
        poll_interval_s: float,
        batch_size: int,
        backoff_initial_s: float = RECONNECT_BACKOFF_INITIAL_S,
        backoff_max_s: float = RECONNECT_BACKOFF_MAX_S,
    ) -> None:
        self._pool = pool
        self._rabbitmq_url = rabbitmq_url
        self._poll_interval_s = poll_interval_s
        self._batch_size = batch_size
        self._backoff_initial_s = backoff_initial_s
        self._backoff_max_s = backoff_max_s
        self._connection: AbstractConnection | None = None
        self._channel: AbstractChannel | None = None
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """Inicia o relay como tarefa do loop atual, sem esperar o broker."""
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="outbox-relay")

    async def stop(self) -> None:
        """Para o relay e fecha a conexão com o broker (D4).

        Se a tarefa estiver no meio de um lote, a transação é desfeita e as linhas continuam
        pendentes (nenhuma linha é perdida: no pior caso, é publicada de novo).
        """
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await self._close_broker()

    async def _run(self) -> None:
        backoff_s = self._backoff_initial_s
        while True:
            if self._channel is None or self._channel.is_closed:
                try:
                    await self._connect()
                except Exception as exc:
                    logger.warning("outbox.publish_failed", stage="connect", error=_name(exc))
                    await self._close_broker()
                    await asyncio.sleep(backoff_s)
                    backoff_s = min(backoff_s * 2, self._backoff_max_s)
                    continue
                backoff_s = self._backoff_initial_s

            try:
                published = await self.publish_pending()
            except psycopg.Error as exc:
                # Banco indisponível: as linhas continuam pendentes; tenta na próxima volta.
                logger.warning("outbox.publish_failed", stage="database", error=_name(exc))
                await asyncio.sleep(self._poll_interval_s)
                continue
            except Exception as exc:
                # Falha no broker durante o lote: a transação foi desfeita e a conexão é refeita
                # com backoff.
                logger.warning("outbox.publish_failed", stage="publish", error=_name(exc))
                await self._close_broker()
                await asyncio.sleep(backoff_s)
                backoff_s = min(backoff_s * 2, self._backoff_max_s)
                continue

            if published < self._batch_size:
                await asyncio.sleep(self._poll_interval_s)

    async def _connect(self) -> None:
        self._connection = await aio_pika.connect(self._rabbitmq_url)
        self._channel = await self._connection.channel(publisher_confirms=True)
        await declare_publish_queue(self._channel)

    async def _close_broker(self) -> None:
        connection, self._connection, self._channel = self._connection, None, None
        if connection is not None and not connection.is_closed:
            with contextlib.suppress(Exception):
                await connection.close()

    async def publish_pending(self) -> int:
        """Publica um lote de linhas pendentes e marca `published_at`; devolve quantas publicou.

        Leitura com `FOR UPDATE SKIP LOCKED`, publicação e marcação na mesma transação: a
        marcação só acontece depois da confirmação do broker para todas as mensagens do lote.
        """
        if self._channel is None:
            raise RuntimeError("broker channel is not open")
        exchange = self._channel.default_exchange
        async with self._pool.connection() as conn, conn.transaction():
            cursor = await conn.execute(_SELECT_PENDING, {"batch_size": self._batch_size})
            rows = await cursor.fetchall()
            if not rows:
                return 0
            for _, _, topic, dedup_key, payload in rows:
                message = aio_pika.Message(
                    body=json.dumps(payload).encode(),
                    content_type="application/json",
                    delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                    message_id=dedup_key,
                )
                # Com publisher confirms, o `publish` só retorna depois do ack do broker.
                await exchange.publish(message, routing_key=topic)
            await self._mark_published(conn, [row[0] for row in rows])
        # Só depois do commit: as linhas estão marcadas como publicadas.
        for outbox_id, event_id, _, dedup_key, _ in rows:
            logger.info(
                "outbox.published", event_id=event_id, outbox_id=outbox_id, dedup_key=dedup_key
            )
        return len(rows)

    async def _mark_published(self, conn: psycopg.AsyncConnection, ids: list[int]) -> None:
        await conn.execute(_MARK_PUBLISHED, {"ids": ids})


def _name(exc: BaseException) -> str:
    """Só o tipo do erro: a mensagem pode conter a URL do broker com a senha."""
    return type(exc).__name__
