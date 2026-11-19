-- Papéis e schemas do ambiente integrado (D18, D32). Somente para uso local: a senha de cada
-- papel é igual ao nome dele. Roda no initdb, antes de `catalog/schema.sql` e `catalog/seed.sql`.
--
-- Cada schema tem o serviço dono, para que cada migração rode como o próprio papel. Efeito
-- aceito (D32): o dono tem todos os privilégios no próprio schema, e não só SELECT, INSERT e
-- UPDATE; a escrita continua restrita ao próprio schema (RNF07).

CREATE ROLE analyst_api  LOGIN PASSWORD 'analyst_api';
CREATE ROLE triage_svc   LOGIN PASSWORD 'triage_svc';
CREATE ROLE publisher_svc LOGIN PASSWORD 'publisher_svc';

CREATE SCHEMA inbound   AUTHORIZATION analyst_api;
CREATE SCHEMA triage    AUTHORIZATION triage_svc;
CREATE SCHEMA publisher AUTHORIZATION publisher_svc;

-- USAGE nos schemas dos serviços para os três papéis (o de `catalog` vem do arquivo que roda
-- depois da seed).
GRANT USAGE ON SCHEMA inbound, triage, publisher TO analyst_api, triage_svc, publisher_svc;

-- Leituras entre serviços: valem para as tabelas e views que o dono criar depois, nas migrações.
-- issue-triage lê `inbound`; issue-analyst-api lê `triage` e `publisher` (API de consulta).
ALTER DEFAULT PRIVILEGES FOR ROLE analyst_api IN SCHEMA inbound
  GRANT SELECT ON TABLES TO triage_svc;
ALTER DEFAULT PRIVILEGES FOR ROLE triage_svc IN SCHEMA triage
  GRANT SELECT ON TABLES TO analyst_api;
ALTER DEFAULT PRIVILEGES FOR ROLE publisher_svc IN SCHEMA publisher
  GRANT SELECT ON TABLES TO analyst_api;

-- O runner de migração e a `0001` executam CREATE SCHEMA IF NOT EXISTS, que exige CREATE no
-- banco mesmo quando o schema já existe (D32, opção A). Vale para o banco atual, o que serve
-- também ao banco temporário dos testes. Efeito aceito: cada papel pode criar schemas novos
-- nesse banco, e nada além disso.
DO $$
BEGIN
  EXECUTE format(
    'GRANT CREATE ON DATABASE %I TO analyst_api, triage_svc, publisher_svc',
    current_database()
  );
END
$$;
