"""Leitura do schema `catalog` (somente leitura; os dados vêm da seed, D8)."""

from psycopg import AsyncConnection

from issue_triage.domain.catalog import Project, TriageGuideline


async def find_project(conn: AsyncConnection, repository: str) -> Project | None:
    """Devolve o projeto do repositório ou `None` se ele não estiver no catálogo (RF10)."""
    cur = await conn.execute(
        "SELECT repository, name, responsible, description "
        "FROM catalog.projects WHERE repository = %s",
        (repository,),
    )
    row = await cur.fetchone()
    if row is None:
        return None
    return Project(repository=row[0], name=row[1], responsible=row[2], description=row[3])


async def list_guidelines(conn: AsyncConnection, repository: str) -> list[TriageGuideline]:
    """Devolve todas as orientações do repositório, na ordem em que foram cadastradas."""
    cur = await conn.execute(
        "SELECT category, guideline FROM catalog.triage_guidelines "
        "WHERE repository = %s ORDER BY id",
        (repository,),
    )
    return [TriageGuideline(category=row[0], guideline=row[1]) for row in await cur.fetchall()]
