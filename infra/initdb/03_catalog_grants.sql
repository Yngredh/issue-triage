-- Privilégios do schema `catalog` (D32). Roda no initdb depois de `catalog/schema.sql` e de
-- `catalog/seed.sql`, quando o schema e as tabelas já existem.
-- O catálogo é lido pelo issue-triage e pelo issue-publisher (ADR-0001); ninguém escreve nele
-- (a seed roda como superusuário). O issue-analyst-api não lê o catálogo.

GRANT USAGE ON SCHEMA catalog TO analyst_api, triage_svc, publisher_svc;
GRANT SELECT ON ALL TABLES IN SCHEMA catalog TO triage_svc, publisher_svc;
