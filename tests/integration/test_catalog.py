"""Catálogo: schema, seed, conteúdo de `catalog/source` e leitura (Etapa 3)."""

import psycopg

from issue_triage.adapters.catalog import find_project, list_guidelines
from issue_triage.adapters.database import close_pool, create_pool, open_pool
from issue_triage.domain.catalog import TriageGuideline
from tests.conftest import CATALOG_SEED, ROOT

SOURCE_DIR = ROOT / "catalog" / "source"

REGISTERED_REPOSITORIES = {
    "Yaane-h/mechanical-hub-orders",
    "Yaane-h/mechanical-hub-billing",
    "Yaane-h/mechanical-hub-stock",
    "Yaane-h/mechanical-hub-orchestrator",
}

EXPECTED_GUIDELINE_COUNTS = {
    "Yaane-h/mechanical-hub-orders": 3,
    "Yaane-h/mechanical-hub-billing": 2,
    "Yaane-h/mechanical-hub-stock": 1,
    "Yaane-h/mechanical-hub-orchestrator": 2,
}


def guideline_counts(conn: psycopg.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT repository, count(*) FROM catalog.triage_guidelines GROUP BY repository"
    ).fetchall()
    return {repository: count for repository, count in rows}


def test_seed_runs_twice_without_duplicating_guidelines(database_url: str) -> None:
    seed = CATALOG_SEED.read_text(encoding="utf-8")
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute(seed)
        conn.execute(seed)
        projects = {
            row[0] for row in conn.execute("SELECT repository FROM catalog.projects").fetchall()
        }
        assert projects == REGISTERED_REPOSITORIES
        assert guideline_counts(conn) == EXPECTED_GUIDELINE_COUNTS


def test_unknown_repository_is_left_out_of_the_seed(db_conn: psycopg.Connection) -> None:
    row = db_conn.execute(
        "SELECT 1 FROM catalog.projects WHERE repository = %s",
        ("Yaane-h/mechanical-hub-auth",),
    ).fetchone()
    assert row is None


async def test_reader_finds_project_and_guidelines(database_url: str) -> None:
    pool = create_pool(database_url)
    await open_pool(pool)
    try:
        async with pool.connection() as conn:
            project = await find_project(conn, "Yaane-h/mechanical-hub-billing")
            guidelines = await list_guidelines(conn, "Yaane-h/mechanical-hub-billing")
    finally:
        await close_pool(pool)

    assert project is not None
    assert project.name == "Orçamento e Pagamento"
    assert project.responsible == "@Yaane-h"
    assert project.description.startswith("Calcula o orçamento")
    assert guidelines == [
        TriageGuideline(None, "Nunca peça dados de cartão ou dados bancários."),
        TriageGuideline(
            "bug", "Problemas de pagamento devem pedir o `orderId` e o horário da tentativa."
        ),
    ]


async def test_reader_does_not_find_repository_outside_catalog(database_url: str) -> None:
    pool = create_pool(database_url)
    await open_pool(pool)
    try:
        async with pool.connection() as conn:
            assert await find_project(conn, "Yaane-h/mechanical-hub-auth") is None
            assert await list_guidelines(conn, "Yaane-h/mechanical-hub-auth") == []
    finally:
        await close_pool(pool)


def test_every_registered_repository_has_a_source_folder(db_conn: psycopg.Connection) -> None:
    repositories = [
        row[0] for row in db_conn.execute("SELECT repository FROM catalog.projects").fetchall()
    ]
    assert set(repositories) == REGISTERED_REPOSITORIES
    for repository in repositories:
        folder = SOURCE_DIR / repository.split("/", 1)[1]
        assert folder.is_dir(), f"sem pasta em catalog/source para {repository}"


def test_every_source_folder_has_the_service_spec() -> None:
    folders = [path for path in SOURCE_DIR.iterdir() if path.is_dir()]
    assert {folder.name for folder in folders} == {
        repository.split("/", 1)[1] for repository in REGISTERED_REPOSITORIES
    }
    for folder in folders:
        assert (folder / f"{folder.name}-spec.md").is_file(), f"sem spec em {folder.name}"
