"""Templates do comentário (Issue Triage Spec, seção 4; texto literal de "Contratos > Templates").

O texto final vai em `proposals.comment_body`. O `TRIAGE_TEMPLATE` entra na Etapa 9.
"""

from string import Template

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


def render_missing_info(*, project: str, responsible: str, event_id: str, revision: int) -> str:
    """Monta o pedido de informações; `revision` é o `attempt` que gerou a proposta."""
    return MISSING_INFO_TEMPLATE.substitute(
        projeto=project, responsavel=responsible, event_id=event_id, revision=revision
    )
