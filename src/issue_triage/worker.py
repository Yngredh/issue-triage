import asyncio
import signal
import sys
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

import structlog

from issue_triage.adapters.analysis_consumer import AnalysisConsumer
from issue_triage.adapters.anthropic_router import AnthropicRouterLLM
from issue_triage.adapters.claude_specialist import ClaudeSpecialistRunner
from issue_triage.adapters.database import close_pool, create_pool, open_pool
from issue_triage.adapters.outbox_relay import OutboxRelay
from issue_triage.analysis import AnalysisProcessor
from issue_triage.config import Settings

logger = structlog.get_logger()


def install_stop_handlers(loop: asyncio.AbstractEventLoop, stop: Callable[[], None]) -> None:
    """Liga SIGTERM e SIGINT à função `stop` (D4).

    No Windows o loop não suporta `add_signal_handler`; o handler do módulo `signal`
    agenda `stop` no loop de forma segura entre threads.
    """
    for sig in (signal.SIGTERM, signal.SIGINT):
        if sys.platform == "win32":
            signal.signal(sig, lambda _signum, _frame: loop.call_soon_threadsafe(stop))
        else:
            loop.add_signal_handler(sig, stop)


ServicesFactory = Callable[[Settings], AbstractAsyncContextManager[None]]


@asynccontextmanager
async def worker_services(settings: Settings) -> AsyncIterator[None]:
    """Pool do banco, roteador, especialistas, consumidor da `analysis` e relay da outbox (D4).

    O roteador usa o SDK `anthropic` e os especialistas o `claude-agent-sdk`, ambos com
    `ANTHROPIC_API_KEY`, que `main.py` exige na subida.

    Ativos enquanto o bloco dura. Na saída o consumidor para de consumir e termina a mensagem em
    curso, o relay para (um lote em curso é desfeito e fica pendente) e só então o pool fecha.
    """
    if settings.ANTHROPIC_API_KEY is None:
        raise ValueError("ANTHROPIC_API_KEY is required")
    router = AnthropicRouterLLM(
        settings.ANTHROPIC_API_KEY,
        model=settings.ROUTER_MODEL,
        max_tokens=settings.ROUTER_MAX_TOKENS,
        timeout_s=settings.ROUTER_TIMEOUT_S,
    )
    specialist = ClaudeSpecialistRunner(
        settings.ANTHROPIC_API_KEY,
        max_turns=settings.SPECIALIST_MAX_TURNS,
        max_tool_calls=settings.SPECIALIST_MAX_TOOL_CALLS,
        max_budget_usd=settings.SPECIALIST_MAX_BUDGET_USD,
        max_read_chars=settings.SPECIALIST_MAX_READ_CHARS,
        timeout_s=settings.SPECIALIST_TIMEOUT_S,
    )
    pool = create_pool(settings.DATABASE_URL)
    await open_pool(pool)
    processor = AnalysisProcessor(
        pool,
        router=router,
        specialist=specialist,
        specialist_models={
            "bug": settings.BUG_SPECIALIST_MODEL,
            "melhoria": settings.IMPROVEMENT_SPECIALIST_MODEL,
            "dominio": settings.DOMAIN_SPECIALIST_MODEL,
        },
        catalog_source_dir=settings.CATALOG_SOURCE_DIR,
        max_manual_attempts=settings.MAX_MANUAL_ATTEMPTS,
    )
    consumer = AnalysisConsumer(
        processor.process, settings.RABBITMQ_URL, prefetch=settings.ANALYSIS_PREFETCH
    )
    relay = OutboxRelay(
        pool,
        settings.RABBITMQ_URL,
        poll_interval_s=settings.RELAY_POLL_INTERVAL_S,
        batch_size=settings.RELAY_BATCH_SIZE,
    )
    consumer.start()
    relay.start()
    try:
        yield
    finally:
        await consumer.stop()
        await relay.stop()
        await close_pool(pool)


class Worker:
    """Worker sem HTTP do issue-triage (D4).

    Roda até o pedido de parada. O consumidor da fila `analysis` (Etapa 4) e o relay da
    `publish_outbox` (Etapa 5) rodam como tarefas deste mesmo processo, criadas por `services`;
    ao receber o pedido de parada, deixam de consumir, terminam a mensagem em curso e fecham as
    conexões.
    """

    def __init__(self, settings: Settings, services: ServicesFactory = worker_services) -> None:
        self._settings = settings
        self._services = services
        self._stop = asyncio.Event()

    def request_stop(self) -> None:
        """Pede o encerramento limpo do worker."""
        if not self._stop.is_set():
            logger.info("worker.stop_requested")
            self._stop.set()

    async def run(self) -> None:
        """Executa até o pedido de parada."""
        logger.info("worker.started", log_level=self._settings.LOG_LEVEL)
        try:
            async with self._services(self._settings):
                await self._stop.wait()
        finally:
            logger.info("worker.stopped")


async def run_worker(settings: Settings) -> None:
    """Cria o worker, liga os sinais de parada e executa até SIGTERM/SIGINT."""
    worker = Worker(settings)
    install_stop_handlers(asyncio.get_running_loop(), worker.request_stop)
    await worker.run()
