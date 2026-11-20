"""Processamento de uma mensagem da fila `analysis` ("Contratos > Processamento", passos 1 a 8).

Com a decisão `bug`, `melhoria` ou `duvida`, o especialista da categoria roda fora de transação
(D15), o validador decide e o commit 3 grava proposta, caso, tentativa e outbox juntos. Na
retomada, a decisão gravada do roteador é reutilizada, sem nova chamada (RNF04).
"""

import asyncio
import dataclasses
import time
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any, TypeVar

import structlog
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool
from pydantic import ValidationError

from issue_triage.adapters import triage_store
from issue_triage.adapters.catalog import find_project, list_guidelines
from issue_triage.adapters.catalog_tools import docs_dir
from issue_triage.adapters.inbound import find_close_review, find_origin_event, read_issue_text
from issue_triage.adapters.provider_errors import TechnicalFailure, classify
from issue_triage.domain.catalog import Project
from issue_triage.domain.issue import OriginEvent
from issue_triage.domain.messages import AnalysisMessage, Outcome
from issue_triage.domain.prompts import ROUTER_PROMPT_VERSION
from issue_triage.domain.router import RouterFailed, RouterLLM, route
from issue_triage.domain.specialist import SpecialistRunner
from issue_triage.domain.specialist_flow import SpecialistOutcome, run_specialist
from issue_triage.domain.templates import render_missing_info, render_triage

logger = structlog.get_logger()

# Chaves ligadas ao contexto do structlog durante uma mensagem ("Contratos > Logs").
_CONTEXT_KEYS = ("event_id", "repository", "issue_number", "attempt")

T = TypeVar("T")


def _elapsed_ms(started: float) -> float:
    return round((time.monotonic() - started) * 1000, 3)


class _TechnicalFailed(Exception):
    """Falha técnica sem nova tentativa: permanente ou tentativas esgotadas (D28)."""

    def __init__(
        self,
        error: str,
        stage: str,
        cause: str | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(error)
        self.error = error
        self.stage = stage
        self.cause = cause
        self.request_id = request_id


class AnalysisProcessor:
    """Aplica os passos 1 a 8 do processamento a uma mensagem da fila `analysis`.

    `specialist_models` dá o modelo de cada especialista (`bug`, `melhoria`, `dominio`, D16);
    `catalog_source_dir` é a `CATALOG_SOURCE_DIR`. Erros de banco sobem para o consumidor, que
    não confirma a mensagem e reconecta. Falhas da chamada ao roteador ou ao especialista são
    classificadas (D28): as transitórias esperam `retry_delays_s` (`ANALYSIS_RETRY_DELAYS_S`) com
    `asyncio.sleep`, sem ack, e repetem só a etapa que falhou; os atrasos são consumidos ao longo
    da mensagem (3 tentativas ao todo com `10,30`). Esgotadas ou permanentes, a tentativa termina
    `NEEDS_REVIEW (technical_failure)`.
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
        retry_delays_s: Sequence[float] = (10, 30),
    ) -> None:
        self._pool = pool
        self._router = router
        self._specialist = specialist
        self._specialist_models = dict(specialist_models)
        self._catalog_source_dir = Path(catalog_source_dir)
        self._max_manual_attempts = max_manual_attempts
        self._retry_delays_s = tuple(retry_delays_s)

    async def process(self, body: bytes) -> Outcome:
        """Processa o corpo da mensagem; só devolve depois do commit."""
        retries = iter(self._retry_delays_s)
        try:
            message = AnalysisMessage.model_validate_json(body)
        except ValidationError as exc:
            logger.warning(
                "analysis.discarded_invalid", reason="invalid_schema", errors=exc.error_count()
            )
            return Outcome.ACK

        structlog.contextvars.bind_contextvars(event_id=message.event_id)
        try:
            return await self._process(message, retries)
        finally:
            structlog.contextvars.unbind_contextvars(*_CONTEXT_KEYS)

    async def _process(self, message: AnalysisMessage, retries: Iterator[float]) -> Outcome:
        """Passos 2 a 8 de "Processamento"; o contexto de log já leva o `event_id`."""
        logger.info("analysis.received", trigger=message.trigger)

        async with self._pool.connection() as conn:
            # Passos 2 e 3: evento de origem e abertura ou retomada da tentativa (commit 1, D15).
            async with conn.transaction():
                origin = await find_origin_event(conn, message.event_id)
                if origin is None:
                    logger.warning("analysis.discarded_invalid", reason="origin_not_found")
                    return Outcome.ACK
                structlog.contextvars.bind_contextvars(
                    repository=origin.repository, issue_number=origin.issue_number
                )
                if message.trigger == "close":
                    return await self._close(conn, message, origin)
                opened = await self._open(conn, message, origin)

            if opened is None:
                logger.warning("analysis.discarded_invalid", reason="case_not_found")
                return Outcome.ACK
            if isinstance(opened, triage_store.Skipped):
                logger.info(opened.event, trigger=message.trigger, message_attempt=message.attempt)
                return Outcome.ACK
            structlog.contextvars.bind_contextvars(attempt=opened.attempt)
            if opened.resumed:
                logger.info("analysis.resumed")

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
                    self._log_finished("triage.unknown_repo", finished)
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
                    self._log_finished("triage.empty_description", finished)
                    return Outcome.ACK

                stored = await triage_store.find_route_decision(conn, opened.attempt_id)
                guidelines = await list_guidelines(conn, origin.repository)
                previous = await triage_store.list_previous_proposals(conn, opened)

            # Passo 5: roteador (Spec D). Retomada reutiliza a decisão gravada (RNF04); senão a
            # chamada roda fora de transação (D15) e a decisão é gravada no commit 2.
            if stored is None:
                try:
                    result = await self._call_provider(
                        "router",
                        retries,
                        lambda: route(
                            self._router,
                            repository=origin.repository,
                            title=text.title,
                            description=text.description,
                            enrichments=text.enrichments,
                            guidelines=guidelines,
                        ),
                    )
                except _TechnicalFailed as failed:
                    return await self._technical_failure(conn, opened, origin, failed)
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
                        logger.info(
                            "triage.proposal_invalid",
                            validation_error=result.rejection.code,
                            stage="router",
                            status=finished.outcome,
                            status_reason=finished.status_reason,
                        )
                    elif finished.outcome == "duplicate":
                        logger.info("analysis.skipped_duplicate")
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
                logger.info(
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
                    logger.info("analysis.skipped_duplicate")
                return Outcome.ACK

            # Passos 6 e 7: especialista e validador, fora de transação (D15).
            try:
                outcome = await self._call_provider(
                    "specialist",
                    retries,
                    lambda: run_specialist(
                        self._specialist,
                        category=stored.decision.category,
                        models=self._specialist_models,
                        project=project,
                        text=text,
                        guidelines=guidelines,
                        previous=previous,
                        docs_dir=docs_dir(self._catalog_source_dir, origin.repository),
                    ),
                )
            except _TechnicalFailed as failed:
                return await self._technical_failure(conn, opened, origin, failed)

            # Passo 8: commit 3 (D15).
            async with conn.transaction():
                finished = await self._finish_specialist(conn, opened, origin, project, outcome)
            if finished.outcome == "duplicate":
                logger.info("analysis.skipped_duplicate")
            elif finished.applied and outcome.rejection is None:
                logger.info(
                    "triage.proposal_validated",
                    specialist=outcome.specialist,
                    llm_calls=outcome.llm_calls,
                    status=finished.outcome,
                    status_reason=finished.status_reason,
                )
            elif finished.applied and outcome.rejection is not None:
                logger.info(
                    "triage.proposal_invalid",
                    validation_error=outcome.rejection.code,
                    stage="specialist",
                    status=finished.outcome,
                    status_reason=finished.status_reason,
                )
        return Outcome.ACK

    async def _call_provider(
        self,
        stage: str,
        retries: Iterator[float],
        call: Callable[[], Awaitable[T]],
    ) -> T:
        """Roda a chamada ao roteador ou ao especialista com a nova tentativa da D28.

        Transitória com atraso disponível: log `triage.retry_scheduled`, `asyncio.sleep` sem ack
        e repete só esta etapa. Permanente, inesperada ou sem atraso: `_TechnicalFailed`.
        """
        while True:
            started = time.monotonic()
            try:
                result = await call()
            except Exception as exc:
                failure: TechnicalFailure = classify(exc)
                logger.warning(
                    "triage.provider_call",
                    stage=stage,
                    outcome="error",
                    duration_ms=_elapsed_ms(started),
                    error=failure.error,
                    transient=failure.transient,
                    cause=failure.cause,
                    request_id=failure.request_id,
                )
            else:
                logger.info(
                    "triage.provider_call",
                    stage=stage,
                    outcome="ok",
                    duration_ms=_elapsed_ms(started),
                )
                return result
            delay = next(retries, None) if failure.transient else None
            if delay is None:
                raise _TechnicalFailed(failure.error, stage, failure.cause, failure.request_id)
            logger.warning(
                "triage.retry_scheduled",
                stage=stage,
                error=failure.error,
                delay_s=delay,
                cause=failure.cause,
                request_id=failure.request_id,
            )
            await asyncio.sleep(delay)

    async def _technical_failure(
        self,
        conn: AsyncConnection,
        opened: triage_store.OpenedAttempt,
        origin: OriginEvent,
        failed: _TechnicalFailed,
    ) -> Outcome:
        """`NEEDS_REVIEW (technical_failure)` com o erro gravado e ack (D28)."""
        async with conn.transaction():
            finished = await triage_store.finish_attempt(
                conn,
                opened,
                event_id=origin.event_id,
                repository=origin.repository,
                issue_number=origin.issue_number,
                status="NEEDS_REVIEW",
                status_reason="technical_failure",
                max_manual_attempts=self._max_manual_attempts,
                error=failed.error,
            )
        if finished.outcome == "duplicate":
            logger.info("analysis.skipped_duplicate")
        elif finished.applied:
            logger.error(
                "triage.technical_failure",
                stage=failed.stage,
                error=failed.error,
                cause=failed.cause,
                request_id=failed.request_id,
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
    def _log_finished(event: str, finished: triage_store.FinishedAttempt) -> None:
        if finished.outcome == "duplicate":
            logger.info("analysis.skipped_duplicate")
        elif finished.applied:
            logger.info(event, status=finished.outcome, status_reason=finished.status_reason)

    @staticmethod
    async def _close(
        conn: AsyncConnection, message: AnalysisMessage, origin: OriginEvent
    ) -> Outcome:
        """`close` (RF29, P1): passo 3 de "Processamento"; não abre tentativa nem chama a IA.

        Roda dentro da transação do commit 1. A linha de review precisa existir, ser um `close`
        e ser do mesmo evento (`review_not_found`); caso inexistente é `case_not_found`.
        """
        assert message.trigger_ref is not None
        review = await find_close_review(conn, message.trigger_ref, origin.event_id)
        if review is None:
            logger.warning("analysis.discarded_invalid", reason="review_not_found")
            return Outcome.ACK
        closed = await triage_store.close_case(conn, event_id=origin.event_id, review=review)
        if closed is None:
            logger.warning("analysis.discarded_invalid", reason="case_not_found")
        elif closed.event == "triage.case_closed":
            logger.info(
                closed.event,
                status=closed.status,
                status_reason=closed.status_reason,
                from_status=closed.from_status,
                review_id=review.review_id,
            )
        else:
            logger.info(closed.event, trigger="close", review_id=review.review_id)
        return Outcome.ACK
