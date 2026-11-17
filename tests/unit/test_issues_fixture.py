"""Confere `tests/routing/fixtures/issues.yaml` (Etapa 13, P3). Não chama a Anthropic."""

import re
from pathlib import Path

from tests.routing.cases import load_cases

SEED = Path(__file__).parents[2] / "catalog" / "seed.sql"
OUT_OF_CATALOG = "Yaane-h/mechanical-hub-auth"


def _seed_repositories() -> set[str]:
    text = SEED.read_text(encoding="utf-8")
    block = text.split("INSERT INTO catalog.projects", 1)[1].split("ON CONFLICT", 1)[0]
    return set(re.findall(r"\('([^']+/[^']+)',", block))


def test_file_loads_with_expected_counts() -> None:
    data = load_cases()
    assert data.responsible == "@Yaane-h"
    assert len(data.cases) == 27
    assert sum(case.active for case in data.cases) == 26


def test_ids_are_unique() -> None:
    ids = [case.id for case in load_cases().cases]
    assert len(ids) == len(set(ids))


def test_evidence_is_literal_in_title_or_description() -> None:
    for case in load_cases().cases:
        text = f"{case.title or ''}\n{case.description or ''}"
        for snippet in case.evidence:
            assert snippet in text, f"{case.id}: evidência não literal: {snippet!r}"


def test_case_ref_points_to_an_existing_router_case() -> None:
    cases = {case.id: case for case in load_cases().cases}
    refs = [case for case in cases.values() if case.stage == "specialist"]
    assert refs
    for case in refs:
        assert case.case_ref in cases, f"{case.id}: case_ref inexistente"
        assert cases[case.case_ref].stage == "router"


def test_repositories_match_the_seed() -> None:
    seed = _seed_repositories()
    assert len(seed) == 4
    assert OUT_OF_CATALOG not in seed
    for case in load_cases().cases:
        if case.repository is None:
            continue
        if case.id == "H04":
            assert case.repository == OUT_OF_CATALOG
        else:
            assert case.repository in seed, f"{case.id}: repositório fora da seed"
