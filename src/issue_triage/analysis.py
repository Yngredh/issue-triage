"""Processamento de uma mensagem da fila `analysis` ("Contratos > Processamento", passos 1 a 8).

Com a decisão `bug`, `melhoria` ou `duvida`, o especialista da categoria roda fora de transação
(D15), o validador decide e o commit 3 grava proposta, caso, tentativa e outbox juntos. Na
retomada, a decisão gravada do roteador é reutilizada, sem nova chamada (RNF04).
"""

import dataclasses
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import structlog
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool
from pydantic import ValidationError

from issue_triage.adapters import triage_store
from issue_triage.adapters.catalog import find_project, list_guidelines
from issue_triage.adapters.catalog_tools import docs_dir
from issue_triage.adapters.inbound import find_origin_event, read_issue_text
from issue_triage.domain.catalog import Project
from issue_triage.domain.issue import OriginEvent
from issue_triage.domain.messages import AnalysisMessage, Outcome
from issue_triage.domain.prompts import ROUTER_PROMPT_VERSION
from issue_triage.domain.router import RouterFailed, RouterLLM, route
from issue_triage.domain.specialist import SpecialistRunner
from issue_triage.domain.specialist_flow import SpecialistOutcome, run_specialist
from issue_triage.domain.templates import render_missing_info, render_triage

logger = structlog.get_logger()


class AnalysisProcessor:
    """Aplica os passos 1 a 8 do processamento a uma mensagem da fila `analysis`.

    `specialist_models` dá o modelo de cada especialista (`bug`, `melhoria`, `dominio`, D16);
    `catalog_source_dir` é a `CATALOG_SOURCE_DIR`. Erros de banco e do provedor sobem para o
    consumidor, que não confirma a mensagem e reconecta (classificação na Etapa 10).
    """

    def __init__(
        self,
        pool: AsyncConnectionPool,
        *,
        router: RouterLLM,
        specialist: SpecialistRunner,
        specialist_models: Mapping[str, str],
        catalog_source_dir: str | Path,
        max_manual_attempts: int,
    ) -> None:
        self._pool = pool
        self._router = router
        self._specialist = specialist
        self._specialist_models = dict(specialist_models)
        self._catalog_source_dir = Path(catalog_source_dir)
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

                stored = await triage_store.find_route_decision(conn, opened.attempt_id)
                guidelines = await list_guidelines(conn, origin.repository)
                previous = await triage_store.list_previous_proposals(conn, opened)

            # Passo 5: roteador (Spec D). Retomada reutiliza a decisão gravada (RNF04); senão a
            # chamada roda fora de transação (D15) e a decisão é gravada no commit 2.
            if stored is None:
                result = await route(
                    self._router,
                    repository=origin.repository,
                    title=text.title,
                    description=text.description,
                    enrichments=text.enrichments,
                    guidelines=guidelines,
                )
                if isinstance(result, RouterFailed):
                    async with conn.transaction():
                        finished = await triage_store.finish_attempt(
                            conn,
                            opened,
                            event_id=origin.event_id,
                            repository=origin.repository,
                            issue_number=origin.issue_number,
                            status="NEEDS_REVIEW",
                            status_reason="invalid_proposal",
                            max_manual_attempts=self._max_manual_attempts,
                            error=result.rejection.error,
                        )
                    if finished.applied:
                        log.info(
                            "triage.proposal_invalid",
                            validation_error=result.rejection.code,
                            stage="router",
                            status=finished.outcome,
                            status_reason=finished.status_reason,
                        )
                    elif finished.outcome == "duplicate":
                        log.info("analysis.skipped_duplicate")
                    return Outcome.ACK

                async with conn.transaction():
                    stored = await triage_store.save_route_decision(
                        conn,
                        opened,
                        result.decision,
                        llm_calls=result.llm_calls,
                        prompt_version=ROUTER_PROMPT_VERSION,
                        model=self._router.model,
                    )
                log.info(
                    "triage.routed",
                    category=stored.decision.category,
                    llm_calls=stored.llm_calls,
                )

            if stored.decision.category == "indeterminado":
                async with conn.transaction():
                    finished = await triage_store.finish_attempt(
                        conn,
                        opened,
                        event_id=origin.event_id,
                        repository=origin.repository,
                        issue_number=origin.issue_number,
                        status="NEEDS_REVIEW",
                        status_reason="insufficient_content",
                        max_manual_attempts=self._max_manual_attempts,
                    )
                if finished.outcome == "duplicate":
                    log.info("analysis.skipped_duplicate")
                return Outcome.ACK

            # Passos 6 e 7: especialista e validador, fora de transação (D15).
            outcome = await run_specialist(
                self._specialist,
                category=stored.decision.category,
                models=self._specialist_models,
                project=project,
                text=text,
                guidelines=guidelines,
                previous=previous,
                docs_dir=docs_dir(self._catalog_source_dir, origin.repository),
            )

            # Passo 8: commit 3 (D15).
            async with conn.transaction():
                finished = await self._finish_specialist(conn, opened, origin, project, outcome)
            if finished.outcome == "duplicate":
                log.info("analysis.skipped_duplicate")
            elif finished.applied and outcome.rejection is None:
                log.info(
                    "triage.proposal_validated",
                    specialist=outcome.specialist,
                    llm_calls=outcome.llm_calls,
                    status=finished.outcome,
                    status_reason=finished.status_reason,
                )
            elif finished.applied and outcome.rejection is not None:
                log.info(
                    "triage.proposal_invalid",
                    validation_error=outcome.rejection.code,
                    stage="specialist",
                    status=finished.outcome,
                    status_reason=finished.status_reason,
                )
        return Outcome.ACK

    async def _finish_specialist(
        self,
        conn: AsyncConnection,
        opened: triage_store.OpenedAttempt,
        origin: OriginEvent,
        project: Project,
        outcome: SpecialistOutcome,
    ) -> triage_store.FinishedAttempt:
        """Grava a proposta do especialista com o status do caso (D24, D25).

        Aprovada: `comment_body` do template, caso `READY_TO_PUBLISH (proposal_validated)` e
        linha na outbox. Reprovada: proposta `INVALID` com as colunas da última saída que passou
        na checagem 1 (ou nulas), caso `NEEDS_REVIEW (invalid_proposal)` e
        `analysis_attempts.error = 'specialist: <código>: <detalhe>'`.
        """
        fields = outcome.output if outcome.output is not None else outcome.last_checked
        columns: dict[str, Any] = {}
        if fields is not None:
            columns = fields.model_dump(exclude={"evidence"})
            columns["evidence"] = [item.model_dump() for item in fields.evidence]
        proposal = triage_store.TriageProposal(
            specialist=outcome.specialist,
            validation_status="VALID" if outcome.rejection is None else "INVALID",
            validation_error=outcome.rejection.code if outcome.rejection else None,
            prompt_version=outcome.prompt_version,
            model=outcome.model,
            raw_output=outcome.raw_output,
            tool_calls=outcome.tool_calls,
            **columns,
        )
        if outcome.output is not None:
            comment = render_triage(
                outcome.output,
                specialist=outcome.specialist,
                project=project.name,
                responsible=project.responsible,
                event_id=origin.event_id,
                revision=opened.attempt,
            )
            proposal = dataclasses.replace(proposal, comment_body=comment)
            status, status_reason, error = "READY_TO_PUBLISH", "proposal_validated", None
        else:
            assert outcome.rejection is not None
            status, status_reason = "NEEDS_REVIEW", "invalid_proposal"
            error = outcome.rejection.error
        return await triage_store.finish_attempt(
            conn,
            opened,
            event_id=origin.event_id,
            repository=origin.repository,
            issue_number=origin.issue_number,
            status=status,
            status_reason=status_reason,
            max_manual_attempts=self._max_manual_attempts,
            triage=proposal,
            error=error,
        )

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
