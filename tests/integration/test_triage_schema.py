"""Schema `triage`, runner de migrações, adaptador de conexão e dados de teste (Etapa 2)."""

from collections.abc import Callable

import psycopg
import pytest
from psycopg import errors

from issue_triage.adapters.database import close_pool, create_pool, open_pool
from issue_triage.migrate import apply_migrations, discover_migrations
from tests.conftest import TestData

PUBLISHER_VIEW_COLUMNS = [
    "proposal_id",
    "repository",
    "issue_number",
    "origin_event_id",
    "revision",
    "current_attempt",
    "case_status",
    "kind",
    "validation_status",
    "comment_body",
]


def assert_rejected(
    conn: psycopg.Connection, error: type[Exception], action: Callable[[], object]
) -> None:
    """Executa `action` num savepoint e confirma que o banco recusa com `error`."""
    with pytest.raises(error):
        with conn.transaction():
            action()


def insert_case(
    conn: psycopg.Connection,
    *,
    origin_event_id: str = "evt-1",
    repository: str = "Yaane-h/mechanical-hub-orders",
    issue_number: int = 1,
    status: str = "ANALYZING",
    status_reason: str = "initial",
    current_attempt: int = 1,
) -> int:
    return conn.execute(
        "INSERT INTO triage.issue_cases "
        "(repository, issue_number, origin_event_id, status, status_reason, current_attempt) "
        "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
        (repository, issue_number, origin_event_id, status, status_reason, current_attempt),
    ).fetchone()[0]


def insert_attempt(
    conn: psycopg.Connection,
    case_id: int,
    *,
    event_id: str = "evt-1",
    attempt: int = 1,
    trigger: str = "initial",
    trigger_ref: str | None = None,
    state: str = "IN_PROGRESS",
) -> int:
    return conn.execute(
        "INSERT INTO triage.analysis_attempts "
        "(case_id, event_id, attempt, trigger, trigger_ref, state) "
        "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
        (case_id, event_id, attempt, trigger, trigger_ref, state),
    ).fetchone()[0]


def insert_route_decision(
    conn: psycopg.Connection, attempt_id: int, *, category: str, evidence: str
) -> None:
    conn.execute(
        "INSERT INTO triage.route_decisions "
        "(attempt_id, category, reason, evidence, llm_calls, prompt_version, model) "
        "VALUES (%s, %s, 'motivo', %s, 1, 'router-v1', 'claude-haiku-5-5')",
        (attempt_id, category, evidence),
    )


def insert_proposal(
    conn: psycopg.Connection,
    attempt_id: int,
    *,
    kind: str = "triage",
    specialist: str | None = "bug",
    validation_status: str = "VALID",
    validation_error: str | None = None,
    comment_body: str | None = "Comentário <!-- triage:evt-1:1 -->",
) -> int:
    return conn.execute(
        "INSERT INTO triage.proposals "
        "(attempt_id, kind, specialist, validation_status, validation_error, comment_body) "
        "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
        (attempt_id, kind, specialist, validation_status, validation_error, comment_body),
    ).fetchone()[0]


def insert_outbox(conn: psycopg.Connection, dedup_key: str) -> None:
    conn.execute(
        "INSERT INTO triage.publish_outbox (event_id, topic, dedup_key, payload) "
        "VALUES ('evt-1', 'publish', %s, '{\"proposalId\": 1, \"revision\": 1}')",
        (dedup_key,),
    )


def test_runner_is_idempotent(database_url: str) -> None:
    assert apply_migrations(database_url) == []
    assert apply_migrations(database_url) == []
    with psycopg.connect(database_url) as conn:
        versions = [
            row[0]
            for row in conn.execute("SELECT version FROM triage.schema_migrations ORDER BY 1")
        ]
    assert versions == [version for version, _ in discover_migrations()]


def test_attempt_is_unique_per_event_and_number(db_conn: psycopg.Connection) -> None:
    case_id = insert_case(db_conn)
    insert_attempt(db_conn, case_id, attempt=1)
    assert_rejected(
        db_conn, errors.UniqueViolation, lambda: insert_attempt(db_conn, case_id, attempt=1)
    )
    insert_attempt(db_conn, case_id, attempt=2, trigger="enrich", trigger_ref="10")


def test_one_attempt_per_author_edit(db_conn: psycopg.Connection) -> None:
    case_id = insert_case(db_conn)
    insert_attempt(db_conn, case_id, attempt=2, trigger="author_edit", trigger_ref="edit-1")
    assert_rejected(
        db_conn,
        errors.UniqueViolation,
        lambda: insert_attempt(
            db_conn, case_id, attempt=3, trigger="author_edit", trigger_ref="edit-1"
        ),
    )
    insert_attempt(db_conn, case_id, attempt=3, trigger="author_edit", trigger_ref="edit-2")
    # O índice é parcial: o mesmo trigger_ref em outro trigger não conflita.
    insert_attempt(db_conn, case_id, attempt=4, trigger="enrich", trigger_ref="edit-1")


@pytest.mark.parametrize(
    ("status", "status_reason"),
    [
        ("ANALYZING", "initial"),
        ("ANALYZING", "author_edit"),
        ("ANALYZING", "enrich"),
        ("ANALYZING", "reprocess"),
        ("READY_TO_PUBLISH", "empty_description"),
        ("READY_TO_PUBLISH", "proposal_validated"),
        ("NEEDS_REVIEW", "unknown_repo"),
        ("NEEDS_REVIEW", "insufficient_content"),
        ("NEEDS_REVIEW", "invalid_proposal"),
        ("NEEDS_REVIEW", "technical_failure"),
        ("MAX_RETRIES_REACHED", "manual_attempts_exhausted"),
    ],
)
def test_case_accepts_valid_status_and_reason(
    db_conn: psycopg.Connection, status: str, status_reason: str
) -> None:
    insert_case(db_conn, status=status, status_reason=status_reason)


@pytest.mark.parametrize(
    ("status", "status_reason"),
    [
        ("ANALYZING", "unknown_repo"),
        ("READY_TO_PUBLISH", "initial"),
        ("NEEDS_REVIEW", "proposal_validated"),
        ("MAX_RETRIES_REACHED", "invalid_proposal"),
        ("CANCELLED", "invalid_proposal"),
        ("UNKNOWN", "initial"),
    ],
)
def test_case_rejects_invalid_status_and_reason(
    db_conn: psycopg.Connection, status: str, status_reason: str
) -> None:
    assert_rejected(
        db_conn,
        errors.CheckViolation,
        lambda: insert_case(db_conn, status=status, status_reason=status_reason),
    )


def test_closed_case_requires_review_id(db_conn: psycopg.Connection) -> None:
    assert_rejected(
        db_conn,
        errors.CheckViolation,
        lambda: insert_case(db_conn, status="CANCELLED", status_reason="closed_by_reviewer"),
    )
    db_conn.execute(
        "INSERT INTO triage.issue_cases (repository, issue_number, origin_event_id, status, "
        "status_reason, closed_by_review_id) "
        "VALUES ('Yaane-h/mechanical-hub-orders', 1, 'evt-1', 'FINISHED', "
        "'closed_by_reviewer', 7)"
    )


def test_route_decision_requires_evidence_outside_indeterminado(
    db_conn: psycopg.Connection,
) -> None:
    case_id = insert_case(db_conn)
    first = insert_attempt(db_conn, case_id, attempt=1)
    second = insert_attempt(db_conn, case_id, attempt=2, trigger="reprocess", trigger_ref="1")
    assert_rejected(
        db_conn,
        errors.CheckViolation,
        lambda: insert_route_decision(db_conn, first, category="bug", evidence=""),
    )
    insert_route_decision(db_conn, first, category="indeterminado", evidence="")
    insert_route_decision(db_conn, second, category="bug", evidence="devolve 500")


def test_invalid_proposal_requires_validation_error(db_conn: psycopg.Connection) -> None:
    case_id = insert_case(db_conn)
    attempt_id = insert_attempt(db_conn, case_id)
    assert_rejected(
        db_conn,
        errors.CheckViolation,
        lambda: insert_proposal(
            db_conn, attempt_id, validation_status="INVALID", comment_body=None
        ),
    )
    insert_proposal(
        db_conn,
        attempt_id,
        validation_status="INVALID",
        validation_error="ungrounded_claim",
        comment_body=None,
    )


def test_valid_proposal_requires_comment_body(db_conn: psycopg.Connection) -> None:
    case_id = insert_case(db_conn)
    attempt_id = insert_attempt(db_conn, case_id)
    assert_rejected(
        db_conn,
        errors.CheckViolation,
        lambda: insert_proposal(db_conn, attempt_id, comment_body=None),
    )


def test_outbox_dedup_key_is_unique_per_topic(db_conn: psycopg.Connection) -> None:
    insert_outbox(db_conn, "Yaane-h/mechanical-hub-orders#1:1")
    assert_rejected(
        db_conn,
        errors.UniqueViolation,
        lambda: insert_outbox(db_conn, "Yaane-h/mechanical-hub-orders#1:1"),
    )
    insert_outbox(db_conn, "Yaane-h/mechanical-hub-orders#1:2")


def test_publishable_view_returns_publisher_columns(db_conn: psycopg.Connection) -> None:
    case_id = insert_case(
        db_conn,
        origin_event_id="evt-view",
        issue_number=42,
        status="READY_TO_PUBLISH",
        status_reason="proposal_validated",
        current_attempt=2,
    )
    attempt_id = insert_attempt(
        db_conn, case_id, event_id="evt-view", attempt=2, trigger="enrich", trigger_ref="3"
    )
    body = "Comentário <!-- triage:evt-view:2 -->"
    proposal_id = insert_proposal(db_conn, attempt_id, comment_body=body)

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM triage.publishable_proposals WHERE proposal_id = %s", (proposal_id,)
        )
        columns = [column.name for column in cur.description]
        row = cur.fetchone()

    assert columns == PUBLISHER_VIEW_COLUMNS
    assert dict(zip(columns, row, strict=True)) == {
        "proposal_id": proposal_id,
        "repository": "Yaane-h/mechanical-hub-orders",
        "issue_number": 42,
        "origin_event_id": "evt-view",
        "revision": 2,
        "current_attempt": 2,
        "case_status": "READY_TO_PUBLISH",
        "kind": "triage",
        "validation_status": "VALID",
        "comment_body": body,
    }


def test_factory_creates_inbound_and_catalog_rows(
    data: TestData, db_conn: psycopg.Connection
) -> None:
    origin = data.origin_event(description="")
    edit = data.edit_event(origin, description="Agora com descrição.")
    enrich = data.review(origin, action="enrich", target_attempt=2)
    close = data.review(origin, action="close")
    project = data.project()

    events = db_conn.execute(
        "SELECT event_id, type FROM inbound.issue_events "
        "WHERE repository = %s AND issue_number = %s ORDER BY received_at, type",
        (origin.repository, origin.issue_number),
    ).fetchall()
    assert set(events) == {(origin.event_id, "issue.created"), (edit.event_id, "issue.edited")}

    reviews = db_conn.execute(
        "SELECT id, action, target_attempt, enrichment_description "
        "FROM inbound.issue_review_history WHERE origin_event_id = %s ORDER BY id",
        (origin.event_id,),
    ).fetchall()
    assert reviews == [
        (enrich.id, "enrich", 2, enrich.enrichment_description),
        (close.id, "close", None, None),
    ]

    guidelines = db_conn.execute(
        "SELECT category, guideline FROM catalog.triage_guidelines "
        "WHERE repository = %s ORDER BY id",
        (project.repository,),
    ).fetchall()
    assert tuple(guidelines) == project.guidelines

    # Recriar o projeto substitui as orientações, sem duplicar.
    data.project(guidelines=((None, "Só uma orientação."),))
    count = db_conn.execute(
        "SELECT count(*) FROM catalog.triage_guidelines WHERE repository = %s",
        (project.repository,),
    ).fetchone()[0]
    assert count == 1


async def test_pool_connects_to_database(database_url: str) -> None:
    pool = create_pool(database_url)
    await open_pool(pool)
    try:
        async with pool.connection() as conn:
            cur = await conn.execute("SELECT count(*) FROM triage.schema_migrations")
            assert (await cur.fetchone())[0] >= 1
    finally:
        await close_pool(pool)
