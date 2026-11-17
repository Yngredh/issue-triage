-- Concede `SELECT` da view `triage.publishable_proposals` ao `publisher_svc` (D32).
-- Só roda se o papel existir: fora do ambiente integrado (testes, desenvolvimento) ele não existe.
-- Nenhuma tabela de `triage` é concedida ao publisher; a view é o contrato (Modelagem de dados).

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'publisher_svc') THEN
    GRANT SELECT ON triage.publishable_proposals TO publisher_svc;
  END IF;
END
$$;
