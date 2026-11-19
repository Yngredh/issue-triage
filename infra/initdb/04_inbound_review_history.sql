-- `inbound.issue_review_history` no ambiente integrado (D34). A tabela é da Revisão Humana Spec e
-- nenhuma migração dos serviços a cria (o plano do issue-analyst-api a deixa fora de escopo), mas o
-- issue-triage a lê em toda análise (enriquecimentos e `close`). Sem ela, o ambiente integrado
-- falha com `relation "inbound.issue_review_history" does not exist`.
-- Roda no initdb depois de `00_roles.sql` (que cria o schema `inbound`). O DDL é cópia literal do
-- de `tests/fixtures/sql/inbound_stub.sql`, e um teste confere que os dois não divergem.
-- Dono: `analyst_api` (dono do schema `inbound`, que escreverá nela quando o endpoint de revisão
-- existir). O `ALTER DEFAULT PRIVILEGES` do `00_roles.sql` só vale para tabelas criadas pelo
-- próprio `analyst_api`, por isso o `GRANT` é explícito.

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

ALTER TABLE inbound.issue_review_history OWNER TO analyst_api;
GRANT SELECT ON inbound.issue_review_history TO triage_svc;
