"""Grava um `issue.created` de teste em `inbound.issue_events` e envia a mensagem para a fila
`analysis` (ferramenta de desenvolvimento, D32). Não chama a Anthropic.

Uso:
    uv run python scripts/send_analysis.py --repository R --title T --description D \
        [--issue-number N] [--event-id ID]
    uv run python scripts/send_analysis.py --from-case R01

`--from-case ID` lê repositório, título e descrição de `tests/routing/fixtures/issues.yaml`.
`DATABASE_URL` e `RABBITMQ_URL` vêm do ambiente ou do `.env` (em desenvolvimento, o usuário
`postgres`). O `event_id` padrão é `dev-<uuid>`; sem `--issue-number`, usa-se o horário atual em
segundos (o número só precisa ser único por repositório). A fila `analysis` é declarada durável e
sem argumentos, como o consumidor a declara.
"""

import argparse
import asyncio
import hashlib
import json
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import aio_pika
import psycopg
from psycopg.types.json import Jsonb
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

ANALYSIS_QUEUE = "analysis"

_INSERT_EVENT = """
INSERT INTO inbound.issue_events
  (event_id, type, occurred_at, repository, issue_number, title, description, raw_payload,
   body_hash)
VALUES (%s, 'issue.created', now(), %s, %s, %s, %s, %s, %s)
"""


class _DevSettings(BaseSettings):
    """Só as duas URLs; não exige as demais variáveis do worker."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    DATABASE_URL: str | None = None
    RABBITMQ_URL: str | None = None


def build_message(event_id: str) -> dict[str, Any]:
    """Mensagem inicial da fila `analysis`."""
    return {"eventId": event_id, "attempt": 1, "trigger": "initial", "triggerRef": None}


def build_event(
    *, event_id: str, repository: str, issue_number: int, title: str, description: str
) -> dict[str, Any]:
    """Colunas do evento (D32): `raw_payload` com `source = send_analysis`, `body_hash` sha256."""
    raw_payload = {
        "source": "send_analysis",
        "repository": repository,
        "issueNumber": issue_number,
        "title": title,
        "description": description,
    }
    body = json.dumps(raw_payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return {
        "event_id": event_id,
        "repository": repository,
        "issue_number": issue_number,
        "title": title,
        "description": description,
        "raw_payload": raw_payload,
        "body_hash": hashlib.sha256(body).hexdigest(),
    }


def insert_event(database_url: str, event: dict[str, Any]) -> None:
    with psycopg.connect(database_url) as conn:
        conn.execute(
            _INSERT_EVENT,
            (
                event["event_id"],
                event["repository"],
                event["issue_number"],
                event["title"],
                event["description"],
                Jsonb(event["raw_payload"]),
                event["body_hash"],
            ),
        )


async def publish_message(rabbitmq_url: str, message: dict[str, Any]) -> None:
    connection = await aio_pika.connect(rabbitmq_url)
    try:
        channel = await connection.channel(publisher_confirms=True)
        await channel.declare_queue(ANALYSIS_QUEUE, durable=True)
        await channel.default_exchange.publish(
            aio_pika.Message(
                json.dumps(message).encode(),
                content_type="application/json",
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            ),
            routing_key=ANALYSIS_QUEUE,
            mandatory=True,
        )
    finally:
        await connection.close()


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Envia um issue.created de teste para analysis.")
    parser.add_argument("--repository", help="owner/nome do repositório")
    parser.add_argument("--title")
    parser.add_argument("--description")
    parser.add_argument("--from-case", metavar="ID", help="caso de tests/routing/fixtures")
    parser.add_argument("--issue-number", type=int)
    parser.add_argument("--event-id")
    args = parser.parse_args(argv)
    if args.from_case:
        if args.repository or args.title or args.description is not None:
            parser.error("--from-case não combina com --repository, --title e --description")
    elif not (args.repository and args.title and args.description is not None):
        parser.error("informe --repository, --title e --description, ou --from-case")
    if args.issue_number is not None and args.issue_number <= 0:
        parser.error("--issue-number precisa ser positivo")
    return args


def _case_text(case_id: str) -> tuple[str, str, str]:
    from tests.routing.cases import load_cases

    for case in load_cases().cases:
        if case.id == case_id:
            if case.stage == "specialist":
                case = next(c for c in load_cases().cases if c.id == case.case_ref)
            assert case.repository is not None and case.title is not None
            return case.repository, case.title, case.description or ""
    raise SystemExit(f"caso {case_id} não existe em issues.yaml")


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    settings = _DevSettings()
    if not settings.DATABASE_URL or not settings.RABBITMQ_URL:
        print(
            "DATABASE_URL e RABBITMQ_URL precisam estar definidas (ambiente ou .env).",
            file=sys.stderr,
        )
        return 2
    if args.from_case:
        repository, title, description = _case_text(args.from_case)
    else:
        repository, title, description = args.repository, args.title, args.description
    event = build_event(
        event_id=args.event_id or f"dev-{uuid.uuid4()}",
        repository=repository,
        issue_number=args.issue_number or int(time.time()),
        title=title,
        description=description,
    )
    insert_event(settings.DATABASE_URL, event)
    asyncio.run(publish_message(settings.RABBITMQ_URL, build_message(event["event_id"])))
    print(f"event {event['event_id']} ({repository}#{event['issue_number']}) sent to analysis")
    return 0


if __name__ == "__main__":
    sys.exit(main())
