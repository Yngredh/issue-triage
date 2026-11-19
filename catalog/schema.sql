-- Schema `catalog` (ADR-0001, D8). Cópia da Modelagem de dados; executado pelo initdb antes da seed.

CREATE SCHEMA IF NOT EXISTS catalog;

CREATE TABLE IF NOT EXISTS catalog.projects (
  repository   text PRIMARY KEY CHECK (repository ~ '^[^/\s]+/[^/\s]+$'),
  name         text NOT NULL,
  responsible  text NOT NULL,              -- citado no comentário (RF19)
  description  text NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS catalog.triage_guidelines (
  id          bigserial PRIMARY KEY,
  repository  text NOT NULL REFERENCES catalog.projects (repository),
  category    text CHECK (category IN ('bug', 'melhoria', 'duvida')),  -- NULL = vale para todas
  guideline   text NOT NULL
);
