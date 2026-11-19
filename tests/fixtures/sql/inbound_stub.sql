CREATE SCHEMA IF NOT EXISTS inbound;

CREATE TABLE inbound.issue_events (
  event_id             text PRIMARY KEY,
  type                 text NOT NULL CHECK (type IN ('issue.created', 'issue.edited')),
  occurred_at          timestamptz NOT NULL,
  received_at          timestamptz NOT NULL DEFAULT now(),
  repository           text NOT NULL,
  issue_number         int  NOT NULL CHECK (issue_number > 0),
  title                text NOT NULL CHECK (btrim(title) <> ''),
  description          text NOT NULL DEFAULT '',
  raw_payload          jsonb NOT NULL,
  body_hash            text NOT NULL,
  status               text NOT NULL DEFAULT 'ACCEPTED' CHECK (status = 'ACCEPTED')
);
CREATE INDEX ON inbound.issue_events (repository, issue_number, received_at);
CREATE UNIQUE INDEX issue_events_one_created_per_issue
  ON inbound.issue_events (repository, issue_number) WHERE type = 'issue.created';

CREATE TABLE inbound.issue_review_history (
  id                      bigserial PRIMARY KEY,
  origin_event_id         text NOT NULL,
  repository              text NOT NULL,
  issue_number            int  NOT NULL,
  action                  text NOT NULL CHECK (action IN ('enrich', 'reprocess', 'close')),
  from_status             text NOT NULL,
  from_reason             text,
  enrichment_description  text,
  final_status            text CHECK (final_status IN ('CANCELLED', 'FINISHED')),
  comment                 text,
  target_attempt          int,
  actor                   text NOT NULL,
  created_at              timestamptz NOT NULL DEFAULT now(),
  CHECK (
    (action = 'enrich'    AND btrim(enrichment_description) <> ''
                          AND target_attempt IS NOT NULL AND final_status IS NULL)
    OR (action = 'reprocess' AND enrichment_description IS NULL
                          AND target_attempt IS NOT NULL AND final_status IS NULL)
    OR (action = 'close'  AND final_status IS NOT NULL AND btrim(comment) <> ''
                          AND target_attempt IS NULL AND enrichment_description IS NULL)
  ),
  UNIQUE (origin_event_id, target_attempt)
);
CREATE UNIQUE INDEX issue_review_history_one_close
  ON inbound.issue_review_history (origin_event_id) WHERE action = 'close';
