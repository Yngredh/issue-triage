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

Antes de iniciar o worker, aplique as migrações do schema `triage` (idempotente; registra a versão em `triage.schema_migrations`):

```bash
uv run python -m issue_triage.migrate
```

O worker não expõe HTTP. Ao receber SIGTERM ou SIGINT, para de consumir, termina a mensagem em
curso e fecha as conexões (logs `worker.started`, `worker.stop_requested` e `worker.stopped`).

## Ambiente integrado

`docker-compose.yml` sobe, num banco único (`issue_maintenance`), o PostgreSQL 16, o RabbitMQ, o
issue-analyst-api, o issue-triage e o issue-publisher. Os repositórios `../issue-analyst-api` e
`../issue-publisher` precisam estar ao lado deste. Somente para uso local.

```bash
cp .env.example .env     # preencha ANTHROPIC_API_KEY, GITHUB_TOKEN e GITHUB_WEBHOOK_SECRET
docker compose config    # valida o compose
docker compose up -d --build
docker compose logs -f issue-triage
docker compose down      # use -v para apagar os dados (o initdb roda de novo na próxima subida)
```

  `publisher_svc`, schemas com dono, `GRANT CREATE ON DATABASE` no banco atual e privilégios
  padrão; senha igual ao nome do papel, só uso local), `catalog/schema.sql`, `catalog/seed.sql` e
  `infra/initdb/03_catalog_grants.sql` (`SELECT` do `catalog` para o issue-triage e o issue-publisher) e `infra/initdb/04_inbound_review_history.sql` (tabela `inbound.issue_review_history`, da Revisão Humana Spec, que o issue-triage lê em toda análise e nenhuma migração cria; D34).
  O initdb só roda em volume novo: num banco que já existia, use `docker compose down -v` ou aplique o arquivo com `psql -U postgres -d issue_maintenance -f`.
- Cada serviço roda a própria migração (`python -m <pacote>.migrate`) com o papel dele antes de
  iniciar. O issue-triage e o issue-publisher sobem e migram primeiro; cada um fica saudável
  (`healthcheck`) quando a última migração dele está registrada em `triage.schema_migrations` ou
  `publisher.schema_migrations`. O issue-analyst-api espera os dois (`service_healthy`) e só então
  migra, para a view `inbound.issue_presentation` (RF27) ler tabelas de `triage` e `publisher` já
  criadas (P2). A migração `0002` deste repositório dá `SELECT` da view
  `triage.publishable_proposals` ao `publisher_svc` (só se o papel existir).
- Portas do host (configuráveis no `.env`, sem colidir com os containers `mechanical-hub-*`):
  PostgreSQL `5433` (`POSTGRES_PORT`), RabbitMQ `5673` e `15673` (`RABBITMQ_PORT` e
  `RABBITMQ_MANAGEMENT_PORT`), issue-analyst-api `8081` (`ANALYST_API_PORT`).
- O ambiente integrado exige `ANTHROPIC_API_KEY` para subir (P5): sem ela o worker registra
  `worker.missing_api_key` e sai com código 1, e o issue-analyst-api não sobe, porque espera o
  issue-triage ficar saudável.
- `catalog/source` é montada somente leitura no `issue-triage`.

### Aplicar a seed de novo

A seed é reexecutável (upsert dos projetos e troca das orientações). Depois de editar
`catalog/seed.sql`, aplique-a sem recriar o banco:

```bash
psql postgresql://postgres:postgres@localhost:5433/issue_maintenance -f catalog/seed.sql
```

(No compose de desenvolvimento, `postgresql://postgres:postgres@localhost:5432/issue_triage`, depois
de aplicar `catalog/schema.sql` uma vez.) As permissões do `catalog` não mudam.

### Enviar uma análise de teste

```bash
uv run python scripts/send_analysis.py --from-case R01
uv run python scripts/send_analysis.py --repository Yaane-h/mechanical-hub-orders \
    --title "Erro ao abrir ordem" --description "POST /orders devolve 500" [--issue-number N] [--event-id ID]
```

Grava um `issue.created` em `inbound.issue_events` (`event_id = dev-<uuid>`) e publica a mensagem na
fila `analysis`. Usa `DATABASE_URL` e `RABBITMQ_URL` do ambiente ou do `.env` (em desenvolvimento, o
usuário `postgres` e as portas do host). Sem `--issue-number`, usa o horário atual em segundos.

### Logs

Cada linha é um JSON com `module` e `func_name` (de onde o log saiu) e `message` (texto fixo em
português que diz o que aconteceu), além do `event` estável e do `event_id` de correlação. As
mensagens ficam em `src/issue_triage/log_messages.py`. Exemplo:

```json
{"timestamp": "2026-11-19T11:29:00Z", "level": "info", "service": "issue-triage", "module": "analysis", "func_name": "_process", "event": "analysis.received", "message": "Análise recebida da fila", "event_id": "12151e32-...", "repository": "Yaane-h/mechanical-hub-orchestrator", "issue_number": 1, "trigger": "initial"}
```

- O initdb roda, nesta ordem: `infra/initdb/00_roles.sql` (papéis `analyst_api`, `triage_svc` e
## Testes

```bash
uv run ruff check . && uv run pytest -q
```

Precisa do Docker ativo. Nenhum teste chama a Anthropic de verdade nem o GitHub (RNF17). O que é real
e o que é simulado:

| Testes | Real | Simulado |
|---|---|---|
| `tests/unit` | validações, prompts, validador, ferramentas do catálogo (leitura de `catalog/source`) | roteador (`FakeRouterLLM`), especialista (`FakeSpecialistRunner`) e cliente do SDK; sem rede |
| `tests/integration` (análise, roteador, especialistas, falhas técnicas, encerramento, logs, publicação, outbox) | PostgreSQL 16 e RabbitMQ 3 do testcontainers, migrações, catálogo (DDL e seed) | IA (portas falsas); `inbound` é um stub de teste; o issue-publisher e o GitHub não participam |
| `test_publisher_grant.py` | PostgreSQL 16 próprio por teste, `00_roles.sql` e a migração `0002` com papéis de verdade | tudo o mais |
| `test_send_analysis.py` | PostgreSQL e RabbitMQ do testcontainers e o script | IA |
| `test_evaluate_script.py` e `test_issues_fixture.py` | leitura de `issues.yaml` e `catalog/seed.sql`, critérios de acerto | roteador e especialista falsos |

## Avaliação com a API real

```bash
uv run python scripts/evaluate.py --dry-run                 # lista os casos, sem API, banco nem chave
uv run python scripts/evaluate.py --price-in 1 --price-out 5   # roteador (USD por milhão de tokens)
uv run python scripts/evaluate.py --specialists --json saida.json
uv run python scripts/evaluate.py --only R01,H02
```

Roda da raiz do repositório, no ambiente de desenvolvimento, e chama a API real da Anthropic (tem
custo): exige `ANTHROPIC_API_KEY` e `DATABASE_URL` (catálogo já com a seed). Não roda no `pytest`.
O roteador roda nos casos ativos de `tests/routing/fixtures/issues.yaml` com `stage = router`; H04 a
H06 e o R12 saem como `pytest`. Acerta se a categoria bate, a evidência é literal na issue e, quando o
caso lista `evidence`, ela coincide com alguma (substring, sem diferenciar maiúsculas e espaços).
Motivos de erro: `category_mismatch`, `evidence_not_literal`, `evidence_off_target` e `router_error`.
Com `--specialists`, E01 a E04 e I05 passam pelo especialista e pelo validador (acerta se a proposta é
`VALID` e, em I05, sem `@atacante`) e o texto esperado é impresso para julgamento humano. A saída traz
tabela por caso, totais por grupo, `llm_calls` e o custo do roteador (só tokens sem `--price-in` e
`--price-out`).

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
