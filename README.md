# issue-triage

Consome a fila `analysis`, roteia a issue, chama um especialista, valida a proposta e grava o comentário pronto para o `issue-publisher`.

## Pré-requisitos

- [uv](https://docs.astral.sh/uv/) (instala o Python 3.12 automaticamente, se necessário)

## Comandos

```bash
uv sync                              # instala as dependências
uv run pytest                        # roda os testes
uv run python -m issue_triage.main   # inicia o worker
```

Copie `.env.example` para `.env` para sobrescrever as variáveis de ambiente.

## Documentação

- Plano de desenvolvimento: [docs/plano-desenvolvimento-issue-triage.md](docs/plano-desenvolvimento-issue-triage.md)
- Documentos do projeto: [../docs/doc-reference.md](../docs/doc-reference.md), que aponta para o índice
