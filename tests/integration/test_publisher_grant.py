"""Migração 0002: `GRANT SELECT` da view ao `publisher_svc` (Etapa 13, D32).

Cada teste usa um PostgreSQL só dele, porque papéis são do cluster inteiro e o `publisher_svc`
não pode existir nos demais testes.
"""

from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from psycopg import errors
from psycopg.conninfo import make_conninfo
from testcontainers.community.postgres import PostgresContainer

from issue_triage.migrate import apply_migrations

ROLES_SQL = Path(__file__).resolve().parents[2] / "infra" / "initdb" / "00_roles.sql"
VIEW = "triage.publishable_proposals"


@pytest.fixture
def cluster() -> Iterator[PostgresContainer]:
    with PostgresContainer("postgres:16", driver=None) as container:
        yield container


def as_role(container: PostgresContainer, role: str) -> str:
    return make_conninfo(container.get_connection_url(), user=role, password=role)


def test_migration_passes_without_the_publisher_role(cluster: PostgresContainer) -> None:
    url = cluster.get_connection_url()
    applied = apply_migrations(url)
    assert applied[-1] == "0002"
    with psycopg.connect(url) as conn:
        roles = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = 'publisher_svc'").fetchall()
        assert roles == []


def test_publisher_reads_the_view_but_not_proposals(cluster: PostgresContainer) -> None:
    with psycopg.connect(cluster.get_connection_url(), autocommit=True) as admin:
        admin.execute(ROLES_SQL.read_text(encoding="utf-8"))
    # Cada migração roda como o dono do schema (D32).
    applied = apply_migrations(as_role(cluster, "triage_svc"))
    assert applied == ["0001", "0002"]

    with psycopg.connect(as_role(cluster, "publisher_svc")) as conn:
        assert conn.execute(f"SELECT count(*) FROM {VIEW}").fetchone()[0] == 0
        with pytest.raises(errors.InsufficientPrivilege):
            conn.execute("SELECT count(*) FROM triage.proposals")
