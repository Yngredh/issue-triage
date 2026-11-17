"""Fixtures compartilhadas dos testes."""

import asyncio
import hashlib
import itertools
import sys
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import psycopg
import pytest
from psycopg.types.json import Jsonb
from testcontainers.community.postgres import PostgresContainer

from issue_triage.migrate import apply_migrations

if sys.platform == "win32":
    # O psycopg assíncrono não funciona com o ProactorEventLoop, padrão no Windows.
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

ROOT = Path(__file__).resolve().parents[1]
# Stub só de teste de `inbound` (D9); nunca vai para `migrations/`.
INBOUND_STUB = ROOT / "tests" / "fixtures" / "sql" / "inbound_stub.sql"
# DDL do schema `catalog` (D8); nos testes as fixtures aplicam o arquivo.
CATALOG_SCHEMA = ROOT / "catalog" / "schema.sql"


@pytest.fixture(scope="session")
def postgres_container() -> Iterator[PostgresContainer]:
    """PostgreSQL 16 do testcontainers (imagem da D5), um por sessão de testes."""
    with PostgresContainer("postgres:16", driver=None) as container:
        yield container


@pytest.fixture(scope="session")
def database_url(postgres_container: PostgresContainer) -> str:
    """URL do PostgreSQL da sessão com o stub de `inbound`, `catalog/schema.sql` e as migrações."""
    url = postgres_container.get_connection_url()
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(INBOUND_STUB.read_text(encoding="utf-8"))
        conn.execute(CATALOG_SCHEMA.read_text(encoding="utf-8"))
    apply_migrations(url)
    return url


@pytest.fixture
def db_conn(database_url: str) -> Iterator[psycopg.Connection]:
    """Conexão dentro de uma transação desfeita ao fim do teste.

    Blocos `conn.transaction()` dentro do teste viram savepoints dessa transação externa.
    """
    with psycopg.connect(database_url) as conn:
        with conn.transaction(force_rollback=True):
            yield conn


@dataclass(frozen=True)
class IssueEvent:
    """Linha de `inbound.issue_events` criada pela fábrica."""

    event_id: str
    type: str
    repository: str
    issue_number: int
    title: str
    description: str


@dataclass(frozen=True)
class ReviewEntry:
    """Linha de `inbound.issue_review_history` criada pela fábrica."""

    id: int
    origin_event_id: str
    action: str
    target_attempt: int | None
    enrichment_description: str | None


@dataclass(frozen=True)
class CatalogProject:
    """Projeto de `catalog.projects` com as orientações de `catalog.triage_guidelines`."""

    repository: str
    name: str
    responsible: str
    description: str
    guidelines: tuple[tuple[str | None, str], ...]


_issue_numbers = itertools.count(1)


class TestData:
    """Fábrica de dados de teste: eventos de `inbound`, resoluções do revisor e catálogo.

    Grava pela conexão recebida; com a conexão de `db_conn`, tudo é desfeito ao fim do teste.
    """

    __test__ = False  # não é uma classe de teste do pytest

    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn

    def _insert_event(
        self,
        *,
        event_type: str,
        event_id: str,
        repository: str,
        issue_number: int,
        title: str,
        description: str,
    ) -> IssueEvent:
        payload = {"repository": repository, "issueNumber": issue_number, "title": title}
        body_hash = hashlib.sha256(description.encode("utf-8")).hexdigest()
        self._conn.execute(
            "INSERT INTO inbound.issue_events "
            "(event_id, type, occurred_at, repository, issue_number, title, description, "
            " raw_payload, body_hash) "
            "VALUES (%s, %s, now(), %s, %s, %s, %s, %s, %s)",
            (
                event_id,
                event_type,
                repository,
                issue_number,
                title,
                description,
                Jsonb(payload),
                body_hash,
            ),
        )
        return IssueEvent(event_id, event_type, repository, issue_number, title, description)

    def origin_event(
        self,
        *,
        repository: str = "Yaane-h/mechanical-hub-orders",
        issue_number: int | None = None,
        title: str = "Erro ao mudar o status da ordem",
        description: str = "Ao mudar a ordem 123 para IN_PROGRESS a API devolve 500.",
        event_id: str | None = None,
    ) -> IssueEvent:
        """Cria o `issue.created` de origem da issue."""
        return self._insert_event(
            event_type="issue.created",
            event_id=event_id or f"evt-{uuid.uuid4()}",
            repository=repository,
            issue_number=issue_number if issue_number is not None else next(_issue_numbers),
            title=title,
            description=description,
        )

    def edit_event(
        self,
        origin: IssueEvent,
        *,
        title: str | None = None,
        description: str = "Descrição completada pelo autor.",
        event_id: str | None = None,
    ) -> IssueEvent:
        """Cria um `issue.edited` da mesma issue do evento de origem."""
        return self._insert_event(
            event_type="issue.edited",
            event_id=event_id or f"evt-{uuid.uuid4()}",
            repository=origin.repository,
            issue_number=origin.issue_number,
            title=title if title is not None else origin.title,
            description=description,
        )

    def review(
        self,
        origin: IssueEvent,
        *,
        action: str = "enrich",
        target_attempt: int | None = 2,
        enrichment_description: str | None = None,
        final_status: str | None = None,
        comment: str | None = None,
        from_status: str = "NEEDS_REVIEW",
        from_reason: str | None = "insufficient_content",
        actor: str = "reviewer",
    ) -> ReviewEntry:
        """Cria uma resolução do revisor (`enrich`, `reprocess` ou `close`)."""
        if action == "enrich" and enrichment_description is None:
            enrichment_description = "Informação adicional do revisor."
        if action == "close":
            target_attempt = None
            final_status = final_status or "CANCELLED"
            comment = comment or "Encerrada pelo revisor."
        review_id = self._conn.execute(
            "INSERT INTO inbound.issue_review_history "
            "(origin_event_id, repository, issue_number, action, from_status, from_reason, "
            " enrichment_description, final_status, comment, target_attempt, actor) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (
                origin.event_id,
                origin.repository,
                origin.issue_number,
                action,
                from_status,
                from_reason,
                enrichment_description,
                final_status,
                comment,
                target_attempt,
                actor,
            ),
        ).fetchone()[0]
        return ReviewEntry(
            review_id, origin.event_id, action, target_attempt, enrichment_description
        )

    def project(
        self,
        *,
        repository: str = "Yaane-h/mechanical-hub-orders",
        name: str = "Ordens de Serviço",
        responsible: str = "@Yaane-h",
        description: str = "Abre e acompanha ordens de serviço.",
        guidelines: tuple[tuple[str | None, str], ...] = (
            (None, "Peça o número da ordem de serviço e o status em que ela estava."),
            ("bug", "Erros em mudança de status devem pedir o status atual e o pedido."),
        ),
    ) -> CatalogProject:
        """Cria (ou substitui) o projeto do catálogo com as suas orientações."""
        self._conn.execute(
            "INSERT INTO catalog.projects (repository, name, responsible, description) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT (repository) DO UPDATE SET "
            "name = EXCLUDED.name, responsible = EXCLUDED.responsible, "
            "description = EXCLUDED.description",
            (repository, name, responsible, description),
        )
        self._conn.execute(
            "DELETE FROM catalog.triage_guidelines WHERE repository = %s", (repository,)
        )
        for category, guideline in guidelines:
            self._conn.execute(
                "INSERT INTO catalog.triage_guidelines (repository, category, guideline) "
                "VALUES (%s, %s, %s)",
                (repository, category, guideline),
            )
        return CatalogProject(repository, name, responsible, description, guidelines)


@pytest.fixture
def data(db_conn: psycopg.Connection) -> TestData:
    """Fábrica de dados de teste ligada à conexão do teste (desfeita ao fim)."""
    return TestData(db_conn)
