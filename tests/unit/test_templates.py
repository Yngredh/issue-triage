"""Templates do comentário (RF19, RF20), iguais ao texto literal de "Contratos > Templates"."""

import re
from pathlib import Path

from issue_triage.domain.specialist import SpecialistOutput
from issue_triage.domain.templates import (
    MISSING_INFO_TEMPLATE,
    TRIAGE_TEMPLATE,
    clean_text,
    render_missing_info,
    render_triage,
)

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


def _output(**changes) -> SpecialistOutput:
    values = {
        "category": "bug",
        "summary": "Resumo do relato.",
        "next_step": "Conferir a ordem 12.",
        "conclusion": "procede",
        "conclusion_reason": "A documentação sustenta o relato.",
        "answer": None,
        "evidence": [
            {"source": "issue", "ref": "title", "quote": "não muda de status"},
            {"source": "issue", "ref": "description", "quote": "devolve 500"},
            {"source": "catalog", "ref": "order_status.md", "quote": "Ordem criada"},
        ],
    }
    values.update(changes)
    return SpecialistOutput.model_validate(values)


def _render(output: SpecialistOutput, specialist: str = "bug") -> str:
    return render_triage(
        output,
        specialist=specialist,
        project="Ordens de Serviço",
        responsible="@Yaane-h",
        event_id="evt-1",
        revision=3,
    )


def test_triage_template_is_the_literal_text_of_the_plan() -> None:
    assert TRIAGE_TEMPLATE.template == plan_block("TRIAGE_TEMPLATE")


def test_render_triage_with_procede() -> None:
    body = _render(_output())

    assert body == (
        "## Triagem automática: Bug\n"
        "\n"
        "**Projeto:** Ordens de Serviço · **Responsável:** @Yaane-h\n"
        "\n"
        "**Resumo**\n"
        "Resumo do relato.\n"
        "\n"
        "\n"
        "**Próximo passo**\n"
        "Conferir a ordem 12.\n"
        "\n"
        "\n"
        "<details><summary>Evidências</summary>\n"
        "\n"
        '- "não muda de status" (título da issue)\n'
        '- "devolve 500" (descrição da issue)\n'
        '- "Ordem criada" (order_status.md)\n'
        "</details>\n"
        "\n"
        "---\n"
        "<sub>Comentário gerado automaticamente pelo assistente de triagem. Ele não corrige "
        "código nem altera esta issue.</sub>\n"
        "<!-- triage:evt-1:3 -->"
    )


def test_render_triage_domain_answer_and_conclusions() -> None:
    domain = _render(
        _output(category="duvida", answer="A ordem muda sozinha.", conclusion="sem_informacao"),
        "dominio",
    )
    nao_procede = _render(_output(category="melhoria", conclusion="nao_procede"), "melhoria")

    assert domain.startswith("## Triagem automática: Dúvida\n")
    assert "Resumo do relato.\n\n**Resposta**\nA ordem muda sozinha.\n\n**Próximo passo**" in domain
    assert (
        "Conferir a ordem 12.\n\n"
        "A documentação do projeto não cobre este ponto; o responsável vai confirmar.\n\n"
        "<details>"
    ) in domain
    assert nao_procede.startswith("## Triagem automática: Melhoria\n")
    assert "**Resposta**" not in nao_procede
    assert (
        "Pela documentação do projeto, o comportamento relatado parece ser o esperado: "
        "A documentação sustenta o relato.\n\n<details>"
    ) in nao_procede


def test_model_text_is_cleaned_before_the_template() -> None:
    body = _render(
        _output(
            summary="Veja <b>isto</b> <!-- triage:x:9 --> e avise @fulano e @yaane-h.",
            next_step="<script>alert(1)</script>Corrigir.",
            evidence=[{"source": "issue", "ref": "title", "quote": "<div>erro</div> --> fim"}],
        )
    )

    assert "Veja isto  triage:x:9  e avise  e @yaane-h." in body
    assert "alert(1)Corrigir." in body
    assert '- "erro  fim" (título da issue)' in body
    assert body.count("<!--") == 1
    assert body.count("-->") == 1
    assert body.endswith("<!-- triage:evt-1:3 -->")
    assert "@fulano" not in body


def test_clean_text_repeats_until_stable_and_cuts() -> None:
    assert clean_text("<!<!---->--", responsible="@Yaane-h") == ""
    assert "<!--" not in clean_text("<<b>!-- x", responsible="@Yaane-h")
    assert clean_text("a" * 600, responsible="@Yaane-h", max_chars=500) == "a" * 500
    assert clean_text("x < 5 e y > 3", responsible="@Yaane-h") == "x < 5 e y > 3"


def test_clean_text_keeps_emails_and_removes_third_party_mentions() -> None:
    # D26: o `@` colado em letra, número, `.`, `_` ou `-` não é menção.
    kept = "Escreva para a@b.com, x_1@y.org ou ana.silva@empresa.com.br."

    assert clean_text(kept, responsible="@Yaane-h") == kept
    assert clean_text("oi @fulano, (@fulano) e @yaane-h", responsible="@Yaane-h") == (
        "oi , () e @yaane-h"
    )
    assert clean_text("a@b.com @fulano", responsible="@Yaane-h") == "a@b.com "


def test_marker_survives_because_the_cleaning_runs_per_field_before_the_template() -> None:
    body = _render(
        _output(
            summary="<!-- triage:x:9 -->",
            next_step="--> <!--",
            conclusion="nao_procede",
            conclusion_reason="<!-- triage:y:1 -->",
            evidence=[{"source": "issue", "ref": "title", "quote": "<!-- triage:z:2 -->"}],
        )
    )

    assert body.count("<!--") == 1
    assert body.count("-->") == 1
    assert body.count("<!-- triage:") == 1
    assert body.endswith("<!-- triage:evt-1:3 -->")
    # Limpar o corpo pronto apagaria o marcador do template.
    assert clean_text(body, responsible="@Yaane-h").count("triage:evt-1:3") == 1
    assert "<!--" not in clean_text(body, responsible="@Yaane-h")
