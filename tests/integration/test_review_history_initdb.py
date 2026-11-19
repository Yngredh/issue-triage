"""`infra/initdb/04_inbound_review_history.sql` (D34).

A tabela `inbound.issue_review_history` não vem de nenhuma migração dos serviços; no ambiente
integrado ela vem do initdb. Cada teste usa um PostgreSQL só dele, porque papéis são do cluster.
"""

import re
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from psycopg import errors
from psycopg.conninfo import make_conninfo
from testcontainers.community.postgres import PostgresContainer

ROOT = Path(__file__).resolve().parents[2]
ROLES_SQL = ROOT / "infra" / "initdb" / "00_roles.sql"
REVIEW_SQL = ROOT / "infra" / "initdb" / "04_inbound_review_history.sql"
STUB_SQL = ROOT / "tests" / "fixtures" / "sql" / "inbound_stub.sql"
TABLE = "inbound.issue_review_history"


def table_ddl(text: str) -> str:
    """Do `CREATE TABLE` da tabela até o índice único `..._one_close`, sem comentários."""
    start = text.index(f"CREATE TABLE {TABLE}")
    end = text.index(";", text.index("issue_review_history_one_close")) + 1
    return re.sub(r"\s+", " ", text[start:end]).strip()


@pytest.fixture
def cluster() -> Iterator[PostgresContainer]:
    with PostgresContainer("postgres:16", driver=None) as container:
        yield container


def as_role(container: PostgresContainer, role: str) -> str:
    return make_conninfo(container.get_connection_url(), user=role, password=role)


def test_initdb_ddl_is_the_same_as_the_test_stub() -> None:
    initdb = table_ddl(REVIEW_SQL.read_text(encoding="utf-8"))
    stub = table_ddl(STUB_SQL.read_text(encoding="utf-8"))
    assert initdb == stub


def test_triage_reads_the_table_created_by_the_initdb(cluster: PostgresContainer) -> None:
    with psycopg.connect(cluster.get_connection_url(), autocommit=True) as admin:
        admin.execute(ROLES_SQL.read_text(encoding="utf-8"))
        admin.execute(REVIEW_SQL.read_text(encoding="utf-8"))

    with psycopg.connect(as_role(cluster, "triage_svc")) as conn:
        assert conn.execute(f"SELECT count(*) FROM {TABLE}").fetchone()[0] == 0
        with pytest.raises(errors.InsufficientPrivilege):
            conn.execute(f"DELETE FROM {TABLE}")

    with psycopg.connect(cluster.get_connection_url()) as admin:
        owner = admin.execute(
            "SELECT tableowner FROM pg_tables "
            "WHERE schemaname = 'inbound' AND tablename = 'issue_review_history'"
        ).fetchone()[0]
    assert owner == "analyst_api"
