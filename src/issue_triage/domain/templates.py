"""Templates do comentário (Issue Triage Spec, seção 4; texto literal de "Contratos > Templates").

O texto final vai em `proposals.comment_body`. Todo texto do modelo é limpo antes de entrar no
template de triagem (`clean_text`): o único marcador `<!-- triage:... -->` do corpo é o do
template.
"""

import re
from string import Template

from issue_triage.domain.specialist import SpecialistName, SpecialistOutput

# Triagem (RF19).
TRIAGE_TEMPLATE = Template(
    """## Triagem automática: $categoria_rotulo

**Projeto:** $projeto · **Responsável:** $responsavel

**Resumo**
$summary

$bloco_resposta
**Próximo passo**
$next_step

$bloco_conclusao
<details><summary>Evidências</summary>

$lista_evidencias
</details>

---
<sub>Comentário gerado automaticamente pelo assistente de triagem. Ele não corrige código nem altera esta issue.</sub>
<!-- triage:$event_id:$revision -->"""  # noqa: E501
)

# Pedido de informações (RF20), sem IA.
MISSING_INFO_TEMPLATE = Template(
    """## Precisamos de mais informações

Esta issue foi aberta sem descrição, então ainda não foi possível fazer a triagem.

Edite a descrição contando:
- o que você estava fazendo e em qual tela ou funcionalidade de $projeto;
- o que esperava que acontecesse;
- o que aconteceu, com a mensagem de erro, se houver.

Assim que a descrição for editada, a triagem roda de novo automaticamente.

**Responsável:** $responsavel
<!-- triage:$event_id:$revision -->"""
)

CATEGORY_LABELS = {"bug": "Bug", "melhoria": "Melhoria", "duvida": "Dúvida"}

CONCLUSION_NAO_PROCEDE = (
    "Pela documentação do projeto, o comportamento relatado parece ser o esperado: "
)
CONCLUSION_SEM_INFORMACAO = (
    "A documentação do projeto não cobre este ponto; o responsável vai confirmar."
)

ISSUE_REF_LABELS = {"title": "título da issue", "description": "descrição da issue"}

# Tamanhos máximos dos campos do modelo (`SpecialistOutput`), aplicados depois da limpeza.
MAX_SUMMARY = 500
MAX_NEXT_STEP = 500
MAX_CONCLUSION_REASON = 500
MAX_ANSWER = 1000
MAX_QUOTE = 300

_HTML_TAG = re.compile(r"</?[A-Za-z][^<>]*>")
_MENTION = re.compile(r"@[\w-]+")


def render_missing_info(*, project: str, responsible: str, event_id: str, revision: int) -> str:
    """Monta o pedido de informações; `revision` é o `attempt` que gerou a proposta."""
    return MISSING_INFO_TEMPLATE.substitute(
        projeto=project, responsavel=responsible, event_id=event_id, revision=revision
    )


def _is_responsible(mention: str, responsible: str) -> bool:
    return mention.lstrip("@").casefold() == responsible.strip().lstrip("@").casefold()


def clean_text(text: str, *, responsible: str, max_chars: int | None = None) -> str:
    """Limpa um texto do modelo antes do template (Issue Triage Spec, seção 4).

    Remove marcações HTML, `<!--` e `-->` e as `@menções` que não sejam do responsável, e corta
    no tamanho máximo. Repete a limpeza até o texto parar de mudar, para que uma remoção não
    forme outra marcação.
    """
    previous = None
    while previous != text:
        previous = text
        text = _HTML_TAG.sub("", text)
        text = text.replace("<!--", "").replace("-->", "")
        text = _MENTION.sub(
            lambda match: match.group(0) if _is_responsible(match.group(0), responsible) else "",
            text,
        )
    return text if max_chars is None else text[:max_chars]


def render_triage(
    output: SpecialistOutput,
    *,
    specialist: SpecialistName,
    project: str,
    responsible: str,
    event_id: str,
    revision: int,
) -> str:
    """Monta o comentário de triagem (RF19); `revision` é o `attempt` que gerou a proposta."""

    def clean(text: str, max_chars: int | None = None) -> str:
        return clean_text(text, responsible=responsible, max_chars=max_chars)

    answer_block = ""
    if specialist == "dominio" and output.answer is not None:
        answer_block = f"**Resposta**\n{clean(output.answer, MAX_ANSWER)}\n"

    conclusion_block = ""
    if output.conclusion == "nao_procede":
        reason = clean(output.conclusion_reason, MAX_CONCLUSION_REASON)
        conclusion_block = f"{CONCLUSION_NAO_PROCEDE}{reason}\n"
    elif output.conclusion == "sem_informacao":
        conclusion_block = f"{CONCLUSION_SEM_INFORMACAO}\n"

    evidence_lines = []
    for item in output.evidence:
        if item.source == "issue":
            label = ISSUE_REF_LABELS[item.ref]
        else:
            label = clean(item.ref)
        evidence_lines.append(f'- "{clean(item.quote, MAX_QUOTE)}" ({label})')

    return TRIAGE_TEMPLATE.substitute(
        categoria_rotulo=CATEGORY_LABELS[output.category],
        projeto=project,
        responsavel=responsible,
        summary=clean(output.summary, MAX_SUMMARY),
        bloco_resposta=answer_block,
        next_step=clean(output.next_step, MAX_NEXT_STEP),
        bloco_conclusao=conclusion_block,
        lista_evidencias="\n".join(evidence_lines),
        event_id=event_id,
        revision=revision,
    )
