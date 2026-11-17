# issue-triage

Consome a fila `analysis`, roteia a issue, chama um especialista, valida a proposta e grava o comentário pronto para o `issue-publisher`.

## Pré-requisitos

- [uv](https://docs.astral.sh/uv/) (instala o Python 3.12 automaticamente, se necessário)
- Docker com Docker Compose (PostgreSQL e RabbitMQ locais e testes de integração)

## Configuração

Copie `.env.example` para `.env` e preencha os valores. São obrigatórios `DATABASE_URL` e
`RABBITMQ_URL`. `ANTHROPIC_API_KEY` é segredo (nunca vai para o repositório, para o banco nem para
log): o worker real não sobe sem ela (log `worker.missing_api_key` e código de saída 1), mas os
testes automatizados não a usam. Os demais têm padrão:

| Variável | Padrão |
|---|---|
| `LOG_LEVEL`, `SERVICE_NAME` | `INFO`, `issue-triage` |
| `ROUTER_MODEL`, `BUG_SPECIALIST_MODEL`, `IMPROVEMENT_SPECIALIST_MODEL`, `DOMAIN_SPECIALIST_MODEL` | `claude-haiku-5-5` |
| `ROUTER_MAX_TOKENS`, `ROUTER_TIMEOUT_S` | `300`, `20` |
| `SPECIALIST_MAX_TURNS`, `SPECIALIST_MAX_TOOL_CALLS` | `6`, `8` |
| `SPECIALIST_MAX_BUDGET_USD`, `SPECIALIST_MAX_READ_CHARS`, `SPECIALIST_TIMEOUT_S` | `0.05`, `20000`, `90` |
| `ANALYSIS_RETRY_DELAYS_S` | `10,30` (inteiros positivos separados por vírgula) |
| `ANALYSIS_PREFETCH` | `1` |
| `MAX_MANUAL_ATTEMPTS` | `2` |
| `CATALOG_SOURCE_DIR` | `catalog/source` (na imagem, `/app/catalog/source`) |
| `RELAY_POLL_INTERVAL_S`, `RELAY_BATCH_SIZE` | `1`, `100` |

Os limites numéricos precisam ser positivos. Para o compose de desenvolvimento, as URLs são:

```
DATABASE_URL=postgresql://postgres:postgres@localhost:5432/issue_triage
RABBITMQ_URL=amqp://guest:guest@localhost:5672/
```

Nunca commite o `.env`.

## Subir a infraestrutura de desenvolvimento

```bash
docker compose -f docker-compose.dev.yml up -d   # PostgreSQL 16 (5432) e RabbitMQ (5672, painel em 15672)
docker compose -f docker-compose.dev.yml down    # para os contêineres (use -v para apagar os dados)
```

O compose de desenvolvimento usa as mesmas portas dos composes do issue-analyst-api e do
issue-publisher; suba um de cada vez.

## Rodar localmente

```bash
uv sync                                          # instala as dependências
docker compose -f docker-compose.dev.yml up -d   # PostgreSQL e RabbitMQ locais
uv run python -m issue_triage.main               # inicia o worker; Ctrl+C (SIGINT) ou SIGTERM encerra
```

O worker não expõe HTTP. Ao receber SIGTERM ou SIGINT, para de consumir, termina a mensagem em
curso e fecha as conexões (logs `worker.started`, `worker.stop_requested` e `worker.stopped`).

## Testes

```bash
uv run ruff check . && uv run pytest -q
```

Nenhum teste chama a Anthropic de verdade.

## Imagem

```bash
docker build -t issue-triage .
```

Base `ghcr.io/astral-sh/uv:python3.12-bookworm-slim`, como o issue-publisher. A pasta
`catalog/source` é copiada para `/app/catalog/source` (`CATALOG_SOURCE_DIR`).

Claude Code CLI: o `claude-agent-sdk` instalado (0.2.183) publica wheels por plataforma, e cada
uma traz o CLI embutido em `claude_agent_sdk/_bundled/` (no Windows, `claude.exe`, versão
2.1.326; os wheels `manylinux_2_17` para x86_64 e aarch64 têm cerca de 160 MB pelo mesmo motivo).
O SDK usa esse CLI antes de procurar outro no `PATH`, então a imagem não instala Node.js nem o
CLI à parte.

## Documentação

- Plano de desenvolvimento: [docs/plano-desenvolvimento-issue-triage.md](docs/plano-desenvolvimento-issue-triage.md)
- Documentos do projeto: [../docs/doc-reference.md](../docs/doc-reference.md), que aponta para o índice
