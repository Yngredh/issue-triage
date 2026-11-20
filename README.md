# Assistente de manutenção de software

Backend que recebe issues de repositórios por webhook, consulta um catálogo de projetos, classifica a
issue com IA (bug, melhoria ou dúvida), valida a proposta de um especialista e publica um comentário
de triagem na issue de origem. Repositório desconhecido, descrição vazia ou conteúdo insuficiente nunca
geram ação indevida: vão para revisão humana ou recebem um pedido de informações.

O sistema tem **três serviços em processos separados**, ligados por duas filas e por um banco com um
schema por dono. Este repositório (`issue-triage`) é o **repositório principal da entrega**: traz o
`docker-compose.yml` que sobe todas as dependencias do sistema. Cada
serviço tem o próprio README com o que é exclusivo dele.

| Serviço | Papel | Repositório | README do serviço |
|---|---|---|---|
| `issue-analyst-api` | Recebe o webhook, valida, deduplica, grava e expõe a consulta | https://github.com/Yngredh/issue-analyst-api | [README](https://github.com/Yngredh/issue-analyst-api/blob/main/README.md) |
| `issue-triage` | Consome a fila `analysis`, roteia, chama o especialista e valida | este repositório | [docs/servico-issue-triage.md](docs/servico-issue-triage.md) |
| `issue-publisher` | Consome a fila `publish` e comenta na issue sem duplicar | https://github.com/Yngredh/issue-publisher | [README](https://github.com/Yngredh/issue-publisher/blob/main/README.md) |

## Mapa da entrega

| O que o desafio pede | Onde está |
|---|---|
| Instruções para executar | [Como executar](#como-executar) |
| Instruções para testar | [Como testar](#como-testar) |
| Variáveis de ambiente de exemplo, sem segredos | [Variáveis de ambiente](#variáveis-de-ambiente) e [`.env.example`](.env.example) |
| Catálogo de domínio usado pela ferramenta | [Catálogo de domínio](#catálogo-de-domínio) |
| Exemplos de eventos | [Exemplos de eventos](#exemplos-de-eventos) e a pasta [`examples/`](examples) |
| Métodos aprendidos nas aulas e onde estão implementados | [Métodos de arquitetura distribuída](#métodos-de-arquitetura-distribuída) |
| Desenho da arquitetura | [Arquitetura](#arquitetura) |
| Justificativa das escolhas técnicas | [Escolhas técnicas](#escolhas-técnicas) |
| Demonstração (parar a análise, enviar, retomar, reenviar) | [Demonstração](#demonstração) |
| Evidências dos cenários do nível | [Evidências](#evidências) |
| Rastreabilidade com os requisitos | [Rastreabilidade dos requisitos](#rastreabilidade-dos-requisitos) |
| Decisões e limitações | [Limitações e decisões](#limitações-e-decisões) |
| Declaração de uso de IA | [Uso de IA](#uso-de-ia) |

## Arquitetura

```mermaid
flowchart TB
    gh_in["GitHub<br/>webhook de issues"]
    api["issue-analyst-api<br/>entrada e consulta"]
    q_an(["analysis<br/>fila no RabbitMQ"])
    tri["issue-triage<br/>roteador e especialistas"]
    q_pub(["publish<br/>fila no RabbitMQ"])
    pub["issue-publisher<br/>publicação"]
    gh_out["GitHub<br/>API de comentários"]
    rev["Revisor<br/>consulta"]
    ia["Provedor de IA<br/>Claude Haiku 5.5"]
    db_in[("Schema inbound<br/>PostgreSQL")]
    db_tri[("Schema triage<br/>PostgreSQL")]
    db_cat[("Schema catalog<br/>somente leitura")]
    db_pub[("Schema publisher<br/>PostgreSQL")]

    gh_in --> api
    api --> q_an
    q_an --> tri
    tri --> q_pub
    q_pub --> pub
    pub -.-> gh_out
    rev -.-> api
    tri -.-> ia
    api <-.-> db_in
    tri <-.-> db_tri
    pub <-.-> db_pub
    tri -.-> db_cat
    pub -.-> db_cat

    classDef service fill:#eeedfe,stroke:#534ab7,color:#3c3489
    classDef store fill:#e1f5ee,stroke:#0f6e56,color:#085041
    classDef external fill:#fcfcfb,stroke:#898781,color:#0b0b0b
    class api,tri,pub service
    class q_an,q_pub,db_in,db_tri,db_cat,db_pub store
    class gh_in,gh_out,rev,ia external
```

Setas contínuas são mensagens; tracejadas são consultas ou dados. O trabalho desce em uma só direção:
cada serviço grava o resultado e a mensagem de saída na mesma transação (outbox), e o próximo só
começa quando consome a fila. Por isso qualquer serviço pode parar sem que o anterior perca o que já
aceitou.

| Elemento | O que é | Dono |
|---|---|---|
| `issue-analyst-api` | API FastAPI: `POST /webhooks/github`, `GET /issues/{owner}/{repo}/{number}`, `GET /health`; relay da outbox `inbound` para a fila `analysis` | schema `inbound` |
| `issue-triage` | Worker: consome `analysis`, aplica checagens sem IA, roteia, chama o especialista, valida, grava a proposta e a outbox `publish` | schema `triage` |
| `issue-publisher` | Worker: consome `publish`, lê a proposta pela view `triage.publishable_proposals`, comenta na issue | schema `publisher` |
| Fila `analysis` | Durável; produtor `issue-analyst-api`, consumidor `issue-triage` | declarada pelo `issue-triage` e pelo `issue-analyst-api` |
| Fila `publish` | Durável; produtor `issue-triage`, consumidor `issue-publisher`; `publish.retry` é a fila de espera do publisher | declarada pelo `issue-triage` |
| Schema `catalog` | Projetos aceitos, responsáveis e orientações; carregado por seed | `postgres` (initdb), leitura para os outros |

Fluxo das decisões e estados do caso (`ACCEPTED`, `ANALYZING`, `READY_TO_PUBLISH`, `NEEDS_REVIEW`,
`MAX_RETRIES_REACHED`, `CANCELLED`, `FINISHED`) estão no documento High Level Design.

## Escolhas técnicas

| Camada | Escolha | Por quê |
|---|---|---|
| Linguagem | Python 3.12 com asyncio | Mais bibliotecas e SDKs para soluções com IA; usada nos três serviços |
| HTTP | FastAPI + Pydantic v2 | Assíncrono; os mesmos modelos validam webhook, consulta e contratos; dá acesso ao corpo bruto para conferir o HMAC antes de ler o JSON |
| Broker | RabbitMQ | Filas duráveis guardam o trabalho com o consumidor parado; ack manual; dead-letter para a fila de espera `publish.retry`; publisher confirms no relay. Kafka resolveria volume e vários grupos de consumidores, que o desafio não tem |
| Banco | PostgreSQL | `INSERT ... ON CONFLICT` para deduplicar, outbox na mesma transação do evento, `FOR UPDATE SKIP LOCKED` no relay, JSONB só onde serve (payload bruto e proposta) |
| IA | Anthropic, Claude Haiku 5.5 (`claude-haiku-5-5`) | Modelo menor, com custo e latência baixos para issues curtas; o modelo de cada papel é variável de ambiente e pode ser trocado sem alterar código |
| Integração com IA | SDK `anthropic` no roteador; Claude Agent SDK nos especialistas | O roteador faz uma chamada só, sem ferramentas; os especialistas precisam do laço de agente com ferramentas da aplicação, limite de turnos e de custo |
| Testes | pytest, pytest-asyncio, respx, testcontainers | IA e GitHub atrás de portas, com versões simuladas; PostgreSQL e RabbitMQ reais em container |
| Logs | structlog, JSON, com `event_id` como correlação entre os três serviços | Mostra na demonstração a retomada e a não duplicação |
| Execução | Docker Compose | Sobe tudo com um comando e permite parar só o `issue-triage` na demonstração |

## Métodos de arquitetura distribuída

Os dois métodos declarados para esta entrega são o **outbox transacional** e o **consumidor
idempotente**. Eles respondem aos dois comportamentos pedidos: continuar recebendo eventos com a análise
parada e não duplicar a ação no destino. A tabela traz, para cada método, a aula de referência, o
problema que resolve, onde está no código e como a demonstração o mostra.

| Método | Aula de referência | Problema que resolve | Onde está no código | Como a demonstração mostra |
|---|---|---|---|---|
| **Outbox transacional** (estado e mensagem na mesma transação, relay publica depois, entrega at-least-once) | "Arquitetura orientada a eventos: orquestração e coreografia" (18/10/2026); "Arquitetura na prática: acoplamento, idempotência, cache e AWS" (29/09/2026) | Gravar no banco e publicar na fila são duas escritas: sem outbox, uma queda entre as duas perde a mensagem ou publica algo que não foi gravado. Com a análise parada, a entrada aceita o evento e o trabalho espera na fila (ou na outbox, se o broker cair) | **issue-analyst-api**: `adapters/issue_events.py` (`store_issue_event`, `_insert_outbox`), `adapters/outbox_relay.py` (`OutboxRelay.publish_pending`, `FOR UPDATE SKIP LOCKED`), migração `0001_inbound_schema.sql`. **issue-triage**: `adapters/triage_store.py` (`finish_attempt`, `_enqueue_publish`), `adapters/outbox_relay.py`, migração `0001_triage_schema.sql` (`triage.publish_outbox`) | Passos 2 a 6 do [roteiro](#demonstração): com o `issue-triage` parado, o webhook responde `202`, a mensagem fica na fila `analysis` e é processada quando o serviço volta |
| **Consumidor idempotente** (deduplicação por chave, com `INSERT ... ON CONFLICT DO NOTHING`) | "Arquitetura na prática: acoplamento, idempotência, cache e AWS" (29/09/2026) | A entrega é at-least-once: o mesmo webhook pode chegar duas vezes, a mesma mensagem pode ser reentregue e o publisher pode cair depois de comentar. Sem chave por etapa, a issue recebe dois comentários | **Entrada**: `eventId` (e, em `issue.created`, a issue) em `issue_events.py` (`store_issue_event`, `_find_conflict`). **Análise**: `(event_id, attempt)` em `triage_store.py` (`open_initial`). **Publicação**: `(repository, issue_number, revision)` mais o marcador oculto do comentário, em `domain/publication.py` (`claim_and_check`, `build_marker`), `adapters/publications.py` e `adapters/github.py` (`find_comment_by_marker`) do **issue-publisher** | Passos 7 e 8 do [roteiro](#demonstração): o mesmo webhook devolve `200 duplicate` e a mesma mensagem de publicação não cria segundo comentário |

Métodos de apoio, que sustentam os dois acima:

| Apoio | Aula de referência | Onde está |
|---|---|---|
| Comunicação assíncrona por filas e serviços em processos separados, sem estado em memória | "Introdução a microsserviços: monolitos, domínios e padrões de comunicação" (14/09/2026); "Arquiteturas backend: decisões arquiteturais, stateful e stateless" (22/09/2026) | Os três serviços; fila `analysis` e fila `publish`; [`docker-compose.yml`](docker-compose.yml) |
| Portas e adaptadores (IA e GitHub atrás de interfaces, trocáveis por simulados) | "Arquitetura limpa e hexagonal na prática: TDD, AWS e deploys" (07/10/2026) | `domain/` e `adapters/` em cada serviço (`RouterLLM` e `SpecialistRunner` no `issue-triage`; `GitHubIssuesPort` no `issue-publisher`) |
| Um schema por dono, sem escrita cruzada | "Arquitetura na prática: acoplamento, idempotência, cache e AWS" (29/09/2026) | [`infra/initdb/00_roles.sql`](infra/initdb/00_roles.sql) (papéis `analyst_api`, `triage_svc`, `publisher_svc`) |

Os limites de cada método estão em [Limitações e decisões](#limitações-e-decisões).

## Como executar

Pré-requisitos: Docker com Docker Compose, Git e (para a demonstração com o GitHub real) uma conta e
repositórios de teste, uma chave da Anthropic e um túnel público para o webhook.

1. Clone os três repositórios **lado a lado** (o compose referencia os outros dois por caminho relativo):

   ```bash
   git clone https://github.com/Yngredh/issue-triage.git
   git clone https://github.com/Yngredh/issue-analyst-api.git
   git clone https://github.com/Yngredh/issue-publisher.git
   cd issue-triage
   ```

2. Crie o `.env` a partir do exemplo e preencha os três segredos (veja [Variáveis de ambiente](#variáveis-de-ambiente)):

   ```bash
   cp .env.example .env          # PowerShell: Copy-Item .env.example .env
   ```

3. Suba o ambiente completo (PostgreSQL, RabbitMQ e os três serviços):

   ```bash
   docker compose up -d --build
   docker compose ps             # os três serviços ficam "healthy"
   curl http://localhost:8081/health
   ```

4. Acompanhe os logs e, se quiser, o painel do RabbitMQ (`http://localhost:15673`, usuário e senha `guest`):

   ```bash
   docker compose logs -f issue-triage issue-publisher issue-analyst-api
   ```

5. Para parar: `docker compose down`. Use `docker compose down -v` para apagar também os dados (o
   initdb, que cria papéis, schemas, catálogo e seed, só roda em volume novo).

Sem `ANTHROPIC_API_KEY` o ambiente não sobe: o worker do `issue-triage` sai com `worker.missing_api_key`
e o `issue-analyst-api` espera o `issue-triage` ficar saudável. Detalhes de portas, ordem de subida e
initdb em [docs/servico-issue-triage.md](docs/servico-issue-triage.md#ambiente-integrado).

Para receber eventos reais do GitHub, exponha a porta `8081` com um túnel e cadastre o webhook:

```bash
cloudflared tunnel --url http://localhost:8081      # mostra https://<nome>.trycloudflare.com
```

No repositório de teste: Settings → Webhooks → Add webhook, com **Payload URL**
`https://<nome>.trycloudflare.com/webhooks/github`, **Content type** `application/json`, **Secret** igual
a `GITHUB_WEBHOOK_SECRET` e o evento **Issues**. A URL do túnel muda a cada execução.

## Variáveis de ambiente

O exemplo, sem segredos, está em [`.env.example`](.env.example) (e em `.env.example` de cada serviço).
Você preenche só três valores; o restante tem padrão ou é definido pelo compose.

| Variável | Onde obter | Usada por |
|---|---|---|
| `ANTHROPIC_API_KEY` | Console da Anthropic (Settings → API keys), com crédito pré-pago | `issue-triage` |
| `GITHUB_TOKEN` | GitHub (Settings → Developer settings → Fine-grained tokens), da conta de teste, com acesso aos repositórios de teste e permissão **Issues: Read and write** | `issue-publisher` |
| `GITHUB_WEBHOOK_SECRET` | Um texto qualquer que você inventa; o mesmo valor vai no campo Secret do webhook | `issue-analyst-api` |

Os segredos nunca vão para o repositório, para o banco nem para os logs. O `.env` está no `.gitignore`.
Variáveis de cada serviço (modelos, limites, tentativas) estão no README de cada um.

## Catálogo de domínio

O catálogo é o domínio que a ferramenta da IA consulta: repositórios aceitos, responsáveis e
orientações de triagem, mais a documentação de cada projeto. É fictício e vem do sistema de uma oficina
mecânica (ADR-0001 — Catálogo de projetos dentro do issue-triage).

| Parte | Arquivo | Conteúdo |
|---|---|---|
| Esquema | [`catalog/schema.sql`](catalog/schema.sql) | Tabelas `catalog.projects` e `catalog.triage_guidelines` |
| Seed | [`catalog/seed.sql`](catalog/seed.sql) | Repositórios aceitos, responsável e orientações (reexecutável) |
| Documentação dos projetos | [`catalog/source/`](catalog/source) | Uma pasta por repositório; é o que as ferramentas dos especialistas leem |

Repositórios aceitos, todos com responsável `@Yaane-h`:

| Repositório | Projeto |
|---|---|
| `Yaane-h/mechanical-hub-orders` | Ordens de Serviço |
| `Yaane-h/mechanical-hub-billing` | Orçamento e Pagamento |
| `Yaane-h/mechanical-hub-stock` | Estoque |
| `Yaane-h/mechanical-hub-orchestrator` | Orquestrador da saga |

`Yaane-h/mechanical-hub-auth` fica fora do catálogo de propósito: é o repositório para demonstrar o caso
`unknown_repo`, que vai para revisão humana. As orientações de triagem (por repositório e, quando for
o caso, por categoria) estão em `catalog/seed.sql`.

As ferramentas que o especialista usa para consultar o catálogo são três, controladas pela aplicação:
`list_docs`, `search_docs` e `read_doc` (cada especialista recebe só as que cabem ao papel). Elas só
enxergam a pasta do repositório da issue, e as ferramentas embutidas do SDK (Bash, arquivos, web) ficam
desligadas. Para reaplicar a seed depois de editá-la:
`psql postgresql://postgres:postgres@localhost:5433/issue_maintenance -f catalog/seed.sql`.

## Exemplos de eventos

Tudo em [`examples/`](examples), com dados fictícios.

| Arquivo | O que é |
|---|---|
| [`github-issues-opened.json`](examples/github-issues-opened.json) | Payload do GitHub (`issues.opened`) de um bug no `mechanical-hub-orders` |
| [`github-issues-opened-sem-descricao.json`](examples/github-issues-opened-sem-descricao.json) | `issues.opened` com `body` nulo: vira pedido de informações, sem IA |
| [`github-issues-edited.json`](examples/github-issues-edited.json) | `issues.edited` da mesma issue, agora com descrição: reabre a análise |
| [`github-issues-opened-repositorio-desconhecido.json`](examples/github-issues-opened-repositorio-desconhecido.json) | Repositório fora do catálogo: revisão humana (`unknown_repo`) |
| [`evento-normalizado.json`](examples/evento-normalizado.json) | Evento normalizado gravado pela entrada (equivale ao exemplo do enunciado) |
| [`resposta-webhook-202.json`](examples/resposta-webhook-202.json), [`resposta-webhook-200-duplicate.json`](examples/resposta-webhook-200-duplicate.json) | Respostas do webhook: aceito e repetido |
| [`mensagem-analysis.json`](examples/mensagem-analysis.json), [`mensagem-publish.json`](examples/mensagem-publish.json) | Mensagens das filas `analysis` e `publish` |
| [`resposta-consulta.json`](examples/resposta-consulta.json) | Resposta de `GET /issues/{owner}/{repo}/{number}` (os textos entre `<>` são escritos pela IA) |

**Mapeamento do payload do GitHub para o evento normalizado.** O enunciado aceita outro contrato desde
que o mapeamento esteja documentado. O nosso é plano, sem o objeto `data`:

| Evento normalizado | Origem no webhook do GitHub |
|---|---|
| `eventId` | cabeçalho `X-GitHub-Delivery` (obrigatório) |
| `type` | `action`: `opened` vira `issue.created`, `edited` vira `issue.edited`; outras ações são ignoradas |
| `occurredAt` | `issue.created_at` (`issue.created`) ou `issue.updated_at` (`issue.edited`) |
| `repository` | `repository.full_name` (obrigatório, formato `owner/nome`) |
| `issueNumber` | `issue.number` (obrigatório, inteiro positivo) |
| `title` | `issue.title` (obrigatório) |
| `description` | `issue.body`; nulo ou só espaços vira `""` |
| `missingDescription` | `true` quando `description` é vazia (a issue é aceita e registrada como sem informações) |

Eventos de `ping`, de outros tipos e de remetentes `Bot` recebem `200 ignored`.

**Enviar um evento de exemplo.** Com o ambiente de pé, o `issue-analyst-api` (clonado ao lado) tem um
script que assina o payload com HMAC e envia ao webhook, fazendo o papel do GitHub:

```bash
cd ../issue-analyst-api
export GITHUB_WEBHOOK_SECRET=<o mesmo valor do .env>
uv run python scripts/send_event.py --url http://localhost:8081/webhooks/github \
  --action opened --repository Yaane-h/mechanical-hub-orders --number 42 \
  --title "Erro ao abrir ordem de serviço" \
  --description "Ao enviar POST /orders para abrir uma ordem, recebo erro 500." --delivery-id evt-001
```

Ou com `curl`, usando os arquivos de [`examples/`](examples) (a assinatura é o HMAC SHA-256 do corpo):

```bash
BODY=examples/github-issues-opened.json
SIG=$(openssl dgst -sha256 -hmac "$GITHUB_WEBHOOK_SECRET" "$BODY" | sed 's/^.* //')
curl -i http://localhost:8081/webhooks/github \
  -H "Content-Type: application/json" -H "X-GitHub-Event: issues" \
  -H "X-GitHub-Delivery: evt-001" -H "X-Hub-Signature-256: sha256=$SIG" \
  --data-binary @"$BODY"
```

Para injetar uma análise direto na fila, sem o webhook (só desenvolvimento), veja
`scripts/send_analysis.py` em [docs/servico-issue-triage.md](docs/servico-issue-triage.md#scripts-e-exemplos).

## Como testar

Cada serviço tem testes próprios, que precisam do Docker ativo (PostgreSQL e RabbitMQ reais por
testcontainers):

```bash
cd issue-triage        && uv sync && uv run ruff check . && uv run pytest -q
cd ../issue-analyst-api && uv sync && uv run ruff check . && uv run pytest -q
cd ../issue-publisher   && uv sync && uv run ruff check . && uv run pytest -q
```

**O que é real e o que é simulado.**

| Camada | Testes automatizados | Demonstração |
|---|---|---|
| Provedor de IA | Simulado (portas `RouterLLM` e `SpecialistRunner` com versões falsas); nenhum teste chama a Anthropic | **Real** (Claude Haiku 5.5) |
| GitHub, entrada | Simulado: o script `send_event.py` e o `TestClient` fazem o papel do GitHub | **Real**: webhook de issue criada no repositório de teste |
| GitHub, comentário | Simulado (porta falsa em memória e respx) | **Real**: comentário na issue, com URL confirmada |
| PostgreSQL e RabbitMQ | **Reais**, em container (testcontainers) | **Reais**, no compose |
| Os três serviços juntos | Cada serviço é testado isoladamente; o `schema` dos outros vem de stubs | **Reais**, em processos separados |

Avaliação do roteador e dos especialistas com a API real (tem custo, não roda no `pytest`):
`uv run python scripts/evaluate.py --specialists` (detalhes em
[docs/servico-issue-triage.md](docs/servico-issue-triage.md#avaliação-com-a-api-real)).
Os casos vêm de [`tests/routing/fixtures/issues.yaml`](tests/routing/fixtures/issues.yaml).

## Demonstração

Roteiro de parar a análise, enviar, retomar, consultar e reenviar, com o ambiente de pé e o webhook
cadastrado. Os números entre colchetes ligam cada passo ao método da tabela acima.

1. **Estado inicial.** `docker compose ps` mostra os três serviços saudáveis.
2. **Pare a análise.** `docker compose stop issue-triage`. [outbox]
3. **Envie um evento.** Abra uma issue no repositório de teste (ou use `send_event.py`). O webhook
   responde `202 accepted`. [outbox]
4. **Veja que o trabalho foi guardado.** A fila `analysis` tem 1 mensagem pronta no painel do RabbitMQ
   (`http://localhost:15673`) e `GET /issues/{owner}/{repo}/{number}` devolve `ACCEPTED` /
   `awaiting_analysis`. [outbox]
5. **Retome.** `docker compose start issue-triage`. O log mostra `analysis.received`, `triage.routed`,
   `triage.proposal_validated`, `outbox.published` e, no publisher, `publish.received` e
   `publish.published`. [outbox]
6. **Consulte o resultado.** A mesma URL do passo 4 devolve `READY_TO_PUBLISH`, a categoria, a proposta
   e `publication.state = PUBLISHED` com a URL do comentário; o comentário está na issue.
7. **Reenvie o mesmo evento.** Na entrega do webhook no GitHub, use *Redeliver* (ou repita o
   `send_event.py` com o mesmo `--delivery-id`). A resposta é `200 duplicate`; nada novo entra nas filas.
   [idempotência]
8. **Reenvie a mensagem de publicação.** No `issue-publisher`, rode
   `uv run python scripts/publish_message.py <proposalId> <revision> --rabbitmq-url amqp://guest:guest@localhost:5673/`
   (o `proposalId` e a `revision` saem da consulta SQL abaixo). O log mostra `publish.received` e nenhum
   novo `publish.published`: a publicação já está `PUBLISHED` e a issue continua com um único
   comentário. [idempotência]

```sql
-- no PostgreSQL do compose (porta 5433): proposta e publicação da issue
SELECT p.id AS proposal_id, a.attempt AS revision, pub.state, pub.github_comment_url
FROM triage.proposals p
JOIN triage.analysis_attempts a ON a.id = p.attempt_id
LEFT JOIN publisher.publications pub ON pub.proposal_id = p.id
ORDER BY p.id DESC LIMIT 5;
```

Cenários de IA a mostrar com issues reais: duas rotas diferentes (por exemplo bug e melhoria), uma
issue de repositório fora do catálogo (`mechanical-hub-auth`), uma issue sem descrição e uma que o
roteador classifica como indeterminada (revisão por falta de informação).

## Evidências

Os cenários pedidos no nível Pleno e onde está a prova de cada um. Os arquivos (capturas, logs, links)
ficam em [`docs/evidencias/`](docs/evidencias).

| Cenário | Prova |
|---|---|
| Evento real recebido do GitHub | <PREENCHER: print da entrega do webhook (200/202) e log `webhook_response` do `issue-analyst-api`> |
| Chamada real ao provedor de IA | <PREENCHER: log `triage.routed` com a categoria e `llm_calls`, ou a saída de `evaluate.py`> |
| Ação confirmada no destino | <PREENCHER: link do comentário na issue e `publication.commentUrl` da consulta> |
| Rota 1 real (bug) | <PREENCHER: link da issue e do comentário> |
| Rota 2 real (melhoria ou dúvida) | <PREENCHER: link da issue e do comentário> |
| Caso que termina em revisão por falta de informação | <PREENCHER: consulta com `NEEDS_REVIEW` e `insufficient_content` ou `unknown_repo`> |
| Análise parada: o evento continua sendo aceito | <PREENCHER: `202` com o `issue-triage` parado e mensagem na fila `analysis`> |
| Retomada: o trabalho aceito é processado | <PREENCHER: logs depois do `docker compose start issue-triage` e a consulta do resultado> |
| Reenvio sem duplicar a ação | <PREENCHER: `200 duplicate` e a issue com um único comentário> |
| Avaliação do roteador e dos especialistas | <PREENCHER: tabela de `scripts/evaluate.py --specialists` com a API real> |
| Testes automatizados | <PREENCHER: saída de `uv run pytest -q` de cada repositório> |

## Rastreabilidade dos requisitos

Os identificadores são os do documento Requisitos; a coluna da direita aponta onde o comportamento está.

| Requisitos | Comportamento | Onde |
|---|---|---|
| RF01 | Assinatura HMAC do webhook | `issue-analyst-api`: `domain/signature.py` |
| RF02 a RF05, RF28 | Filtro de eventos, normalização, descrição vazia aceita e edição do autor | `issue-analyst-api`: `domain/events.py`; `issue-triage`: `triage_store.open_author_edit` |
| RF06 | Deduplicação na entrada | `issue-analyst-api`: `adapters/issue_events.py` |
| RF09, RNF04 | Análise idempotente e retomável | `issue-triage`: `analysis.py`, `adapters/triage_store.py` |
| RF10 | Repositório fora do catálogo vai para revisão humana | `issue-triage`: `analysis.py` (`triage.unknown_repo`) |
| RF11, RF20 | Descrição vazia gera pedido de informações, sem IA | `issue-triage`: `domain/templates.py` (`render_missing_info`) |
| RF13 a RF15, RNF10, RNF11 | Especialistas com ferramentas controladas e limites | `issue-triage`: `domain/specialist.py`, `adapters/catalog_tools.py`, `adapters/claude_specialist.py` |
| RF16, RF17, RF18, RF19 | Resumo e próximo passo, validador, revisão humana e template do comentário | `issue-triage`: `domain/validator.py`, `domain/templates.py` |
| RF21 | Proposta e mensagem de publicação na mesma transação | `issue-triage`: `adapters/triage_store.py`, `adapters/outbox_relay.py` |
| RF22 a RF25 | Publicação no GitHub, anti-duplicação e novas tentativas | `issue-publisher`: `domain/publishing.py`, `domain/publication.py`, `adapters/github.py` |
| RF26 | Catálogo com documentação dos projetos | `catalog/seed.sql`, `catalog/source/` |
| RF27 | Consulta por repositório e número | `issue-analyst-api`: `api/issues.py`, migração `0002_issue_presentation.sql` |
| RF29, RF30 | Resolução manual (consumo no `issue-triage`; sem endpoint, veja as limitações) | `issue-triage`: `adapters/triage_store.py` (`open_manual`, `close_case`) |
| RNF06, RNF07 | Um schema por dono, sem escrita cruzada | [`infra/initdb/00_roles.sql`](infra/initdb/00_roles.sql) |
| RNF14 | Logs estruturados com `event_id`, `module`, `func_name` e `message` | `log_messages.py` e `logging.py` de cada serviço |
| RNF16, RNF17 | IA e GitHub simulados nos testes | `tests/fakes.py` e portas de cada serviço |
| RNF18 | Parar só a análise na demonstração | [`docker-compose.yml`](docker-compose.yml) |

## Limitações e decisões

- **Resolução manual da revisão.** O `issue-triage` já trata as resoluções `enrich`, `reprocess` e
  `close` vindas da fila `analysis` (testadas), mas o endpoint HTTP que as produz fica fora do escopo
  do `issue-analyst-api` nesta entrega. O revisor consulta o caso pela API; resolver exige inserir a
  mensagem à mão.
- **At-least-once.** A entrega pode se repetir; a duplicação no destino é evitada pelas chaves de cada
  etapa e pelo marcador oculto do comentário. Se o publisher cair depois de comentar e antes de gravar,
  a reentrega procura o marcador e adota o comentário existente.
- **Relay dentro do serviço.** O relay da outbox roda no processo produtor: se o serviço cai, a mensagem
  espera na outbox e sai quando ele volta. Com mais de uma instância, o `SKIP LOCKED` evita publicar em
  dobro.
- **Fila `publish` sem DLQ.** O limite de 3 tentativas fica no banco; mensagem inválida é descartada com log.
- **Catálogo sem CRUD.** Seed SQL e documentação em arquivos; atualizar exige reaplicar a seed. O
  catálogo pertence a este repositório embora o `issue-publisher` também o leia.
- **Banco único no compose.** Um PostgreSQL com schemas e papéis por dono, para uso local; em produção
  seriam bancos separados.
- **Consulta sem autenticação.** `GET /issues/...` não exige credencial; é um serviço de demonstração.
- **Modelo único.** Claude Haiku 5.5 em todos os papéis; a avaliação com a API real diz se basta
  (<PREENCHER: resultado da avaliação>). O modelo é variável de ambiente.
- **Túnel.** A URL do túnel de desenvolvimento muda a cada execução; o webhook precisa ser atualizado.

## Uso de IA

<PREENCHER: declare, com as suas palavras, como usou IA. Sugestão de pontos, a confirmar: ferramentas
usadas (Claude, Claude Code, outras); o que a IA produziu (rascunhos dos documentos de requisitos e de
design, planos de desenvolvimento por etapa, código e testes executados por agentes); o que você
decidiu e validou (decisões técnicas numeradas nos planos, escolha dos métodos, revisão do código,
execução dos testes e da demonstração); e o que você consegue explicar e alterar ao vivo.>

Em tempo de execução, a IA é o Claude Haiku 5.5 (roteador e especialistas), sempre atrás de ferramentas
controladas pela aplicação e de um validador. O texto da issue é dado de entrada: não concede permissões
nem muda as regras do harness.

## Documentação

- Detalhes do `issue-triage`: [docs/servico-issue-triage.md](docs/servico-issue-triage.md).
- Plano de desenvolvimento: [docs/plano-desenvolvimento-issue-triage.md](docs/plano-desenvolvimento-issue-triage.md).
- README e plano de cada serviço: [issue-analyst-api](https://github.com/Yngredh/issue-analyst-api) e [issue-publisher](https://github.com/Yngredh/issue-publisher).
- Documentos de design do projeto (páginas do Claude): Requisitos, High Level Design, Modelagem de
  dados, Ingestão Spec, Revisão Humana Spec, Issue Publisher Spec, Spec D: Roteador de issues, Issue
  Triage Spec, ADR-0001 — Catálogo de projetos dentro do issue-triage e Casos de teste. O índice com os
  links é [Índice — Documentação do Assistente de manutenção de software](https://claude.ai/artifact/HRUES7zjJrXmXEbK2GDKic).
  <PREENCHER: confirme que o índice e os documentos estão compartilhados com a mentora, ou exporte-os para `docs/`.>
