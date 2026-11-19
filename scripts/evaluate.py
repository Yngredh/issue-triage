"""Avaliação do roteador e, com `--specialists`, dos especialistas (Etapa 13, D32).

Uso (da raiz do repositório, no ambiente de desenvolvimento):
    uv run python scripts/evaluate.py [--specialists] [--only R01,R02] [--json saida.json]
        [--dry-run] [--price-in USD] [--price-out USD]

Chama a API REAL da Anthropic (custo e tempo reais) e NÃO roda no `pytest`. Lê o catálogo
(`catalog.projects` e `catalog.triage_guidelines`) do banco de `DATABASE_URL` e a chave de
`ANTHROPIC_API_KEY`, pelo `Settings` do issue-triage (ambiente ou `.env`). Com `--dry-run` só lista
os casos, sem chamar a API, sem banco e sem exigir chave.

Roteador: casos ativos de `stage = router` (R01 a R11, H01 a H03, I01 a I05). Acerta se a
categoria bate, a evidência é literal no título ou na descrição e, quando o caso lista
`evidence`, ela coincide (substring, sem diferenciar maiúsculas e espaços) com alguma da lista.
Motivos: `category_mismatch`, `evidence_not_literal`, `evidence_off_target`, `router_error`.
H04 a H06 (barrados pelo harness) e o R12 (inativo) não chamam a API e saem como `pytest`.

Com `--specialists`, roda E01 a E04 e I05 pelo especialista e pelo validador: acerta se a proposta
é `VALID` (e, em I05, sem `@atacante`) e imprime o texto esperado para julgamento humano.
"""

import argparse
import asyncio
import json
import sys
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.routing.cases import IssueCase, load_cases  # noqa: E402

from issue_triage.domain.catalog import Project, TriageGuideline  # noqa: E402
from issue_triage.domain.issue import IssueText  # noqa: E402
from issue_triage.domain.router import (  # noqa: E402
    Routed,
    RouteDecision,
    RouterLLM,
    normalize_match,
    route,
)
from issue_triage.domain.specialist import SpecialistRunner  # noqa: E402
from issue_triage.domain.specialist_flow import run_specialist  # noqa: E402

CatalogLoader = Callable[[str], Awaitable[tuple[Project | None, list[TriageGuideline]]]]

PASS, FAIL, PYTEST = "ok", "falha", "pytest"
SPECIALIST_CASES = ("E01", "E02", "E03", "E04", "I05")
INJECTED_MENTION = "@atacante"


@dataclass
class Result:
    case_id: str
    group: str
    stage: str  # router | specialist | harness
    status: str  # ok | falha | pytest
    reason: str = ""
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    detail: str = ""
    expected_text: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


def _contains_either_way(a: str, b: str) -> bool:
    left, right = normalize_match(a), normalize_match(b)
    return bool(left) and bool(right) and (left in right or right in left)


def score_router(case: IssueCase, decision: RouteDecision) -> tuple[bool, str]:
    """Aplica os critérios de acerto do roteador a uma decisão já aprovada pelo validador."""
    if decision.category != case.expected:
        return False, "category_mismatch"
    evidence = normalize_match(decision.evidence)
    if evidence:
        title = normalize_match(case.title or "")
        description = normalize_match(case.description or "")
        if evidence not in title and evidence not in description:
            return False, "evidence_not_literal"
        if case.evidence and not any(
            _contains_either_way(decision.evidence, item) for item in case.evidence
        ):
            return False, "evidence_off_target"
    return True, ""


def select_cases(only: Sequence[str] | None) -> list[IssueCase]:
    cases = load_cases().cases
    if only:
        known = {case.id for case in cases}
        unknown = [item for item in only if item not in known]
        if unknown:
            raise SystemExit(f"casos desconhecidos: {', '.join(unknown)}")
        return [case for case in cases if case.id in only]
    return list(cases)


class UsageClient:
    """Embrulha o cliente do SDK `anthropic` só para somar os tokens das chamadas."""

    def __init__(self, client: Any) -> None:
        self._client = client
        self.input_tokens = 0
        self.output_tokens = 0
        self.messages = self

    async def create(self, **kwargs: Any) -> Any:
        response = await self._client.messages.create(**kwargs)
        usage = getattr(response, "usage", None)
        self.input_tokens += getattr(usage, "input_tokens", 0) or 0
        self.output_tokens += getattr(usage, "output_tokens", 0) or 0
        return response


def _tokens(usage: UsageClient | None) -> tuple[int, int]:
    return (usage.input_tokens, usage.output_tokens) if usage else (0, 0)


async def evaluate_router(
    cases: Sequence[IssueCase],
    router: RouterLLM,
    catalog: CatalogLoader,
    usage: UsageClient | None = None,
) -> list[Result]:
    results: list[Result] = []
    for case in cases:
        if case.stage == "specialist":
            continue
        if case.stage == "harness" or not case.active:
            results.append(Result(case.id, case.group, case.stage, PYTEST, "coberto pelo pytest"))
            continue
        assert case.repository and case.title is not None and case.description is not None
        before = _tokens(usage)
        try:
            _, guidelines = await catalog(case.repository)
            outcome = await route(
                router,
                repository=case.repository,
                title=case.title,
                description=case.description,
                enrichments=(),
                guidelines=guidelines,
            )
        except Exception as exc:  # noqa: BLE001 - o motivo vai para a tabela
            result = Result(
                case.id, case.group, "router", FAIL, "router_error", detail=type(exc).__name__
            )
        else:
            if isinstance(outcome, Routed):
                ok, reason = score_router(case, outcome.decision)
                result = Result(
                    case.id,
                    case.group,
                    "router",
                    PASS if ok else FAIL,
                    reason,
                    llm_calls=outcome.llm_calls,
                    detail=outcome.decision.category,
                )
            else:
                code = outcome.rejection.code
                reason = "evidence_not_literal" if code == "ungrounded_claim" else "router_error"
                result = Result(
                    case.id, case.group, "router", FAIL, reason, llm_calls=2, detail=code
                )
        after = _tokens(usage)
        result.input_tokens = after[0] - before[0]
        result.output_tokens = after[1] - before[1]
        results.append(result)
    return results


async def evaluate_specialists(
    cases: Sequence[IssueCase],
    runner: SpecialistRunner,
    catalog: CatalogLoader,
    *,
    models: Mapping[str, str],
    catalog_source_dir: str | Path,
) -> list[Result]:
    from issue_triage.adapters.catalog_tools import docs_dir

    by_id = {case.id: case for case in load_cases().cases}
    results: list[Result] = []
    for case in cases:
        if case.id not in SPECIALIST_CASES:
            continue
        base = by_id[case.case_ref] if case.case_ref else case
        assert base.repository and base.title is not None and base.description is not None
        expected = case.expected if case.stage == "specialist" else ""
        extra: dict[str, Any] = {"fail_if": case.fail_if or ""}
        try:
            project, guidelines = await catalog(base.repository)
            if project is None:
                raise LookupError("repository outside the catalog")
            outcome = await run_specialist(
                runner,
                category=base.expected,
                models=models,
                project=project,
                text=IssueText(base.title, base.description, False, ()),
                guidelines=guidelines,
                previous=(),
                docs_dir=docs_dir(catalog_source_dir, base.repository),
            )
        except Exception as exc:  # noqa: BLE001
            results.append(
                Result(
                    case.id,
                    case.group,
                    "specialist",
                    FAIL,
                    "specialist_error",
                    detail=type(exc).__name__,
                    expected_text=expected,
                    extra=extra,
                )
            )
            continue
        status, reason = PASS, ""
        proposal = outcome.output.model_dump() if outcome.output else None
        if outcome.rejection is not None:
            status, reason = FAIL, outcome.rejection.code
        elif (
            case.id == "I05"
            and INJECTED_MENTION in json.dumps(proposal, ensure_ascii=False).lower()
        ):
            status, reason = FAIL, "injected_mention"
        extra["proposal"] = proposal
        results.append(
            Result(
                case.id,
                case.group,
                "specialist",
                status,
                reason,
                llm_calls=outcome.llm_calls,
                expected_text=expected,
                extra=extra,
            )
        )
    return results


def format_table(
    results: Sequence[Result], price_in: float | None = None, price_out: float | None = None
) -> str:
    header = f"{'caso':<5} {'etapa':<10} {'resultado':<9} {'llm':>3} {'tok_in':>7} {'tok_out':>7}"
    lines = [f"{header}  motivo"]
    for r in results:
        lines.append(
            f"{r.case_id:<5} {r.stage:<10} {r.status:<9} {r.llm_calls:>3} "
            f"{r.input_tokens:>7} {r.output_tokens:>7}  {r.reason}"
        )
        if r.stage == "specialist":
            if r.expected_text:
                lines.append(f"      esperado: {r.expected_text}")
            if r.extra.get("fail_if"):
                lines.append(f"      reprova se: {r.extra['fail_if']}")
            if r.extra.get("proposal"):
                proposal = json.dumps(r.extra["proposal"], ensure_ascii=False)
                lines.append(f"      proposta: {proposal}")
    lines.append("")
    lines.append("Totais por grupo (ok/avaliados; casos pytest fora da conta)")
    for stage, group in sorted({(r.stage, r.group) for r in results}):
        rows = [r for r in results if (r.stage, r.group) == (stage, group)]
        evaluated = [r for r in rows if r.status != PYTEST]
        lines.append(
            f"  {stage}/{group}: {sum(r.status == PASS for r in evaluated)}/{len(evaluated)} ok, "
            f"pytest {len(rows) - len(evaluated)}, llm_calls {sum(r.llm_calls for r in rows)}"
        )
    routed = [r for r in results if r.stage == "router"]
    tok_in, tok_out = sum(r.input_tokens for r in routed), sum(r.output_tokens for r in routed)
    lines.append(f"llm_calls: {sum(r.llm_calls for r in results)}")
    lines.append(f"tokens do roteador: entrada {tok_in}, saída {tok_out}")
    if price_in is not None and price_out is not None:
        cost = (tok_in * price_in + tok_out * price_out) / 1_000_000
        lines.append(f"custo do roteador: US$ {cost:.4f}")
    else:
        lines.append("custo do roteador: informe --price-in e --price-out (USD por milhão)")
    return "\n".join(lines)


def format_dry_run(cases: Sequence[IssueCase], specialists: bool) -> str:
    lines = ["Dry run: nenhuma chamada à API.", f"{'caso':<5} {'grupo':<10} {'etapa':<10} execução"]
    for case in cases:
        if case.stage == "specialist":
            if specialists and case.id in SPECIALIST_CASES:
                lines.append(f"{case.id:<5} {case.group:<10} {case.stage:<10} especialista")
            continue
        if case.stage == "harness" or not case.active:
            lines.append(f"{case.id:<5} {case.group:<10} {case.stage:<10} pytest (sem API)")
            continue
        lines.append(f"{case.id:<5} {case.group:<10} {case.stage:<10} roteador")
        if specialists and case.id in SPECIALIST_CASES:
            lines.append(f"{case.id:<5} {case.group:<10} {'specialist':<10} especialista")
    return "\n".join(lines)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Avalia o roteador e os especialistas (API real).")
    parser.add_argument("--specialists", action="store_true", help="roda também E01 a E04 e I05")
    parser.add_argument("--only", help="ids separados por vírgula (ex.: R01,R02)")
    parser.add_argument("--json", metavar="ARQUIVO", help="grava os resultados em JSON")
    parser.add_argument("--dry-run", action="store_true", help="lista os casos, sem chamar a API")
    parser.add_argument("--price-in", type=float, help="USD por milhão de tokens de entrada")
    parser.add_argument("--price-out", type=float, help="USD por milhão de tokens de saída")
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace, cases: list[IssueCase]) -> list[Result]:
    from anthropic import AsyncAnthropic
    from psycopg_pool import AsyncConnectionPool

    from issue_triage.adapters.anthropic_router import AnthropicRouterLLM
    from issue_triage.adapters.catalog import find_project, list_guidelines
    from issue_triage.adapters.claude_specialist import ClaudeSpecialistRunner
    from issue_triage.config import Settings

    settings = Settings()
    key = settings.ANTHROPIC_API_KEY
    if key is None or not key.get_secret_value():
        raise SystemExit("ANTHROPIC_API_KEY não definida (ambiente ou .env).")
    usage = UsageClient(
        AsyncAnthropic(
            api_key=key.get_secret_value(), timeout=settings.ROUTER_TIMEOUT_S, max_retries=0
        )
    )
    router = AnthropicRouterLLM(
        key,
        model=settings.ROUTER_MODEL,
        max_tokens=settings.ROUTER_MAX_TOKENS,
        timeout_s=settings.ROUTER_TIMEOUT_S,
        client=usage,
    )
    async with AsyncConnectionPool(settings.DATABASE_URL, open=False) as pool:
        await pool.open()

        async def catalog(repository: str) -> tuple[Project | None, list[TriageGuideline]]:
            async with pool.connection() as conn:
                return await find_project(conn, repository), await list_guidelines(conn, repository)

        results = await evaluate_router(cases, router, catalog, usage)
        if args.specialists:
            runner = ClaudeSpecialistRunner(
                key,
                max_turns=settings.SPECIALIST_MAX_TURNS,
                max_tool_calls=settings.SPECIALIST_MAX_TOOL_CALLS,
                max_budget_usd=settings.SPECIALIST_MAX_BUDGET_USD,
                max_read_chars=settings.SPECIALIST_MAX_READ_CHARS,
                timeout_s=settings.SPECIALIST_TIMEOUT_S,
            )
            results += await evaluate_specialists(
                cases,
                runner,
                catalog,
                models={
                    "bug": settings.BUG_SPECIALIST_MODEL,
                    "melhoria": settings.IMPROVEMENT_SPECIALIST_MODEL,
                    "dominio": settings.DOMAIN_SPECIALIST_MODEL,
                },
                catalog_source_dir=settings.CATALOG_SOURCE_DIR,
            )
    return results


def main(argv: Sequence[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    args = _parse_args(argv)
    only = [item.strip() for item in args.only.split(",") if item.strip()] if args.only else None
    cases = select_cases(only)
    if args.dry_run:
        print(format_dry_run(cases, args.specialists))
        return 0
    if sys.platform == "win32":
        # O psycopg assíncrono não funciona com o ProactorEventLoop, padrão no Windows.
        results = asyncio.run(_run(args, cases), loop_factory=asyncio.SelectorEventLoop)
    else:
        results = asyncio.run(_run(args, cases))
    print(format_table(results, args.price_in, args.price_out))
    if args.json:
        Path(args.json).write_text(
            json.dumps([asdict(r) for r in results], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
