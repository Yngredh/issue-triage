import asyncio
import os
import signal
import sys

import pytest
import structlog

from issue_triage import main as main_module
from issue_triage.config import Settings
from issue_triage.worker import Worker, install_stop_handlers

API_KEY = "sk-ant-test-key-never-logged"


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.chdir("/")  # evita ler um .env local
    return Settings(
        DATABASE_URL="postgresql://user:pass@localhost:5432/db",
        RABBITMQ_URL="amqp://guest:guest@localhost:5672/",
        ANTHROPIC_API_KEY=API_KEY,
    )


async def test_worker_runs_until_stop_requested(settings: Settings) -> None:
    worker = Worker(settings)

    with structlog.testing.capture_logs() as logs:
        task = asyncio.create_task(worker.run())
        await asyncio.sleep(0.05)
        assert not task.done()
        worker.request_stop()
        worker.request_stop()  # pedido repetido não gera outro log
        await asyncio.wait_for(task, timeout=5)

    events = [entry["event"] for entry in logs]
    assert events == ["worker.started", "worker.stop_requested", "worker.stopped"]
    assert API_KEY not in repr(logs)


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
async def test_signal_requests_stop(sig: signal.Signals) -> None:
    loop = asyncio.get_running_loop()
    stopped = asyncio.Event()
    previous = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}

    try:
        install_stop_handlers(loop, stopped.set)
        if sys.platform == "win32":
            handler = signal.getsignal(sig)
            assert callable(handler)
            handler(sig, None)
        else:
            os.kill(os.getpid(), sig)
        await asyncio.wait_for(stopped.wait(), timeout=1)
    finally:
        for s, handler in previous.items():
            if sys.platform != "win32":
                loop.remove_signal_handler(s)
            signal.signal(s, handler)


def test_main_exits_without_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir("/")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@localhost:5432/db")
    monkeypatch.setenv("RABBITMQ_URL", "amqp://guest:guest@localhost:5672/")
    monkeypatch.setattr(main_module, "configure_logging", lambda _settings: None)

    async def _must_not_run(_settings: Settings) -> None:
        raise AssertionError("worker must not start without the API key")

    monkeypatch.setattr(main_module, "run_worker", _must_not_run)

    with structlog.testing.capture_logs() as logs, pytest.raises(SystemExit) as exc:
        main_module.main()

    assert exc.value.code == 1
    assert [entry["event"] for entry in logs] == ["worker.missing_api_key"]
