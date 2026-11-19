"""Consumidor da fila `analysis` (D3, D6): ack manual, só depois do commit.

Banco ou broker fora: a mensagem não é confirmada (o broker a devolve à fila quando a conexão
fecha) e a conexão é refeita com espera de 1 s dobrando até 30 s (Issue Triage Spec, seção 6).
"""

import asyncio
import contextlib
from collections.abc import Awaitable, Callable

import aio_pika
import structlog
from aio_pika.abc import AbstractChannel, AbstractConnection, AbstractIncomingMessage, AbstractQueue

from issue_triage.domain.messages import Outcome

ANALYSIS_QUEUE = "analysis"
ERROR_DETAIL_MAX_CHARS = 300

logger = structlog.get_logger()

RECONNECT_BACKOFF_INITIAL_S = 1.0
RECONNECT_BACKOFF_MAX_S = 30.0

Processor = Callable[[bytes], Awaitable[Outcome]]


async def declare_analysis_queue(channel: AbstractChannel) -> AbstractQueue:
    """Declara a fila `analysis` durável e sem argumentos, igual ao issue-analyst-api (D6)."""
    return await channel.declare_queue(ANALYSIS_QUEUE, durable=True)


class AnalysisConsumer:
    """Tarefa em segundo plano que consome a fila `analysis` com `prefetch` mensagens."""

    def __init__(
        self,
        process: Processor,
        rabbitmq_url: str,
        *,
        prefetch: int,
        backoff_initial_s: float = RECONNECT_BACKOFF_INITIAL_S,
        backoff_max_s: float = RECONNECT_BACKOFF_MAX_S,
    ) -> None:
        self._process = process
        self._rabbitmq_url = rabbitmq_url
        self._prefetch = prefetch
        self._backoff_initial_s = backoff_initial_s
        self._backoff_max_s = backoff_max_s
        self._backoff_s = backoff_initial_s
        self._connection: AbstractConnection | None = None
        self._task: asyncio.Task[None] | None = None
        self._in_flight: asyncio.Task[None] | None = None

    def start(self) -> None:
        """Inicia o consumidor como tarefa do loop atual, sem esperar o broker."""
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="analysis-consumer")

    async def stop(self) -> None:
        """Para de consumir, termina a mensagem em curso e fecha a conexão (D4)."""
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        in_flight, self._in_flight = self._in_flight, None
        if in_flight is not None:
            with contextlib.suppress(Exception):
                await in_flight
        await self._close_broker()

    async def _run(self) -> None:
        while True:
            try:
                await self._consume()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Falha no banco ou no broker: a mensagem sem ack volta à fila quando a conexão
                # fecha; a espera só volta a 1 s depois de uma mensagem concluída. O erro é
                # registrado (D34), senão a reentrega infinita fica sem causa visível.
                logger.error(
                    "worker.consumer_error",
                    error_type=type(exc).__name__,
                    detail=str(exc)[:ERROR_DETAIL_MAX_CHARS],
                    retry_in_s=self._backoff_s,
                )
            await self._close_broker()
            await asyncio.sleep(self._backoff_s)
            self._backoff_s = min(self._backoff_s * 2, self._backoff_max_s)

    async def _consume(self) -> None:
        self._connection = await aio_pika.connect(self._rabbitmq_url)
        channel = await self._connection.channel()
        await channel.set_qos(prefetch_count=self._prefetch)
        queue = await declare_analysis_queue(channel)
        async with queue.iterator() as messages:
            async for message in messages:
                await self._handle(message)
        # O iterador termina quando o canal fecha: trata como queda do broker.
        raise ConnectionError("analysis channel closed")

    async def _handle(self, message: AbstractIncomingMessage) -> None:
        # A mensagem em curso termina mesmo com o pedido de parada (D4).
        task = asyncio.create_task(self._process_and_settle(message))
        self._in_flight = task
        await asyncio.shield(task)
        self._in_flight = None

    async def _process_and_settle(self, message: AbstractIncomingMessage) -> None:
        outcome = await self._process(message.body)
        if outcome is Outcome.ACK:
            await message.ack()
        else:
            await message.nack(requeue=True)
        self._backoff_s = self._backoff_initial_s

    async def _close_broker(self) -> None:
        connection, self._connection = self._connection, None
        if connection is not None and not connection.is_closed:
            with contextlib.suppress(Exception):
                await connection.close()
