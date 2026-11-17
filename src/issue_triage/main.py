import asyncio
import sys

import structlog

from issue_triage.config import Settings
from issue_triage.logging import configure_logging
from issue_triage.worker import run_worker


def main() -> None:
    """Inicia o worker (D4); sem `ANTHROPIC_API_KEY` encerra com código 1 (D16)."""
    settings = Settings()
    configure_logging(settings)
    if settings.ANTHROPIC_API_KEY is None or not settings.ANTHROPIC_API_KEY.get_secret_value():
        structlog.get_logger().error("worker.missing_api_key")
        sys.exit(1)
    if sys.platform == "win32":
        # O psycopg assíncrono não funciona com o ProactorEventLoop, padrão no Windows.
        asyncio.run(run_worker(settings), loop_factory=asyncio.SelectorEventLoop)
    else:
        asyncio.run(run_worker(settings))


if __name__ == "__main__":
    main()
