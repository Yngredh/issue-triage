import pytest
import structlog
from pydantic import ValidationError

from issue_triage.config import Settings

API_KEY = "sk-ant-test-key-never-logged"

_ALL_VARS = (
    "LOG_LEVEL",
    "SERVICE_NAME",
    "DATABASE_URL",
    "RABBITMQ_URL",
    "ANTHROPIC_API_KEY",
    "ROUTER_MODEL",
    "BUG_SPECIALIST_MODEL",
    "IMPROVEMENT_SPECIALIST_MODEL",
    "DOMAIN_SPECIALIST_MODEL",
    "ROUTER_MAX_TOKENS",
    "ROUTER_TIMEOUT_S",
    "SPECIALIST_MAX_TURNS",
    "SPECIALIST_MAX_TOOL_CALLS",
    "SPECIALIST_MAX_BUDGET_USD",
    "SPECIALIST_MAX_READ_CHARS",
    "SPECIALIST_TIMEOUT_S",
    "ANALYSIS_RETRY_DELAYS_S",
    "ANALYSIS_PREFETCH",
    "MAX_MANUAL_ATTEMPTS",
    "CATALOG_SOURCE_DIR",
    "RELAY_POLL_INTERVAL_S",
    "RELAY_BATCH_SIZE",
)

_POSITIVE_LIMITS = (
    "ROUTER_MAX_TOKENS",
    "ROUTER_TIMEOUT_S",
    "SPECIALIST_MAX_TURNS",
    "SPECIALIST_MAX_TOOL_CALLS",
    "SPECIALIST_MAX_BUDGET_USD",
    "SPECIALIST_MAX_READ_CHARS",
    "SPECIALIST_TIMEOUT_S",
    "ANALYSIS_PREFETCH",
    "MAX_MANUAL_ATTEMPTS",
    "RELAY_POLL_INTERVAL_S",
    "RELAY_BATCH_SIZE",
)


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _ALL_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir("/")  # evita ler um .env local
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@localhost:5432/db")
    monkeypatch.setenv("RABBITMQ_URL", "amqp://guest:guest@localhost:5672/")


def test_settings_load_with_defaults() -> None:
    settings = Settings()

    assert settings.LOG_LEVEL == "INFO"
    assert settings.SERVICE_NAME == "issue-triage"
    assert settings.ANTHROPIC_API_KEY is None
    assert settings.ROUTER_MODEL == "claude-haiku-5-5"
    assert settings.BUG_SPECIALIST_MODEL == "claude-haiku-5-5"
    assert settings.IMPROVEMENT_SPECIALIST_MODEL == "claude-haiku-5-5"
    assert settings.DOMAIN_SPECIALIST_MODEL == "claude-haiku-5-5"
    assert settings.ROUTER_MAX_TOKENS == 300
    assert settings.ROUTER_TIMEOUT_S == 20
    assert settings.SPECIALIST_MAX_TURNS == 6
    assert settings.SPECIALIST_MAX_TOOL_CALLS == 8
    assert settings.SPECIALIST_MAX_BUDGET_USD == 0.05
    assert settings.SPECIALIST_MAX_READ_CHARS == 20000
    assert settings.SPECIALIST_TIMEOUT_S == 90
    assert settings.ANALYSIS_RETRY_DELAYS_S == [10, 30]
    assert settings.ANALYSIS_PREFETCH == 1
    assert settings.MAX_MANUAL_ATTEMPTS == 2
    assert settings.CATALOG_SOURCE_DIR == "catalog/source"
    assert settings.RELAY_POLL_INTERVAL_S == 1
    assert settings.RELAY_BATCH_SIZE == 100


def test_settings_read_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("ANTHROPIC_API_KEY", API_KEY)
    monkeypatch.setenv("ROUTER_MODEL", "other-model")
    monkeypatch.setenv("SPECIALIST_MAX_BUDGET_USD", "0.1")

    settings = Settings()

    assert settings.LOG_LEVEL == "DEBUG"
    assert settings.ANTHROPIC_API_KEY is not None
    assert settings.ANTHROPIC_API_KEY.get_secret_value() == API_KEY
    assert settings.ROUTER_MODEL == "other-model"
    assert settings.SPECIALIST_MAX_BUDGET_USD == 0.1


@pytest.mark.parametrize("name", ["DATABASE_URL", "RABBITMQ_URL"])
def test_settings_fail_without_required_url(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    monkeypatch.delenv(name)

    with pytest.raises(ValidationError, match=name):
        Settings()


def test_retry_delays_parsed_from_comma_separated_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANALYSIS_RETRY_DELAYS_S", "5, 15,45")

    assert Settings().ANALYSIS_RETRY_DELAYS_S == [5, 15, 45]


@pytest.mark.parametrize("value", ["10,abc", "10,0", "-1"])
def test_retry_delays_reject_invalid_values(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("ANALYSIS_RETRY_DELAYS_S", value)

    with pytest.raises(ValidationError, match="ANALYSIS_RETRY_DELAYS_S"):
        Settings()


@pytest.mark.parametrize("name", _POSITIVE_LIMITS)
@pytest.mark.parametrize("value", ["0", "-1"])
def test_limits_must_be_positive(monkeypatch: pytest.MonkeyPatch, name: str, value: str) -> None:
    monkeypatch.setenv(name, value)

    with pytest.raises(ValidationError, match=name):
        Settings()


def test_limit_rejects_non_numeric(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPECIALIST_MAX_TURNS", "six")

    with pytest.raises(ValidationError, match="SPECIALIST_MAX_TURNS"):
        Settings()


def test_api_key_not_in_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", API_KEY)
    settings = Settings()

    assert API_KEY not in repr(settings)
    assert API_KEY not in str(settings)
    assert API_KEY not in repr(settings.model_dump())


def test_api_key_not_in_log(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", API_KEY)
    settings = Settings()

    with structlog.testing.capture_logs() as logs:
        structlog.get_logger().info("settings.loaded", settings=settings.model_dump())

    assert logs
    assert API_KEY not in repr(logs)
