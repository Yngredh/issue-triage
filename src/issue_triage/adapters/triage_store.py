"""Escrita no schema `triage`: caso, tentativa, proposta e `publish_outbox` (D15).

Cada função roda dentro da transação aberta por quem chama; o processamento da mensagem
(`issue_triage.analysis`) decide onde fica cada commit.
"""

from dataclasses import dataclass

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

PUBLISH_TOPIC = "publish"
SUPERSEDED = "superseded"


@dataclass(frozen=True)
class OpenedAttempt:
    """Tentativa aberta (ou retomada, se `resumed`) no commit 1 da D15."""

    case_id: int
    attempt_id: int
    attempt: int
    resumed: bool


@dataclass(frozen=True)
class Skipped:
    """Mensagem que não abre tentativa; `event` é o log que explica o motivo."""

    event: str


@dataclass(frozen=True)
class FinishedAttempt:
    """Resultado do commit 3: status final do caso, ou `superseded`/`duplicate`."""

    outcome: str
    status_reason: str | None = None
    proposal_id: int | None = None

    @property
    def applied(self) -> bool:
        """`True` se o caso recebeu o status desta tentativa."""
        return self.status_reason is not None


@dataclass(frozen=True)
class MissingInfoProposal:
    """Proposta `missing_info` (RF11, RF20): comentário pronto, sem IA."""

    comment_body: str


def _existing_attempt(
    case_id: int, attempt_id: int, attempt: int, state: str
) -> OpenedAttempt | Skipped:
    """Regra comum de repetição: `DONE` é descartada (RF09, RNF04); `IN_PROGRESS` é retomada."""
    if state == "DONE":
        return Skipped("analysis.skipped_duplicate")
    return OpenedAttempt(case_id=case_id, attempt_id=attempt_id, attempt=attempt, resumed=True)


async def open_initial(
    conn: AsyncConnection, *, event_id: str, repository: str, issue_number: int
) -> OpenedAttempt | Skipped:
    """`initial`: cria o caso e a tentativa 1, ambos idempotentes."""
    await conn.execute(
        "INSERT INTO triage.issue_cases "
        "(repository, issue_number, origin_event_id, status, status_reason, current_attempt) "
        "VALUES (%s, %s, %s, 'ANALYZING', 'initial', 1) "
        "ON CONFLICT (origin_event_id) DO NOTHING",
        (repository, issue_number, event_id),
    )
    cur = await conn.execute(
        "SELECT id FROM triage.issue_cases WHERE origin_event_id = %s", (event_id,)
    )
    case_id = (await cur.fetchone())[0]
    cur = await conn.execute(
        "INSERT INTO triage.analysis_attempts (case_id, event_id, attempt, trigger, state) "
        "VALUES (%s, %s, 1, 'initial', 'IN_PROGRESS') "
        "ON CONFLICT (event_id, attempt) DO NOTHING RETURNING id",
        (case_id, event_id),
    )
    row = await cur.fetchone()
    if row is not None:
        return OpenedAttempt(case_id=case_id, attempt_id=row[0], attempt=1, resumed=False)
    cur = await conn.execute(
        "SELECT id, state FROM triage.analysis_attempts WHERE event_id = %s AND attempt = 1",
        (event_id,),
    )
    attempt_id, state = await cur.fetchone()
    return _existing_attempt(case_id, attempt_id, 1, state)


async def _lock_case(conn: AsyncConnection, event_id: str) -> tuple | None:
    cur = await conn.execute(
        "SELECT id, status, status_reason, current_attempt FROM triage.issue_cases "
        "WHERE origin_event_id = %s FOR UPDATE",
        (event_id,),
    )
    return await cur.fetchone()


async def open_author_edit(
    conn: AsyncConnection, *, event_id: str, trigger_ref: str
) -> OpenedAttempt | Skipped:
    """`author_edit` (RF28): só reabre caso em `READY_TO_PUBLISH (empty_description)`."""
    case = await _lock_case(conn, event_id)
    if case is None:
        return Skipped("analysis.edit_ignored")
    case_id, status, status_reason, current_attempt = case

    cur = await conn.execute(
        "SELECT id, attempt, state FROM triage.analysis_attempts "
        "WHERE event_id = %s AND trigger = 'author_edit' AND trigger_ref = %s",
        (event_id, trigger_ref),
    )
    existing = await cur.fetchone()
    if existing is not None:
        attempt_id, attempt, state = existing
        return _existing_attempt(case_id, attempt_id, attempt, state)

    if (status, status_reason) != ("READY_TO_PUBLISH", "empty_description"):
        return Skipped("analysis.edit_ignored")

    attempt = current_attempt + 1
    cur = await conn.execute(
        "INSERT INTO triage.analysis_attempts "
        "(case_id, event_id, attempt, trigger, trigger_ref, state) "
        "VALUES (%s, %s, %s, 'author_edit', %s, 'IN_PROGRESS') "
        "ON CONFLICT DO NOTHING RETURNING id",
        (case_id, event_id, attempt, trigger_ref),
    )
    row = await cur.fetchone()
    if row is None:
        return Skipped("analysis.edit_ignored")
    await conn.execute(
        "UPDATE triage.issue_cases SET status = 'ANALYZING', status_reason = 'author_edit', "
        "current_attempt = %s, updated_at = now() WHERE id = %s",
        (attempt, case_id),
    )
    return OpenedAttempt(case_id=case_id, attempt_id=row[0], attempt=attempt, resumed=False)


async def open_manual(
    conn: AsyncConnection, *, event_id: str, trigger: str, attempt: int, trigger_ref: str
) -> OpenedAttempt | Skipped | None:
    """`enrich` e `reprocess` (RF29, RF30): abre a tentativa `attempt` da mensagem.

    O estado e o limite já foram validados pelo issue-analyst-api; aqui só se descarta a
    tentativa obsoleta. Devolve `None` se o caso não existir (mensagem impossível de processar).
    """
    case = await _lock_case(conn, event_id)
    if case is None:
        return None
    case_id, _status, _status_reason, current_attempt = case

    cur = await conn.execute(
        "SELECT id, state FROM triage.analysis_attempts WHERE event_id = %s AND attempt = %s",
        (event_id, attempt),
    )
    existing = await cur.fetchone()
    if existing is not None:
        return _existing_attempt(case_id, existing[0], attempt, existing[1])
    if attempt <= current_attempt:
        return Skipped("analysis.skipped_stale")

    cur = await conn.execute(
        "INSERT INTO triage.analysis_attempts "
        "(case_id, event_id, attempt, trigger, trigger_ref, state) "
        "VALUES (%s, %s, %s, %s, %s, 'IN_PROGRESS') RETURNING id",
        (case_id, event_id, attempt, trigger, trigger_ref),
    )
    attempt_id = (await cur.fetchone())[0]
    await conn.execute(
        "UPDATE triage.issue_cases SET status = 'ANALYZING', status_reason = %s, "
        "current_attempt = %s, manual_attempts = manual_attempts + 1, updated_at = now() "
        "WHERE id = %s",
        (trigger, attempt, case_id),
    )
    return OpenedAttempt(case_id=case_id, attempt_id=attempt_id, attempt=attempt, resumed=False)


async def finish_attempt(
    conn: AsyncConnection,
    opened: OpenedAttempt,
    *,
    event_id: str,
    repository: str,
    issue_number: int,
    status: str,
    status_reason: str,
    max_manual_attempts: int,
    missing_info: MissingInfoProposal | None = None,
) -> FinishedAttempt:
    """Commit 3 da D15: proposta, status do caso, tentativa `DONE` e outbox, juntos (RF21).

    O caso é lido com `FOR UPDATE`; se a tentativa não for mais a `current_attempt`, ela termina
    `DONE` com `outcome = superseded`, sem proposta. Regra de fechamento (RF30): resultado
    `NEEDS_REVIEW` com `manual_attempts >= max_manual_attempts` vira
    `MAX_RETRIES_REACHED (manual_attempts_exhausted)`.
    """
    cur = await conn.execute(
        "SELECT current_attempt, manual_attempts FROM triage.issue_cases WHERE id = %s FOR UPDATE",
        (opened.case_id,),
    )
    current_attempt, manual_attempts = await cur.fetchone()
    cur = await conn.execute(
        "SELECT state FROM triage.analysis_attempts WHERE id = %s FOR UPDATE",
        (opened.attempt_id,),
    )
    if (await cur.fetchone())[0] == "DONE":
        return FinishedAttempt(outcome="duplicate")

    if current_attempt != opened.attempt:
        await _mark_done(conn, opened.attempt_id, SUPERSEDED)
        return FinishedAttempt(outcome=SUPERSEDED)

    if status == "NEEDS_REVIEW" and manual_attempts >= max_manual_attempts:
        status, status_reason = "MAX_RETRIES_REACHED", "manual_attempts_exhausted"

    proposal_id = None
    if missing_info is not None:
        cur = await conn.execute(
            "INSERT INTO triage.proposals "
            "(attempt_id, kind, validation_status, comment_body) "
            "VALUES (%s, 'missing_info', 'VALID', %s) RETURNING id",
            (opened.attempt_id, missing_info.comment_body),
        )
        proposal_id = (await cur.fetchone())[0]
        await conn.execute(
            "INSERT INTO triage.publish_outbox (event_id, topic, dedup_key, payload) "
            "VALUES (%s, %s, %s, %s)",
            (
                event_id,
                PUBLISH_TOPIC,
                f"{repository}#{issue_number}:{opened.attempt}",
                Jsonb({"proposalId": proposal_id, "revision": opened.attempt}),
            ),
        )

    await conn.execute(
        "UPDATE triage.issue_cases SET status = %s, status_reason = %s, updated_at = now() "
        "WHERE id = %s",
        (status, status_reason, opened.case_id),
    )
    await _mark_done(conn, opened.attempt_id, status)
    return FinishedAttempt(outcome=status, status_reason=status_reason, proposal_id=proposal_id)


async def _mark_done(conn: AsyncConnection, attempt_id: int, outcome: str) -> None:
    await conn.execute(
        "UPDATE triage.analysis_attempts SET state = 'DONE', outcome = %s, finished_at = now() "
        "WHERE id = %s",
        (outcome, attempt_id),
    )
