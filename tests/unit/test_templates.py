"""Template do pedido de informações (RF20), igual ao texto literal de "Contratos > Templates"."""

import re
from pathlib import Path

from issue_triage.domain.templates import MISSING_INFO_TEMPLATE, render_missing_info

PLAN = Path(__file__).resolve().parents[2] / "docs" / "plano-desenvolvimento-issue-triage.md"


def plan_block(constant: str) -> str:
    """Bloco ````markdown```` logo depois da linha que nomeia a constante no plano."""
    text = PLAN.read_text(encoding="utf-8").replace("\r\n", "\n")
    match = re.search(
        rf"constante `{constant}`.*?\n````markdown\n(.*?)\n````\n", text, flags=re.DOTALL
    )
    assert match is not None, f"{constant} not found in the plan"
    return match.group(1)


def test_missing_info_template_is_the_literal_text_of_the_plan() -> None:
    assert MISSING_INFO_TEMPLATE.template == plan_block("MISSING_INFO_TEMPLATE")


def test_render_fills_project_responsible_and_marker() -> None:
    body = render_missing_info(
        project="Ordens de Serviço", responsible="@Yaane-h", event_id="evt-1", revision=2
    )

    assert "em qual tela ou funcionalidade de Ordens de Serviço;" in body
    assert "**Responsável:** @Yaane-h" in body
    assert body.endswith("<!-- triage:evt-1:2 -->")
    assert body.count("<!-- triage:") == 1
    assert "$" not in body
