import asyncio
import signal
import sys
from collections.abc import Callable

import structlog

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


class Worker:
    """Worker sem HTTP do issue-triage (D4).

    Roda até o pedido de parada. O consumidor da fila `analysis` (Etapa 4) e o relay da
    `publish_outbox` (Etapa 5) entram como tarefas deste mesmo processo; ao receber o pedido de
    parada, deixam de consumir, terminam a mensagem em curso e fecham as conexões.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
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
            await self._stop.wait()
        finally:
            logger.info("worker.stopped")


async def run_worker(settings: Settings) -> None:
    """Cria o worker, liga os sinais de parada e executa até SIGTERM/SIGINT."""
    worker = Worker(settings)
    install_stop_handlers(asyncio.get_running_loop(), worker.request_stop)
    await worker.run()
