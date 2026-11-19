import json
from collections.abc import Iterator

import pytest
import structlog

from issue_triage.config import Settings
from issue_triage.log_messages import MAIN_FLOW_EVENTS, MESSAGES, add_message
from issue_triage.logging import configure_logging


@pytest.mark.parametrize("event", MAIN_FLOW_EVENTS)
def test_main_flow_event_has_friendly_message(event: str) -> None:
    message = MESSAGES.get(event)
    assert message
    assert message != event


def test_add_message_known_event() -> None:
    result = add_message(None, "info", {"event": "analysis.received"})
    assert result["message"] == MESSAGES["analysis.received"]


def test_add_message_unknown_event_falls_back_to_event_name() -> None:
    assert add_message(None, "info", {"event": "x.y"})["message"] == "x.y"


def test_add_message_keeps_explicit_message() -> None:
    result = add_message(None, "info", {"event": "analysis.received", "message": "meu texto"})
    assert result["message"] == "meu texto"


@pytest.fixture
def reset_structlog() -> Iterator[None]:
    yield
    structlog.reset_defaults()
    structlog.contextvars.clear_contextvars()


def _emit_received() -> None:
    structlog.get_logger().info("analysis.received", trigger="initial")


def test_log_line_has_module_func_name_and_message(
    reset_structlog: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir("/")  # evita ler um .env local
    configure_logging(
        Settings(
            DATABASE_URL="postgresql://user:pass@localhost:5432/db",
            RABBITMQ_URL="amqp://guest:guest@localhost:5672/",
        )
    )
    _emit_received()
    line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert line["module"] == "test_log_messages"
    assert line["func_name"] == "_emit_received"
    assert line["message"] == MESSAGES["analysis.received"]
    assert line["event"] == "analysis.received"
    assert line["service"] == "issue-triage"
    assert line["level"] == "info"
    assert line["trigger"] == "initial"
