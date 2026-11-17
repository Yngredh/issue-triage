"""Processamento de uma mensagem da fila `analysis` ("Contratos > Processamento", passos 1 a 4).

Nesta etapa o fluxo para antes do roteador: uma issue que chegaria a ele fica com a tentativa
`IN_PROGRESS` e a mensagem volta para a fila (`Outcome.REQUEUE`); o roteador entra na Etapa 6.
"""

from typing import Any

import structlog
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool
from pydantic import ValidationError

from issue_triage.adapters import triage_store
from issue_triage.adapters.catalog import find_project
from issue_triage.adapters.inbound import find_origin_event, read_issue_text
from issue_triage.domain.issue import OriginEvent
from issue_triage.domain.messages import AnalysisMessage, Outcome
from issue_triage.domain.templates import render_missing_info

logger = structlog.get_logger()


class AnalysisProcessor:
    """Aplica os passos 1 a 4 do processamento a uma mensagem da fila `analysis`.

    Erros de banco sobem para o consumidor, que não confirma a mensagem e reconecta.
    """

    def __init__(self, pool: AsyncConnectionPool, *, max_manual_attempts: int) -> None:
        self._pool = pool
        self._max_manual_attempts = max_manual_attempts

    async def process(self, body: bytes) -> Outcome:
        """Processa o corpo da mensagem; só devolve depois do commit."""
        try:
            message = AnalysisMessage.model_validate_json(body)
        except ValidationError as exc:
            logger.warning(
                "analysis.discarded_invalid", reason="invalid_schema", errors=exc.error_count()
            )
            return Outcome.ACK

        log = logger.bind(event_id=message.event_id)
        log.info("analysis.received", trigger=message.trigger)

        async with self._pool.connection() as conn:
            # Passos 2 e 3: evento de origem e abertura ou retomada da tentativa (commit 1, D15).
            async with conn.transaction():
                origin = await find_origin_event(conn, message.event_id)
                if origin is None:
                    log.warning("analysis.discarded_invalid", reason="origin_not_found")
                    return Outcome.ACK
                log = log.bind(repository=origin.repository, issue_number=origin.issue_number)
                opened = await self._open(conn, message, origin)

            if opened is None:
                log.warning("analysis.discarded_invalid", reason="case_not_found")
                return Outcome.ACK
            if isinstance(opened, triage_store.Skipped):
                log.info(opened.event, trigger=message.trigger, message_attempt=message.attempt)
                return Outcome.ACK
            log = log.bind(attempt=opened.attempt)
            if opened.resumed:
                log.info("analysis.resumed")

            # Passo 4: checagens sem IA (harness); o resultado é gravado no commit 3 (D15).
            async with conn.transaction():
                project = await find_project(conn, origin.repository)
                if project is None:
                    finished = await triage_store.finish_attempt(
                        conn,
                        opened,
                        event_id=origin.event_id,
                        repository=origin.repository,
                        issue_number=origin.issue_number,
                        status="NEEDS_REVIEW",
                        status_reason="unknown_repo",
                        max_manual_attempts=self._max_manual_attempts,
                    )
                    self._log_finished(log, "triage.unknown_repo", finished)
                    return Outcome.ACK

                text = await read_issue_text(conn, origin)
                if text.description_empty:
                    comment = render_missing_info(
                        project=project.name,
                        responsible=project.responsible,
                        event_id=origin.event_id,
                        revision=opened.attempt,
                    )
                    finished = await triage_store.finish_attempt(
                        conn,
                        opened,
                        event_id=origin.event_id,
                        repository=origin.repository,
                        issue_number=origin.issue_number,
                        status="READY_TO_PUBLISH",
                        status_reason="empty_description",
                        max_manual_attempts=self._max_manual_attempts,
                        missing_info=triage_store.MissingInfoProposal(comment),
                    )
                    self._log_finished(log, "triage.empty_description", finished)
                    return Outcome.ACK

        # O roteador (Etapa 6) ainda não existe: a tentativa fica IN_PROGRESS e a mensagem volta.
        return Outcome.REQUEUE

    async def _open(
        self, conn: AsyncConnection, message: AnalysisMessage, origin: OriginEvent
    ) -> triage_store.OpenedAttempt | triage_store.Skipped | None:
        if message.trigger == "initial":
            return await triage_store.open_initial(
                conn,
                event_id=origin.event_id,
                repository=origin.repository,
                issue_number=origin.issue_number,
            )
        if message.trigger == "author_edit":
            return await triage_store.open_author_edit(
                conn, event_id=origin.event_id, trigger_ref=message.trigger_ref
            )
        return await triage_store.open_manual(
            conn,
            event_id=origin.event_id,
            trigger=message.trigger,
            attempt=message.attempt,
            trigger_ref=message.trigger_ref,
        )

    @staticmethod
    def _log_finished(log: Any, event: str, finished: triage_store.FinishedAttempt) -> None:
        if finished.outcome == "duplicate":
            log.info("analysis.skipped_duplicate")
        elif finished.applied:
            log.info(event, status=finished.outcome, status_reason=finished.status_reason)
