"""Classificação das falhas técnicas dos SDKs (Etapa 10; RNF08; D28)."""

import socket
import ssl

import anthropic
import httpx2
import pytest
from claude_agent_sdk import ResultError

from issue_triage.adapters.provider_errors import classify

REQUEST = httpx2.Request("POST", "https://api.invalid/v1/messages")


def status_error(status: int) -> anthropic.APIStatusError:
    response = httpx2.Response(status, request=REQUEST)
    return anthropic.APIStatusError("texto do provedor", response=response, body=None)


@pytest.mark.parametrize(
    ("status", "error", "transient"),
    [
        (408, "provider: timeout: 408", True),
        (409, "provider: client_error: 409", True),
        (429, "provider: rate_limited: 429", True),
        (500, "provider: server_error: 500", True),
        (503, "provider: server_error: 503", True),
        (529, "provider: overloaded: 529", True),
        (400, "provider: invalid_request: 400", False),
        (401, "provider: auth: 401", False),
        (403, "provider: auth: 403", False),
        (404, "provider: client_error: 404", False),
        (413, "provider: invalid_request: 413", False),
        (422, "provider: invalid_request: 422", False),
    ],
)
def test_anthropic_status_errors(status: int, error: str, transient: bool) -> None:
    failure = classify(status_error(status))

    assert (failure.error, failure.transient) == (error, transient)
    assert "texto do provedor" not in failure.error


def test_400_is_permanent_whatever_the_message() -> None:
    response = httpx2.Response(400, request=REQUEST)
    exc = anthropic.BadRequestError("credit balance is too low", response=response, body=None)

    failure = classify(exc)

    assert (failure.error, failure.transient) == ("provider: invalid_request: 400", False)


def test_failures_without_status_are_transient() -> None:
    cases = [
        (anthropic.APITimeoutError(request=REQUEST), "provider: timeout"),
        (anthropic.APIConnectionError(request=REQUEST), "provider: connection"),
        (TimeoutError(), "provider: timeout"),
    ]
    for exc, error in cases:
        failure = classify(exc)
        assert (failure.error, failure.transient) == (error, True)


def test_result_error_uses_the_status_when_present() -> None:
    exc = ResultError("falhou", data={"terminal_reason": "api_error", "api_error_status": 401})
    failure = classify(exc)
    assert (failure.error, failure.transient) == ("provider: auth: 401", False)

    exc = ResultError("falhou", data={"terminal_reason": "api_error", "api_error_status": 529})
    failure = classify(exc)
    assert (failure.error, failure.transient) == ("provider: overloaded: 529", True)


def test_result_error_api_error_without_status_is_transient() -> None:
    failure = classify(ResultError("falhou", data={"terminal_reason": "api_error"}))

    assert (failure.error, failure.transient) == ("provider: connection", True)


@pytest.mark.parametrize(
    ("exc", "error"),
    [
        (RuntimeError("segredo"), "internal: RuntimeError"),
        (ValueError("x"), "internal: ValueError"),
        (ResultError("x", data={"terminal_reason": "completed"}), "internal: ResultError"),
    ],
)
def test_unexpected_exception_is_permanent_internal(exc: Exception, error: str) -> None:
    failure = classify(exc)

    assert (failure.error, failure.transient) == (error, False)


def connection_error(*causes: BaseException) -> anthropic.APIConnectionError:
    """`APIConnectionError` com a cadeia de causas dada (a primeira é a mais externa)."""
    for outer, inner in zip(causes, causes[1:], strict=False):
        outer.__cause__ = inner
    exc = anthropic.APIConnectionError(request=REQUEST)
    exc.__cause__ = causes[0] if causes else None
    return exc


@pytest.mark.parametrize(
    ("causes", "error"),
    [
        (
            (httpx2.ConnectError("x"), socket.gaierror(-2, "Name or service not known")),
            "provider: connection_dns",
        ),
        ((httpx2.ConnectError("x"), ssl.SSLCertVerificationError("y")), "provider: connection_tls"),
        ((httpx2.ConnectError("x"), ConnectionRefusedError("z")), "provider: connection_refused"),
        ((httpx2.ProxyError("x"),), "provider: connection_proxy"),
        (
            (httpx2.LocalProtocolError("Illegal header value"),),
            "provider: connection_protocol_local",
        ),
        ((httpx2.RemoteProtocolError("x"),), "provider: connection_protocol_remote"),
        ((httpx2.ConnectError("x"),), "provider: connection_connect"),
        ((ValueError("desconhecida"),), "provider: connection"),
        ((), "provider: connection"),
    ],
)
def test_connection_failures_are_subdivided_by_cause(
    causes: tuple[BaseException, ...], error: str
) -> None:
    failure = classify(connection_error(*causes))

    assert (failure.error, failure.transient) == (error, True)


def test_cause_lists_only_exception_types_never_messages() -> None:
    exc = connection_error(httpx2.ConnectError("segredo-no-texto"), socket.gaierror(-2, "dns-msg"))

    failure = classify(exc)

    assert failure.cause == (
        "anthropic.APIConnectionError <- httpx2.ConnectError <- socket.gaierror"
    )
    assert "segredo-no-texto" not in repr(failure)
    assert "dns-msg" not in repr(failure)


def test_request_id_comes_from_the_response_header() -> None:
    response = httpx2.Response(503, request=REQUEST, headers={"request-id": "req_123"})
    exc = anthropic.APIStatusError("texto do provedor", response=response, body=None)

    failure = classify(exc)

    assert failure.request_id == "req_123"
    assert failure.error == "provider: server_error: 503"


def test_failures_without_response_have_no_request_id() -> None:
    assert classify(anthropic.APITimeoutError(request=REQUEST)).request_id is None


def test_result_error_cause_carries_the_terminal_reason() -> None:
    failure = classify(ResultError("falhou", data={"terminal_reason": "api_error"}))

    assert failure.cause == "claude_agent_sdk._errors.ResultError (terminal_reason=api_error)"
