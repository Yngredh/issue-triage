"""Modelo Pydantic do arquivo `tests/routing/fixtures/issues.yaml` (Etapa 13, P3)."""

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

ISSUES_FILE = Path(__file__).parent / "fixtures" / "issues.yaml"
CATEGORIES = {"bug", "melhoria", "duvida", "indeterminado"}
SPECIALISTS = {"bug", "melhoria", "dominio"}


class IssueCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    group: Literal["router", "review", "injection", "specialist"]
    active: bool
    stage: Literal["router", "harness", "specialist"]
    repository: str | None = None
    title: str | None = None
    description: str | None = None
    expected: str
    evidence: list[str] = Field(default_factory=list)
    specialist: str | None = None
    case_ref: str | None = None
    fail_if: str | None = None
    note: str | None = None

    @model_validator(mode="after")
    def _check_stage(self) -> "IssueCase":
        if self.stage == "specialist":
            if self.specialist not in SPECIALISTS:
                raise ValueError(f"{self.id}: especialista inválido: {self.specialist!r}")
            if not self.case_ref:
                raise ValueError(f"{self.id}: caso de especialista sem case_ref")
            return self
        if self.case_ref or self.specialist:
            raise ValueError(f"{self.id}: case_ref e specialist só valem em caso de especialista")
        if self.repository is None or self.title is None or self.description is None:
            raise ValueError(f"{self.id}: faltam repository, title ou description")
        if self.stage == "router" and self.expected not in CATEGORIES:
            raise ValueError(f"{self.id}: categoria inválida: {self.expected!r}")
        return self


class IssueCases(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int
    responsible: str
    cases: list[IssueCase]


def load_cases(path: Path = ISSUES_FILE) -> IssueCases:
    with path.open(encoding="utf-8") as handle:
        return IssueCases.model_validate(yaml.safe_load(handle))
