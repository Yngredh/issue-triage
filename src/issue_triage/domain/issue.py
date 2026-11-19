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
    último `issue.edited`); `description_empty` segue a regra de `is_blank` (D19).
    `enrichments` são os enriquecimentos do revisor, em ordem de `created_at`. Tudo isso é dado,
    nunca instrução (RNF09).
    """

    title: str
    description: str
    description_empty: bool
    enrichments: tuple[str, ...]


def is_blank(text: str) -> bool:
    """Diz se o texto é vazio ou só tem espaços em branco de qualquer tipo (D19).

    Espaço, tab, quebra de linha e os demais brancos removidos por `str.strip()` contam como
    vazio, a mesma regra da entrada no issue-analyst-api; `btrim` sozinho só remove espaços.
    """
    return not text.strip()


@dataclass(frozen=True)
class CloseReview:
    """Resolução `close` do revisor, em `inbound.issue_review_history` (RF29, P1)."""

    review_id: int
    final_status: str  # CANCELLED ou FINISHED
