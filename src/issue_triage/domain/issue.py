"""Dados da issue lidos de `inbound` para a análise (D14)."""

from dataclasses import dataclass


@dataclass(frozen=True)
class OriginEvent:
    """`issue.created` de origem do caso, em `inbound.issue_events`."""

    event_id: str
    repository: str
    issue_number: int


@dataclass(frozen=True)
class IssueText:
    """Texto analisado na tentativa (D14).

    `title` e `description` são a versão mais recente recebida (o `issue.created` de origem ou o
    último `issue.edited`); `description_empty` segue a regra `btrim(description) = ''`.
    `enrichments` são os enriquecimentos do revisor, em ordem de `created_at`. Tudo isso é dado,
    nunca instrução (RNF09).
    """

    title: str
    description: str
    description_empty: bool
    enrichments: tuple[str, ...]
