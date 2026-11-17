"""Leitura do schema `inbound` (dono: issue-analyst-api); o issue-triage nunca escreve nele."""

from psycopg import AsyncConnection

from issue_triage.domain.issue import IssueText, OriginEvent, is_blank


async def find_origin_event(conn: AsyncConnection, event_id: str) -> OriginEvent | None:
    """Devolve o `issue.created` de origem com esse `event_id`, ou `None` se não existir."""
    cur = await conn.execute(
        "SELECT event_id, repository, issue_number FROM inbound.issue_events "
        "WHERE event_id = %s AND type = 'issue.created'",
        (event_id,),
    )
    row = await cur.fetchone()
    if row is None:
        return None
    return OriginEvent(event_id=row[0], repository=row[1], issue_number=row[2])


async def read_issue_text(conn: AsyncConnection, origin: OriginEvent) -> IssueText:
    """Texto atual da issue e enriquecimentos do revisor (D14).

    O texto é a versão mais recente recebida: o último `issue.edited` da issue, por
    `received_at`, ou o `issue.created` de origem se não houver edição. A descrição vazia segue
    a regra de `is_blank` (D19): vazia ou só com espaços em branco de qualquer tipo.
    """
    cur = await conn.execute(
        "SELECT title, description FROM inbound.issue_events "
        "WHERE repository = %s AND issue_number = %s "
        "  AND (event_id = %s OR type = 'issue.edited') "
        "ORDER BY (type = 'issue.edited') DESC, received_at DESC, event_id DESC "
        "LIMIT 1",
        (origin.repository, origin.issue_number, origin.event_id),
    )
    row = await cur.fetchone()
    if row is None:  # o evento de origem foi lido no passo 2 e nunca é apagado
        raise LookupError(f"origin event {origin.event_id} not found")
    title, description = row

    cur = await conn.execute(
        "SELECT enrichment_description FROM inbound.issue_review_history "
        "WHERE origin_event_id = %s AND action = 'enrich' ORDER BY created_at, id",
        (origin.event_id,),
    )
    enrichments = tuple(r[0] for r in await cur.fetchall())
    return IssueText(
        title=title,
        description=description,
        description_empty=is_blank(description),
        enrichments=enrichments,
    )
