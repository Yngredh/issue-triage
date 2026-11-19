"""`scripts/send_analysis.py` contra PostgreSQL e RabbitMQ reais de teste (Etapa 13, D32)."""

import asyncio
import hashlib
import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any

import aio_pika
import psycopg
import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "send_analysis.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("send_analysis_script", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


send_analysis = _load()


async def _read_one(rabbitmq_url: str) -> dict[str, Any]:
    connection = await aio_pika.connect(rabbitmq_url)
    try:
        channel = await connection.channel()
        queue = await channel.declare_queue("analysis", durable=True)
        message = await queue.get(timeout=5)
        await message.ack()
        return json.loads(message.body)
    finally:
        await connection.close()


def _delete_event(database_url: str, event_id: str) -> int:
    with psycopg.connect(database_url) as conn:
        count = conn.execute(
            "DELETE FROM inbound.issue_events WHERE event_id = %s", (event_id,)
        ).rowcount
        conn.commit()
    return count


def test_from_case_writes_the_event_and_publishes_the_message(
    database_url: str, rabbitmq_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("RABBITMQ_URL", rabbitmq_url)

    code = send_analysis.main(
        ["--from-case", "R01", "--issue-number", "9001", "--event-id", "dev-t1"]
    )

    assert code == 0
    with psycopg.connect(database_url) as conn:
        row = conn.execute(
            "SELECT type, repository, issue_number, title, raw_payload, body_hash "
            "FROM inbound.issue_events WHERE event_id = 'dev-t1'"
        ).fetchone()
    _delete_event(database_url, "dev-t1")
    assert row is not None
    kind, repository, number, title, payload, body_hash = row
    assert (kind, repository, number) == ("issue.created", "Yaane-h/mechanical-hub-orders", 9001)
    assert title == "Erro ao abrir ordem de serviço"
    assert payload["source"] == "send_analysis"
    body = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    assert body_hash == hashlib.sha256(body).hexdigest()
    assert asyncio.run(_read_one(rabbitmq_url)) == {
        "eventId": "dev-t1",
        "attempt": 1,
        "trigger": "initial",
        "triggerRef": None,
    }


def test_explicit_arguments_use_a_dev_uuid_event_id(
    database_url: str, rabbitmq_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("RABBITMQ_URL", rabbitmq_url)

    code = send_analysis.main(
        [
            "--repository",
            "Yaane-h/mechanical-hub-stock",
            "--title",
            "T",
            "--description",
            "",
            "--issue-number",
            "9002",
        ]
    )

    assert code == 0
    message = asyncio.run(_read_one(rabbitmq_url))
    assert message["eventId"].startswith("dev-")
    assert _delete_event(database_url, message["eventId"]) == 1


def test_arguments_are_validated() -> None:
    with pytest.raises(SystemExit):
        send_analysis.main(["--title", "só o título"])
    with pytest.raises(SystemExit):
        send_analysis.main(["--from-case", "R01", "--title", "T"])
