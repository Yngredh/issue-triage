from typing import Annotated

from pydantic import Field, PositiveInt, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

DEFAULT_MODEL = "claude-haiku-5-5"


class Settings(BaseSettings):
    """Configuração do processo, lida das variáveis de ambiente e de um `.env` opcional.

    `ANTHROPIC_API_KEY` é segredo (D16): fica em `SecretStr` e nunca aparece no `repr` nem em
    log. É opcional aqui para que os testes rodem com as portas falsas sem chave; o worker real
    exige a chave na subida (`main.py`).
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    LOG_LEVEL: str = "INFO"
    SERVICE_NAME: str = "issue-triage"
    DATABASE_URL: str
    RABBITMQ_URL: str
    ANTHROPIC_API_KEY: SecretStr | None = None
    ROUTER_MODEL: str = DEFAULT_MODEL
    BUG_SPECIALIST_MODEL: str = DEFAULT_MODEL
    IMPROVEMENT_SPECIALIST_MODEL: str = DEFAULT_MODEL
    DOMAIN_SPECIALIST_MODEL: str = DEFAULT_MODEL
    ROUTER_MAX_TOKENS: int = Field(default=300, gt=0)
    ROUTER_TIMEOUT_S: float = Field(default=20, gt=0)
    SPECIALIST_MAX_TURNS: int = Field(default=6, gt=0)
    SPECIALIST_MAX_TOOL_CALLS: int = Field(default=8, gt=0)
    SPECIALIST_MAX_BUDGET_USD: float = Field(default=0.05, gt=0)
    SPECIALIST_MAX_READ_CHARS: int = Field(default=20000, gt=0)
    SPECIALIST_TIMEOUT_S: float = Field(default=90, gt=0)
    ANALYSIS_RETRY_DELAYS_S: Annotated[list[PositiveInt], NoDecode] = [10, 30]
    ANALYSIS_PREFETCH: int = Field(default=1, gt=0)
    MAX_MANUAL_ATTEMPTS: int = Field(default=2, gt=0)
    CATALOG_SOURCE_DIR: str = "catalog/source"
    RELAY_POLL_INTERVAL_S: float = Field(default=1, gt=0)
    RELAY_BATCH_SIZE: int = Field(default=100, gt=0)

    @field_validator("ANALYSIS_RETRY_DELAYS_S", mode="before")
    @classmethod
    def _parse_delays(cls, value: object) -> object:
        """Aceita a lista separada por vírgulas da variável de ambiente (`10,30`)."""
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value
