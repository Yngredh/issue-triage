"""Classificação das falhas técnicas dos dois SDKs (Issue Triage Spec, seção 6; D28).

Com status HTTP: transitórios 408, 409, 429 e qualquer 5xx; permanentes os demais 4xx. Sem status
(rede, tempo esgotado, `ResultError` com `terminal_reason == "api_error"` e sem
`api_error_status`): transitórios. Qualquer outra exceção da chamada não vem do provedor e é
permanente, com `error = "internal: <tipo da exceção>"`. O texto de `error` nunca leva mensagem
do provedor nem do SDK.
"""

from dataclasses import dataclass

from anthropic import APIConnectionError, APIStatusError, APITimeoutError
from claude_agent_sdk import ResultError

TRANSIENT_STATUSES = frozenset({408, 409, 429})


@dataclass(frozen=True)
class TechnicalFailure:
    """Falha técnica classificada; `error` vai para `analysis_attempts.error` e para os logs."""

    error: str
    transient: bool


def _provider(kind: str, status: int | None, transient: bool) -> TechnicalFailure:
    error = f"provider: {kind}" if status is None else f"provider: {kind}: {status}"
    return TechnicalFailure(error, transient)


def _kind_for_status(status: int) -> str:
    if status == 429:
        return "rate_limited"
    if status == 529:
        return "overloaded"
    if status == 408:
        return "timeout"
    if status >= 500:
        return "server_error"
    if status in (401, 403):
        return "auth"
    if status in (400, 413, 422):
        return "invalid_request"
    return "client_error"


def _by_status(status: int) -> TechnicalFailure:
    transient = status in TRANSIENT_STATUSES or status >= 500
    return _provider(_kind_for_status(status), status, transient)


def classify(exc: BaseException) -> TechnicalFailure:
    """Classifica a exceção levantada pela chamada ao roteador ou ao especialista."""
    if isinstance(exc, APIStatusError):
        return _by_status(exc.status_code)
    if isinstance(exc, APITimeoutError | TimeoutError):
        return _provider("timeout", None, True)
    if isinstance(exc, APIConnectionError):
        return _provider("connection", None, True)
    if isinstance(exc, ResultError):
        if exc.api_error_status is not None:
            return _by_status(exc.api_error_status)
        if exc.terminal_reason == "api_error":
            return _provider("connection", None, True)
    return TechnicalFailure(f"internal: {type(exc).__name__}", False)
