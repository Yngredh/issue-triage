"""Modelo da mensagem da fila `analysis` ("Contratos > Mensagem da fila `analysis`")."""

import json

import pytest
from pydantic import ValidationError

from issue_triage.domain.messages import AnalysisMessage

VALID = [
    {"eventId": "evt-001", "attempt": 1, "trigger": "initial", "triggerRef": None},
    {"eventId": "evt-001", "attempt": None, "trigger": "author_edit", "triggerRef": "dlv-9"},
    {"eventId": "evt-001", "attempt": 3, "trigger": "enrich", "triggerRef": "17"},
    {"eventId": "evt-001", "attempt": 3, "trigger": "reprocess", "triggerRef": "18"},
    {"eventId": "evt-001", "attempt": None, "trigger": "close", "triggerRef": "19"},
]


@pytest.mark.parametrize("payload", VALID)
def test_contract_examples_are_valid(payload: dict) -> None:
    message = AnalysisMessage.model_validate_json(json.dumps(payload))

    assert message.event_id == "evt-001"
    assert message.trigger == payload["trigger"]
    assert message.attempt == payload["attempt"]
    assert message.trigger_ref == payload["triggerRef"]


INVALID = [
    {"eventId": "", "attempt": 1, "trigger": "initial", "triggerRef": None},
    {"eventId": "evt-001", "attempt": 0, "trigger": "initial", "triggerRef": None},
    {"eventId": "evt-001", "attempt": "1", "trigger": "initial", "triggerRef": None},
    {"eventId": "evt-001", "attempt": True, "trigger": "initial", "triggerRef": None},
    {"eventId": "evt-001", "attempt": None, "trigger": "initial", "triggerRef": None},
    {"eventId": "evt-001", "attempt": 2, "trigger": "author_edit", "triggerRef": "dlv-9"},
    {"eventId": "evt-001", "attempt": None, "trigger": "author_edit", "triggerRef": None},
    {"eventId": "evt-001", "attempt": 3, "trigger": "enrich", "triggerRef": None},
    {"eventId": "evt-001", "attempt": 3, "trigger": "reprocess", "triggerRef": ""},
    {"eventId": "evt-001", "attempt": 3, "trigger": "close", "triggerRef": "19"},
    {"eventId": "evt-001", "attempt": None, "trigger": "close", "triggerRef": None},
    {"eventId": "evt-001", "attempt": None, "trigger": "close", "triggerRef": ""},
    {"eventId": "evt-001", "attempt": None, "trigger": "purge", "triggerRef": "19"},
    {"eventId": "evt-001", "attempt": 1, "trigger": "initial", "triggerRef": None, "x": 1},
    {"eventId": "evt-001", "trigger": "initial", "triggerRef": None},
    {"attempt": 1, "trigger": "initial", "triggerRef": None},
]


@pytest.mark.parametrize("payload", INVALID)
def test_invalid_messages_are_rejected(payload: dict) -> None:
    with pytest.raises(ValidationError):
        AnalysisMessage.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize("body", [b"", b"not json", b"[]", b"null"])
def test_non_object_bodies_are_rejected(body: bytes) -> None:
    with pytest.raises(ValidationError):
        AnalysisMessage.model_validate_json(body)
