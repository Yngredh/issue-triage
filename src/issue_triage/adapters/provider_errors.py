"""Classificação das falhas técnicas dos dois SDKs (Issue Triage Spec, seção 6; D28).

Com status HTTP: transitórios 408, 409, 429 e qualquer 5xx; permanentes os demais 4xx. Sem status
(rede, tempo esgotado, `ResultError` com `terminal_reason == "api_error"` e sem
`api_error_status`): transitórios. Qualquer outra exceção da chamada não vem do provedor e é
permanente, com `error = "internal: <tipo da exceção>"`. O texto de `error` nunca leva mensagem
do provedor nem do SDK.

Para diagnóstico, a falha também leva `cause` (cadeia só com os tipos das exceções, nunca com
mensagens) e `request_id` (cabeçalho `request-id` da resposta, para achar a chamada na Console).
Falha de conexão com causa reconhecida ganha um sufixo no `error` (`provider: connection_dns`,
`connection_tls`, `connection_refused`, `connection_proxy`, `connection_protocol_local`,
`connection_protocol_remote`, `connection_connect`); sem causa reconhecida fica
`provider: connection`.
"""

import dataclasses
import socket
import ssl
from dataclasses import dataclass

import httpx2
from anthropic import APIConnectionError, APIStatusError, APITimeoutError
from claude_agent_sdk import ResultError

TRANSIENT_STATUSES = frozenset({408, 409, 429})


@dataclass(frozen=True)
class TechnicalFailure:
    """Falha técnica classificada; `error` vai para `analysis_attempts.error` e para os logs."""

    error: str
    transient: bool
    cause: str | None = None
    request_id: str | None = None


def _provider(
    kind: str,
    status: int | None,
    transient: bool,
    *,
    cause: str | None = None,
    request_id: str | None = None,
) -> TechnicalFailure:
    error = f"provider: {kind}" if status is None else f"provider: {kind}: {status}"
    return TechnicalFailure(error, transient, cause, request_id)


def _chain(exc: BaseException) -> list[BaseException]:
    """A exceção e as suas causas (`__cause__`, depois `__context__`), sem repetir nem laço."""
    chain: list[BaseException] = []
    current: BaseException | None = exc
    while current is not None and current not in chain and len(chain) < 6:
        chain.append(current)
        current = current.__cause__ or current.__context__
    return chain


def _type_name(exc: BaseException) -> str:
    cls = type(exc)
    if cls.__module__ == "builtins":
        return cls.__qualname__
    return f"{cls.__module__}.{cls.__qualname__}"


def describe_cause(exc: BaseException) -> str:
    """Tipos da cadeia de causas (`A <- B <- C`). Nunca inclui o texto das exceções."""
    names = " <- ".join(_type_name(item) for item in _chain(exc))
    if isinstance(exc, ResultError):
        return f"{names} (terminal_reason={exc.terminal_reason})"
    return names


def _request_id(exc: BaseException) -> str | None:
    value = getattr(exc, "request_id", None)
    if value is None:
        response = getattr(exc, "response", None)
        value = getattr(getattr(response, "headers", None), "get", lambda _name: None)("request-id")
    return value if isinstance(value, str) else None


def _connection_kind(exc: BaseException) -> str:
    """Subtipo da falha de conexão pela causa de rede, ou `connection` se não reconhecida."""
    chain = _chain(exc)
    checks: list[tuple[type[BaseException] | tuple[type[BaseException], ...], str]] = [
        (socket.gaierror, "connection_dns"),
        (ssl.SSLError, "connection_tls"),
        (ConnectionRefusedError, "connection_refused"),
        (httpx2.ProxyError, "connection_proxy"),
        (httpx2.LocalProtocolError, "connection_protocol_local"),
        (httpx2.RemoteProtocolError, "connection_protocol_remote"),
        (httpx2.ConnectError, "connection_connect"),
    ]
    for kind_types, kind in checks:
        if any(isinstance(item, kind_types) for item in chain):
            return kind
    return "connection"


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
    failure = _classify(exc)
    return dataclasses.replace(failure, cause=describe_cause(exc), request_id=_request_id(exc))


def _classify(exc: BaseException) -> TechnicalFailure:
    if isinstance(exc, APIStatusError):
        return _by_status(exc.status_code)
    if isinstance(exc, APITimeoutError | TimeoutError):
        return _provider("timeout", None, True)
    if isinstance(exc, APIConnectionError):
        return _provider(_connection_kind(exc), None, True)
    if isinstance(exc, ResultError):
        if exc.api_error_status is not None:
            return _by_status(exc.api_error_status)
        if exc.terminal_reason == "api_error":
            return _provider("connection", None, True)
    return TechnicalFailure(f"internal: {type(exc).__name__}", False)
