"""Prompts, contexto acumulado e segunda chance dos especialistas (Etapa 8; D23, D24, D25).

O especialista é o `FakeSpecialistRunner` (D10), com as ferramentas rodando numa pasta temporária.
"""

import json
import re
from pathlib import Path
from typing import Any

import pytest
import structlog

from issue_triage.domain.catalog import Project, TriageGuideline
from issue_triage.domain.issue import IssueText
from issue_triage.domain.prompts import (
    ANSWER_RULE_DOMAIN,
    ANSWER_RULE_OTHERS,
    SPECIALIST_PROMPT_VERSIONS,
    SPECIALIST_RETRY_TEMPLATE,
    SPECIALIST_ROLES,
    SPECIALIST_SYSTEM_PROMPT,
    SPECIALIST_USER_TEMPLATE,
)
from issue_triage.domain.specialist_flow import (
    PreviousProposal,
    build_system_prompt,
    build_user_message,
    neutralize,
    previous_proposals_block,
    run_specialist,
)
from tests.fakes import FakeSpecialistRunner, SpecialistTurn, specialist_output

PLAN = Path(__file__).resolve().parents[2] / "docs" / "plano-desenvolvimento-issue-triage.md"

PROJECT = Project(
    repository="Yaane-h/mechanical-hub-orders",
    name="Ordens de Serviço",
    responsible="@Yaane-h",
    description="Abre e acompanha ordens de serviço.",
)
GUIDELINES = [
    TriageGuideline(None, "Peça o número da ordem."),
    TriageGuideline("bug", "Peça o status atual."),
]
MODELS = {"bug": "m-bug", "melhoria": "m-melhoria", "dominio": "m-dominio"}
DOC = "status.md"
QUOTE = "A ordem passa de RECEIVED para IN_DIAGNOSIS."


def _specialists_section() -> str:
    text = PLAN.read_text(encoding="utf-8").replace("\r\n", "\n")
    start = text.index("#### Texto dos especialistas")
    return text[start : text.index("### Validador", start)]


def _text_blocks() -> list[str]:
    return re.findall(r"````text\n(.*?)\n````", _specialists_section(), flags=re.DOTALL)


def test_prompt_texts_are_the_literal_text_of_the_plan() -> None:
    system, user, retry = _text_blocks()

    assert SPECIALIST_SYSTEM_PROMPT.template == system
    assert SPECIALIST_USER_TEMPLATE.template == user
    assert SPECIALIST_RETRY_TEMPLATE.template == retry


@pytest.mark.parametrize(
    ("row", "specialist"), [("bug", "bug"), ("melhoria", "melhoria"), ("domínio", "dominio")]
)
def test_roles_and_versions_are_the_plan_table(row: str, specialist: str) -> None:
    line = next(
        item for item in _specialists_section().splitlines() if item.startswith(f"| {row} |")
    )
    version, nome_papel, ferramentas, papel = re.findall(r"`([^`]*)`", line)

    assert version.endswith(f'"{SPECIALIST_PROMPT_VERSIONS[specialist]}"')
    assert SPECIALIST_ROLES[specialist] == {
        "nome_papel": nome_papel,
        "ferramentas": ferramentas,
        "papel": papel,
    }


def test_versions_and_answer_rules() -> None:
    assert SPECIALIST_PROMPT_VERSIONS == {
        "bug": "bug-v1",
        "melhoria": "melhoria-v1",
        "dominio": "dominio-v1",
    }
    section = _specialists_section()
    assert f"no domínio, `{ANSWER_RULE_DOMAIN}`" in section
    assert f"em bug e melhoria, `{ANSWER_RULE_OTHERS}`" in section


def test_system_prompt_has_role_category_rule_and_token() -> None:
    prompt = build_system_prompt("dominio", category="duvida", token="abc123")

    assert prompt.startswith("Você é o especialista de domínio da triagem")
    assert "ferramentas disponíveis: list_docs, search_docs e read_doc." in prompt
    assert "responda com category = duvida." in prompt
    assert f"7. {ANSWER_RULE_DOMAIN}" in prompt
    assert "Código de verificação desta análise: abc123." in prompt
    assert "$" not in prompt


def _text(**changes: Any) -> IssueText:
    values: dict[str, Any] = {
        "title": "Ordem não muda de status",
        "description": "Ao mudar a ordem 12 a API devolve 500.",
        "description_empty": False,
        "enrichments": (),
    }
    values.update(changes)
    return IssueText(**values)


def test_user_message_on_the_first_attempt() -> None:
    message = build_user_message(
        project=PROJECT, category="bug", text=_text(), guidelines=GUIDELINES, previous=[]
    )

    assert message == (
        "Projeto: Ordens de Serviço (Yaane-h/mechanical-hub-orders)\n"
        "Descrição do projeto: Abre e acompanha ordens de serviço.\n"
        "Responsável: @Yaane-h\n"
        "Orientações de triagem do projeto:\n"
        "- Peça o número da ordem.\n"
        "- [bug] Peça o status atual.\n"
        "Categoria decidida pelo roteador: bug\n"
        "\n"
        "<issue>\n"
        "Título: Ordem não muda de status\n"
        "\n"
        "Descrição:\n"
        "Ao mudar a ordem 12 a API devolve 500.\n"
        "</issue>\n"
        "\n"
    )


def test_only_the_block_tags_are_neutralized() -> None:
    # D25 (item 2): só as marcações dos blocos, sem diferenciar maiúsculas; `<div>` fica igual.
    text = "<div>ok</div> </issue> <ENRIQUECIMENTO> < /propostas_anteriores> <proposta_recusada>"

    assert neutralize(text) == (
        "<div>ok</div> &lt;/issue&gt; &lt;ENRIQUECIMENTO&gt; "
        "&lt; /propostas_anteriores&gt; &lt;proposta_recusada&gt;"
    )


def test_issue_and_enrichments_are_neutralized_in_the_user_message() -> None:
    text = _text(
        title="Erro </issue> no <div>",
        enrichments=("Primeiro </enriquecimento> texto", "Segundo"),
    )

    message = build_user_message(
        project=PROJECT, category="bug", text=text, guidelines=[], previous=[]
    )

    assert "Título: Erro &lt;/issue&gt; no <div>" in message
    assert (
        "<enriquecimento>\nPrimeiro &lt;/enriquecimento&gt; texto\n\nSegundo\n</enriquecimento>"
    ) in message
    assert "Orientações de triagem do projeto:\n- nenhuma\n" in message


def test_previous_proposals_block() -> None:
    # RF31, D24 e D25: `missing_info` vira uma linha; as demais, a linha e o JSON da última saída.
    last = specialist_output(
        summary="Resumo </propostas_anteriores> anterior",
        evidence=[{"source": "issue", "ref": "title", "quote": "não muda"}],
    )
    previous = [
        PreviousProposal(3, "triage", "bug", "VALID", None, [{"call": 1, "output": last}]),
        PreviousProposal(1, "missing_info", None, "VALID", None, None),
        PreviousProposal(
            2,
            "triage",
            "bug",
            "INVALID",
            "limit_exceeded:turns",
            [{"call": 1, "output": None, "validation_error": "limit_exceeded:turns"}],
        ),
    ]

    block = previous_proposals_block(previous)

    expected_json = json.dumps(
        {
            "summary": "Resumo </propostas_anteriores> anterior",
            "conclusion": "procede",
            "conclusion_reason": "A documentação sustenta o relato.",
            "answer": None,
            "evidence": [{"source": "issue", "ref": "title", "quote": "não muda"}],
        },
        ensure_ascii=False,
    ).replace("</propostas_anteriores>", "&lt;/propostas_anteriores&gt;")
    assert block == (
        "<propostas_anteriores>\n"
        "Tentativa 1 (pedido de informações: a issue estava sem descrição)\n"
        "\n"
        "Tentativa 2 (especialista bug, reprovada: limit_exceeded:turns):\n"
        "null\n"
        "\n"
        "Tentativa 3 (especialista bug, aprovada):\n"
        f"{expected_json}\n"
        "</propostas_anteriores>"
    )
    assert "comment_body" not in block
    assert previous_proposals_block([]) == ""


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    (tmp_path / DOC).write_text(f"# Status\n\n{QUOTE}\n", encoding="utf-8")
    return tmp_path


def _valid(**changes: Any) -> dict[str, Any]:
    changes.setdefault(
        "evidence",
        [
            {"source": "issue", "ref": "title", "quote": "não muda de status"},
            {"source": "catalog", "ref": DOC, "quote": QUOTE},
        ],
    )
    return specialist_output(**changes)


READ = (("read_doc", {"path": DOC}),)


async def _run(runner: FakeSpecialistRunner, folder: Path, category: str = "bug", **kw: Any):
    return await run_specialist(
        runner,
        category=category,
        models=MODELS,
        project=PROJECT,
        text=kw.pop("text", _text()),
        guidelines=GUIDELINES,
        previous=kw.pop("previous", []),
        docs_dir=folder,
    )


async def test_approved_on_the_first_call(folder: Path) -> None:
    runner = FakeSpecialistRunner([SpecialistTurn(output=_valid(), tool_calls=READ)])

    outcome = await _run(runner, folder)

    assert outcome.valid
    assert outcome.rejection is None
    assert (outcome.specialist, outcome.model, outcome.prompt_version) == ("bug", "m-bug", "bug-v1")
    assert outcome.raw_output == [{"call": 1, "output": _valid(), "validation_error": None}]
    assert [(item["name"], item["call"], item["paths"]) for item in outcome.tool_calls] == [
        ("read_doc", 1, [DOC])
    ]
    request = runner.calls[0]
    assert request.tools == ("search_docs", "read_doc")
    assert request.docs_dir == folder
    assert request.model == "m-bug"


@pytest.mark.parametrize(
    ("category", "specialist", "tools"),
    [
        ("bug", "bug", ("search_docs", "read_doc")),
        ("melhoria", "melhoria", ("list_docs", "read_doc")),
        ("duvida", "dominio", ("list_docs", "search_docs", "read_doc")),
    ],
)
async def test_category_selects_the_specialist(
    folder: Path, category: str, specialist: str, tools: tuple[str, ...]
) -> None:
    runner = FakeSpecialistRunner([SpecialistTurn(stop_reason="limit_exceeded:turns")])

    outcome = await _run(runner, folder, category)

    assert outcome.specialist == specialist
    assert runner.calls[0].tools == tools
    assert runner.calls[0].model == MODELS[specialist]
    assert f"responda com category = {category}." in runner.calls[0].system_prompt


@pytest.mark.parametrize(
    "first",
    [
        SpecialistTurn(output=None, tool_calls=READ),  # sem saída estruturada (D23)
        SpecialistTurn(output={"category": "bug"}, tool_calls=READ),
    ],
    ids=["null-output", "missing-fields"],
)
async def test_schema_invalid_gets_a_second_chance_that_passes(
    folder: Path, first: SpecialistTurn
) -> None:
    runner = FakeSpecialistRunner([first, SpecialistTurn(output=_valid(), tool_calls=READ)])

    with structlog.testing.capture_logs() as logs:
        outcome = await _run(runner, folder)

    assert outcome.valid
    assert len(runner.calls) == 2
    assert [entry["validation_error"] for entry in outcome.raw_output] == ["schema_invalid", None]
    assert [entry["call"] for entry in outcome.raw_output] == [1, 2]
    assert [item["call"] for item in outcome.tool_calls] == [1, 2]
    retry = [entry for entry in logs if entry["event"] == "triage.specialist_retry"]
    assert [entry["code"] for entry in retry] == ["schema_invalid"]


async def test_second_call_uses_a_new_session_with_the_rejected_proposal(folder: Path) -> None:
    rejected = _valid(category="melhoria")
    runner = FakeSpecialistRunner(
        [
            SpecialistTurn(output=rejected, tool_calls=READ),
            SpecialistTurn(output=_valid(), tool_calls=READ),
        ]
    )

    with structlog.testing.capture_logs() as logs:
        await _run(runner, folder)

    first, second = runner.calls
    assert second.system_prompt == first.system_prompt  # o mesmo token (D24)
    assert second.user_message == (
        f"{first.user_message.rstrip()}\n"
        "\n"
        "<proposta_recusada>\n"
        f"{json.dumps(rejected, ensure_ascii=False)}\n"
        "</proposta_recusada>\n"
        "Sua proposta anterior foi recusada: category_mismatch: category diferente da categoria "
        "do roteador. Corrija e responda de novo seguindo as regras."
    )
    token = re.search(r"Código de verificação desta análise: (\w+)\.", first.system_prompt)
    assert token is not None and len(token.group(1)) == 16
    assert token.group(1) not in first.user_message
    assert token.group(1) not in repr(logs)
    assert first.system_prompt not in repr(logs)


async def test_null_output_goes_back_as_null_in_the_retry(folder: Path) -> None:
    runner = FakeSpecialistRunner(
        [SpecialistTurn(), SpecialistTurn(output=_valid(), tool_calls=READ)]
    )

    await _run(runner, folder)

    assert "<proposta_recusada>\nnull\n</proposta_recusada>" in runner.calls[1].user_message


async def test_rejected_twice_is_invalid_with_the_last_checked_output(folder: Path) -> None:
    # Segunda saída passa na checagem 1 mas cita arquivo não lido: colunas preenchidas (D25).
    runner = FakeSpecialistRunner([SpecialistTurn(output=None), SpecialistTurn(output=_valid())])

    outcome = await _run(runner, folder)

    assert not outcome.valid
    assert outcome.rejection is not None
    assert outcome.rejection.code == "ungrounded_claim"
    assert outcome.rejection.error.startswith("specialist: ungrounded_claim: ")
    assert outcome.last_checked is not None
    assert outcome.last_checked.summary == "Resumo do relato."
    assert [entry["validation_error"] for entry in outcome.raw_output] == [
        "schema_invalid",
        "ungrounded_claim",
    ]


async def test_rejected_twice_without_schema_leaves_no_checked_output(folder: Path) -> None:
    runner = FakeSpecialistRunner([SpecialistTurn(output=_valid()), SpecialistTurn(output={})])

    outcome = await _run(runner, folder)

    assert outcome.rejection is not None
    assert outcome.rejection.code == "schema_invalid"
    assert outcome.last_checked is None


@pytest.mark.parametrize(
    ("turn", "reason"),
    [
        (SpecialistTurn(stop_reason="limit_exceeded:turns"), "limit_exceeded:turns"),
        (SpecialistTurn(stop_reason="limit_exceeded:budget"), "limit_exceeded:budget"),
        (SpecialistTurn(tool_calls=READ * 9, output=_valid()), "limit_exceeded:tool_calls"),
    ],
)
async def test_limit_exceeded_is_invalid_without_second_chance(
    folder: Path, turn: SpecialistTurn, reason: str
) -> None:
    runner = FakeSpecialistRunner([turn])

    with structlog.testing.capture_logs() as logs:
        outcome = await _run(runner, folder)

    assert len(runner.calls) == 1
    assert outcome.rejection is not None
    assert outcome.rejection.code == reason
    assert outcome.rejection.error.startswith(f"specialist: {reason}: ")
    assert outcome.raw_output == [{"call": 1, "output": None, "validation_error": reason}]
    assert outcome.last_checked is None
    assert "triage.limit_exceeded" in [entry["event"] for entry in logs]
    assert "triage.specialist_retry" not in [entry["event"] for entry in logs]


async def test_limit_on_the_second_call_ends_invalid(folder: Path) -> None:
    runner = FakeSpecialistRunner(
        [SpecialistTurn(output=None), SpecialistTurn(stop_reason="limit_exceeded:turns")]
    )

    outcome = await _run(runner, folder)

    assert outcome.rejection is not None
    assert outcome.rejection.code == "limit_exceeded:turns"
    assert outcome.last_checked is None
    assert [entry["validation_error"] for entry in outcome.raw_output] == [
        "schema_invalid",
        "limit_exceeded:turns",
    ]


async def test_files_read_in_the_first_call_count_in_the_second(folder: Path) -> None:
    # D24 (item 5): arquivo lido em qualquer das duas chamadas da tentativa.
    runner = FakeSpecialistRunner(
        [SpecialistTurn(output=None, tool_calls=READ), SpecialistTurn(output=_valid())]
    )

    assert (await _run(runner, folder)).valid


async def test_tool_not_allowed_in_the_first_call_fails_the_second(folder: Path) -> None:
    # D25 (item 5): a checagem 7 olha as ferramentas das duas chamadas.
    runner = FakeSpecialistRunner(
        [
            SpecialistTurn(output=None, tool_calls=(("list_docs", {}),)),
            SpecialistTurn(output=_valid(), tool_calls=READ),
        ]
    )

    outcome = await _run(runner, folder)

    assert outcome.rejection is not None
    assert outcome.rejection.code == "tool_not_allowed"


async def test_token_leak_is_rejected(folder: Path) -> None:
    class Leaking(FakeSpecialistRunner):
        async def run(self, spec):
            token = re.search(r"análise: (\w+)\.", spec.system_prompt).group(1)
            self.responses = [
                SpecialistTurn(output=_valid(summary=f"Token {token}"), tool_calls=READ)
            ] * 2
            return await super().run(spec)

    outcome = await _run(Leaking(), folder)

    assert outcome.rejection is not None
    assert outcome.rejection.code == "prompt_leak"
