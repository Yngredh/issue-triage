"""Dados do catálogo de projetos lidos pelo harness (ADR-0001)."""

from dataclasses import dataclass


@dataclass(frozen=True)
class TriageGuideline:
    """Orientação de triagem de um repositório; `category` nula vale para todas."""

    category: str | None
    guideline: str


@dataclass(frozen=True)
class Project:
    """Projeto de `catalog.projects`, citado no comentário (RF19) e no prompt do especialista."""

    repository: str
    name: str
    responsible: str
    description: str
