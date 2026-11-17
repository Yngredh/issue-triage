"""Saída do especialista (D22) e o `FakeSpecialistRunner` (D10)."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from issue_triage.domain.specialist import SPECIALIST_TOOLS, SpecialistOutput, SpecialistRequest
from tests.fakes import FakeSpecialistRunner, SpecialistTurn

OUTPUT = {
    "category": "duvida",
    "summary": "Resumo.",
    "next_step": "Próximo passo.",
    "conclusion": "sem_informacao",
    "conclusion_reason": "Motivo.",
    "answer": None,
    "evidence": [],
}


def test_answer_is_required_in_the_json_but_may_be_null() -> None:
    assert SpecialistOutput.model_validate(OUTPUT).answer is None
    without_answer = {key: value for key, value in OUTPUT.items() if key != "answer"}
    with pytest.raises(ValidationError):
        SpecialistOutput.model_validate(without_answer)
    assert "answer" in SpecialistOutput.model_json_schema()["required"]


@pytest.mark.parametrize(
    "change",
    [
        {"category": "indeterminado"},
        {"summary": ""},
        {"summary": "x" * 501},
        {"answer": "x" * 1001},
        {"conclusion": "talvez"},
        {"extra": "campo"},
        {"evidence": [{"source": "web", "ref": "a", "quote": "b"}]},
        {"evidence": [{"source": "issue", "ref": "title", "quote": "x" * 301}]},
        {"evidence": [{"source": "issue", "ref": "title", "quote": "q"}] * 11},
    ],
)
def test_schema_refuses_values_outside_the_contract(change: dict) -> None:
    with pytest.raises(ValidationError):
        SpecialistOutput.model_validate(OUTPUT | change)


def _request(folder: Path) -> SpecialistRequest:
    return SpecialistRequest(
        specialist="dominio",
        model="fake",
        system_prompt="sistema",
        user_message="dados",
        docs_dir=folder,
        tools=SPECIALIST_TOOLS["dominio"],
    )


async def test_fake_runs_the_real_tools_and_returns_the_output(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("conteúdo", encoding="utf-8")
    runner = FakeSpecialistRunner(
        [SpecialistTurn(output=OUTPUT, tool_calls=(("read_doc", {"path": "a.md"}),))]
    )

    result = await runner.run(_request(tmp_path))

    assert result.raw_output == OUTPUT
    assert result.stop_reason == "completed"
    assert [(call["name"], call["error"]) for call in result.tool_calls] == [("read_doc", None)]


async def test_fake_ends_the_session_when_a_tool_limit_is_hit(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("a" * 50, encoding="utf-8")
    runner = FakeSpecialistRunner(
        [SpecialistTurn(output=OUTPUT, tool_calls=(("read_doc", {"path": "a.md"}),) * 2)],
        max_read_chars=60,
    )

    result = await runner.run(_request(tmp_path))

    assert result.stop_reason == "limit_exceeded:read_chars"
    assert result.raw_output is None
    assert [call["error"] for call in result.tool_calls] == [None, "limit_exceeded"]
