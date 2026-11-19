"""Mensagens fixas, em português, dos logs do fluxo principal (RNF14).

O dado variável fica nos campos do JSON; a mensagem nunca é interpolada e nunca leva segredo,
prompt, corpo bruto nem a descrição da issue.
"""

from structlog.typing import EventDict, WrappedLogger

MESSAGES: dict[str, str] = {
    "analysis.received": "Análise recebida da fila",
    "analysis.resumed": "Retomando uma tentativa de análise que já estava em andamento",
    "analysis.discarded_invalid": "Mensagem de análise descartada por ser inválida",
    "analysis.skipped_duplicate": "Mensagem de análise repetida: ignorada",
    "triage.unknown_repo": "Repositório fora do catálogo: issue enviada para revisão humana",
    "triage.empty_description": (
        "Issue sem descrição: pedido de informações preparado para publicação"
    ),
    "triage.routed": "Issue classificada pelo roteador",
    "triage.retry_scheduled": "Falha transitória no provedor de IA: nova tentativa agendada",
    "triage.proposal_validated": "Proposta validada e pronta para publicação",
    "triage.proposal_invalid": (
        "Proposta reprovada na validação: issue enviada para revisão humana"
    ),
    "triage.technical_failure": "Falha técnica na análise: issue enviada para revisão humana",
    "triage.case_closed": "Caso encerrado pela revisão humana",
    "outbox.published": "Proposta enviada para a fila de publicação",
    "outbox.publish_failed": "Falha ao enviar para a fila de publicação: nova tentativa",
    "worker.consumer_error": (
        "Erro no consumidor da fila de análise: a mensagem volta para a fila"
    ),
    "worker.started": "Worker de triagem iniciado",
    "worker.stopped": "Worker de triagem encerrado",
    "worker.missing_api_key": "Chave da Anthropic ausente: worker não iniciado",
}

MAIN_FLOW_EVENTS: tuple[str, ...] = tuple(MESSAGES)


def add_message(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
    """Processador do structlog: acrescenta `message` sem sobrescrever um já informado."""
    event = event_dict["event"]
    event_dict.setdefault("message", MESSAGES.get(event, event))
    return event_dict
