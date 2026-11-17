# Spec de Desenvolvimento — `mechanical-hub-orchestrator`

Guia prático para construir o serviço. Regras da saga e operações detalhadas ficam nos documentos citados; aqui está **o que construir e em que ordem**.

- Stack: Kotlin, Java 25, Spring Boot 4.1.1 (Maven), Spring Data MongoDB, MongoDB 7 (replica set de 1 nó), Amazon SQS FIFO (Spring Cloud AWS), ShedLock (provider Mongo), mensagens em **JSON**.
- Código de referência: não há equivalente no monolito `mechanical-hub`; seguir a mesma organização em camadas (Clean Architecture) usada em `mechanical-hub-orders`.
- Padrões de desenvolvimento: seguir [`development-standards.md`](../engineering/development-standards.md) (Kotlin, Clean Architecture e testes) em todas as etapas.
- Referências: [`saga_transaction_status.md`](../design/saga_transaction_status.md), [`td_finished_os.md`](../design/distributedTransaction/td_finished_os.md) §0.2–0.4, [`td_stock_pending.md`](../design/distributedTransaction/td_stock_pending.md), [`td_budget_refused.md`](../design/distributedTransaction/td_budget_refused.md), [`td_payment_failed.md`](../design/distributedTransaction/td_payment_failed.md), [`data_model.md`](../design/data_model.md) §5, [`failure_handling.md`](../design/failure_handling.md), [`infrastructure.md`](../infrastructure/infrastructure.md), [ADR-0004](../adr/ADR-0004-banco-de-dados-por-microsservico.md), [ADR-0007](../adr/ADR-0007-outbox-e-inbox.md), [RFC-0005](../rfc/RFC-0005-saga-orquestrada.md).

## 1. Escopo

**Faz:** cria e mantém o estado de uma saga por ordem, consome os eventos de `orders`, `stock` e `billing`, aplica a guarda de passo, publica os comandos de cada passo (via outbox) e conduz as compensações (recusa do orçamento e pagamento não confirmado). Guarda a trilha completa de cada saga (`transaction_history`). Hospeda o BDD do fluxo completo.

**Não faz:** regra de negócio da ordem, do estoque ou do pagamento (cada serviço valida o próprio passo), API para mecânico ou cliente, notificações, chamadas REST aos outros serviços.

**Fora do escopo por enquanto (foco no fluxo):**
- Prazo máximo para `AWAITING_STOCK` e compensação automática de sagas paradas ([`future_requirements.md`](../business/future_requirements.md) FR-01). Sagas paradas são só localizadas pelo índice `status + currentStep + updatedAt` e pelo painel.
- Ajuste de reparos ou de orçamento depois do início da saga (FR-02 e seguintes).
- Endpoint público. Só existe a consulta interna opcional da seção 2.

## 2. Endpoints

O serviço é dirigido por mensagens. Só expõe endpoints operacionais.

| Método e rota | Efeito | Observação |
|---|---|---|
| `GET /actuator/health`, `GET /actuator/prometheus` | Probes do Kubernetes e métricas | — |
| `GET /transaction/{orderId}` *(opcional)* | Documento da transação (`transaction_register`) + `transaction_history` ordenada por `seq` | Para depuração e para o vídeo. Se for exposto no API Gateway, proteger com perfil de administrador |

## 3. Mensagens

Filas: consome `mechanical-hub-orchestrator-events.fifo`; publica em `mechanical-hub-orders-commands.fifo`, `mechanical-hub-stock-commands.fifo` e `mechanical-hub-billing-commands.fifo`. `MessageGroupId = orderId`, `MessageDeduplicationId = eventId`. Envelope JSON: `eventId`, `transactionId`, `orderId`, `type`, `version`, `timestamp` + payload; trace nos *message attributes*.

### Consome (deduplicado por `messageKey = eventId` na `transaction_history`)

Cada linha é uma transação multi-documento: deduplicação → guarda de passo + atualização (`findOneAndUpdate` filtrando `currentStep`, com `$inc: { seq: n }`) → `transaction_history` (`APPLIED`) → `outbox_events` → `commitTransaction` → apaga a mensagem.

| Evento | De | Passo esperado | Atualiza na saga | Publica | Ref. |
|---|---|---|---|---|---|
| `diagnosisCompleted` | orders | — (cria a saga) | `insertOne`: `status = STARTED`, `currentStep = RESERVE_STOCK`, `seq = 1`, `repairs`. `E11000` em `orderId` → descarta | `reserveStock` | td_finished_os 6 |
| `stockPending` | stock | `RESERVE_STOCK` | `currentStep = AWAITING_STOCK` | `markStockPending` | td_stock_pending P2 |
| `stockReserved` | stock | `RESERVE_STOCK` | `reservedMaterials`, `currentStep = GENERATE_BUDGET` | `generateBudget` | td_finished_os 8 |
| `stockReserved` | stock | `AWAITING_STOCK` | `reservedMaterials`, `currentStep = GENERATE_BUDGET` | `clearStockPending`, depois `generateBudget` | td_stock_pending P5 |
| `budgetGenerated` | billing | `GENERATE_BUDGET` | `budget { budgetId, orderCost }`, `currentStep = WAITING_APPROVAL` | `sendApproval` | td_finished_os 10 |
| `orderApproved` | orders | `WAITING_APPROVAL` | `currentStep = AWAITING_PAYMENT` | `generatePaymentLink` | td_finished_os 13 |
| `orderRejected` | orders | `WAITING_APPROVAL` | `status = COMPENSATING`, `currentStep = RESTORE_STOCK` | `restoreStock` | td_budget_refused R2 |
| `paymentLinkGenerated` | billing | `AWAITING_PAYMENT` e `payment.link = null` | `payment.link`, `payment.expiresAt` | `sendPaymentLink` | td_finished_os 15 |
| `paymentConfirmed` | billing | `AWAITING_PAYMENT` | `payment.paymentId`, `currentStep = AWAITING_EXECUTION` | `releaseOrder` | td_finished_os 18 |
| `paymentFailed` | billing | `AWAITING_PAYMENT` | `status = COMPENSATING`, `currentStep = CANCEL_ORDER` | `cancelOrder` | td_payment_failed F4 |
| `orderCanceled` | orders | `CANCEL_ORDER` | `currentStep = RESTORE_STOCK` | `restoreStock` | td_payment_failed F6 |
| `orderFinished` | orders | `AWAITING_EXECUTION` | `currentStep = CONSUME_STOCK` | `consumeStock` | td_finished_os 21 |
| `stockConsumed` | stock | `CONSUME_STOCK` | `status = COMPLETED`, `currentStep = null`, `finishedAt` | — | td_finished_os 23 |
| `stockRestored` | stock | `RESTORE_STOCK` | `status = COMPENSATED`, `currentStep = null`, `finishedAt` | — | td_budget_refused R4, td_payment_failed F8 |

- Passo diferente do esperado (`findOneAndUpdate` retorna `null`) → aborta a transação, grava só `transaction_history` com `outcome = DISCARDED_STEP_GUARD` e apaga a mensagem. Cobre duplicatas, mensagens fora de ordem e o `stockReserved` de outras ordens gerado pela redistribuição do `restoreStock`.
- `stockReserved` tem dois passos de origem com quantidades diferentes de comandos (`n = 1` ou `2`): tentar primeiro a guarda `AWAITING_STOCK` e, se retornar `null`, a guarda `RESERVE_STOCK`.

### Publica (sempre via outbox, na mesma transação da atualização da saga)

Todo comando leva `transactionId`, `orderId` e `idempotencyKey = {transactionId}:{STEP}`.

| Comando | Para | `STEP` | Payload adicional |
|---|---|---|---|
| `reserveStock` | stock | `RESERVE_STOCK` | `repairs[{repairId}]` |
| `generateBudget` | billing | `GENERATE_BUDGET` | `repairs`, `reservedMaterials[{materialId, quantity}]` |
| `generatePaymentLink` | billing | `GENERATE_PAYMENT_LINK` | `budgetId`, `orderCost` |
| `consumeStock` | stock | `CONSUME_STOCK` | `reservedMaterials` (lido da saga) |
| `restoreStock` | stock | `RESTORE_STOCK` | `reservedMaterials` (lido da saga) |
| `sendApproval` | orders | `SEND_APPROVAL` | `budgetId`, `orderCost` |
| `markStockPending` | orders | `MARK_STOCK_PENDING` | — |
| `clearStockPending` | orders | `CLEAR_STOCK_PENDING` | — |
| `sendPaymentLink` | orders | `SEND_PAYMENT_LINK` | `paymentLink` |
| `releaseOrder` | orders | `RELEASE_ORDER` | — |
| `cancelOrder` | orders | `CANCEL_ORDER` | `reason = PAYMENT_NOT_CONFIRMED` |

## 4. Banco de dados

MongoDB em StatefulSet no EKS, provisionado pelo `mechanical-hub-database`. Índices criados pelo próprio serviço na subida. Não há seeds: todo dado nasce das mensagens.

- `transaction_register`: um documento por saga (`_id = transactionId`, `orderId`, `status`, `currentStep`, `seq`, `repairs`, `reservedMaterials`, `budget`, `payment`, `createdAt`, `updatedAt`, `finishedAt`).
- `transaction_history`: append-only (`transactionId`, `orderId`, `seq`, `messageType`, `messageKey`, `stepBefore`, `stepAfter`, `outcome`, `receivedAt`). Faz o papel da `inbox_events`.
- `outbox_events`: `_id = eventId`, `orderId`, `seq`, `eventType`, `payload`, `status` (`PENDING`, `SENT`), `attempts`, `lastError`, `createdAt`, `sentAt`.
- `shedLock`: lease de liderança do relay.
- Índices: `transaction_register { orderId }` único e `{ status, currentStep, updatedAt }`; `transaction_history { messageKey }` único, `{ orderId, seq }` e `{ messageType, receivedAt }`; `outbox_events { status, createdAt }` e `{ orderId, seq }` único.
- Retenção: as três coleções são permanentes, sem job de limpeza.
- Pré-requisitos (ADR-0004): replica set em todos os ambientes, inclusive testes; `writeConcern: majority`, `readConcern: majority` (transações em `snapshot`), `readPreference: primary`; `MongoTransactionManager` declarado no Spring.

## 5. Atividades

### Etapa 0 — Configuração do projeto
- [x] Gerar o projeto pelo Spring Initializr na raiz do repositório (Kotlin, Java 25, Spring Boot 4.1.1, Maven):
  ```bash
  curl -sS https://start.spring.io/starter.tgz \
    -d type=maven-project -d language=kotlin \
    -d bootVersion=4.1.1.RELEASE -d javaVersion=25 \
    -d groupId=com.fiap -d artifactId=mechanical-hub-orchestrator -d name=mechanical-hub-orchestrator \
    -d packageName=com.fiap.mechanicalhub.orchestrator \
    -d dependencies=web,validation,actuator,prometheus,data-mongodb,docker-compose,testcontainers \
    | tar -xzf -
  ```
- [x] Conferir que `pom.xml`, `mvnw` e `src/` ficaram na raiz, ao lado do `README.md` (sem subpasta extra), e remover o `HELP.md` gerado.
- [x] Adicionar ao `pom.xml` o que o Initializr não oferece:
  - Spring Cloud AWS SQS: BOM `io.awspring.cloud:spring-cloud-aws-dependencies` + `spring-cloud-aws-starter-sqs`, em versão compatível com o Spring Boot 4.1 (conferir a matriz de compatibilidade do projeto antes de fixar).
  - Testcontainers LocalStack (`org.testcontainers:localstack`, escopo `test`).
  - `net.logstash.logback:logstash-logback-encoder` (logs JSON, igual ao monolito).
  - ShedLock: `net.javacrumbs.shedlock:shedlock-spring` + `shedlock-provider-mongo`.
  - Cucumber para o BDD (`io.cucumber:cucumber-java`, `cucumber-spring`, `cucumber-junit-platform-engine` + `org.junit.platform:junit-platform-suite`, escopo `test`).
- [x] Plugins `jacoco-maven-plugin` e `sonar-maven-plugin` com a mesma configuração do monolito.
- [x] O monolito é em Java e serve só de referência de regras; o código novo é Kotlin idiomático (data classes, null-safety, sem Lombok).
- [x] `./mvnw -q verify` passando com o projeto vazio (Docker rodando, por causa do Testcontainers).

### Etapa 1 — Base do projeto
- [x] Repositório criado, `main` protegida (PR + checagens obrigatórias).
  - Repositório já existe; falta transferi-lo para a organização `mechanical-hub-fiap` (o `origin` ainda aponta para a conta pessoal) e configurar a proteção da `main` exigindo o job *Build, testes e Sonar*. É configuração no GitHub, não código.
- [x] Projeto Spring Boot com camadas `domain/`, `application/`, `infrastructure/`.
- [x] `docker-compose` com MongoDB 7 em replica set (`--replSet rs0` + `rs.initiate()` no init) e LocalStack (4 filas FIFO + DLQs criadas no init).
- [x] Configuração por variáveis de ambiente (URI do Mongo, URLs das 4 filas); sem chave AWS (usa `LabRole`).

### Etapa 2 — Banco
- [x] `MongoTransactionManager`, write/read concern e `readPreference` da seção 4.
- [x] Criação explícita dos índices na subida (falha a aplicação se não conseguir criar).
- [x] Coleção de lock do ShedLock.

### Etapa 3 — Domínio
- [x] `SagaStatusEnum` (`STARTED`, `COMPLETED`, `COMPENSATING`, `COMPENSATED`) e `SagaStepEnum` (`RESERVE_STOCK` … `RESTORE_STOCK`).
- [x] Máquina de estados da saga como tabela única no domínio (evento + passo esperado → próximo passo, status, comandos a emitir), espelhando a seção 3. Sem dependência do Mongo, testável isoladamente.
- [x] Agregado `Saga` com os dados acumulados por passo (`repairs`, `reservedMaterials`, `budget`, `payment`) e o cálculo de `seq` dos comandos emitidos (`seq − n + 1 … seq`).
- [x] DTOs dos contratos JSON (eventos consumidos e comandos publicados) com campo `version`, cópia local conforme `infrastructure.md` §6.

### Etapa 4 — Consumidor de eventos
- [x] `@SqsListener` em `mechanical-hub-orchestrator-events.fifo` (long polling 20 s, visibility timeout 30 s); apaga a mensagem só após o `commitTransaction`.
- [x] Dispatcher por `type` → handler; tipo desconhecido ou payload inválido → erro permanente (DLQ).
- [x] Pipeline transacional da seção 3: deduplicação por `messageKey` (inclusive `E11000` no insert do histórico = duplicata), guarda de passo, histórico, outbox.
- [x] Descarte pela guarda de passo grava `DISCARDED_STEP_GUARD` e confirma a mensagem (não é erro, log `INFO`).
- [x] Retry bloqueante: erro transitório (`TransientTransactionError`, `WriteConflict`, `NotWritablePrimary`, `UnknownTransactionCommitResult`, timeout) não apaga a mensagem e ajusta o visibility timeout pela `ApproximateReceiveCount` (1 s → 8 s); erro permanente vai direto à DLQ; DLQ nativa com `maxReceiveCount = 6`.

### Etapa 5 — Handlers
- [x] Caminho feliz: `diagnosisCompleted`, `stockReserved` (de `RESERVE_STOCK`), `budgetGenerated`, `orderApproved`, `paymentLinkGenerated`, `paymentConfirmed`, `orderFinished`, `stockConsumed`.
- [x] Pendência de estoque: `stockPending`, `stockReserved` (de `AWAITING_STOCK`, emitindo `clearStockPending` antes de `generateBudget`).
- [x] Compensações: `orderRejected`, `paymentFailed`, `orderCanceled`, `stockRestored`.

### Etapa 6 — Outbox e relay
- [x] Gravação no `outbox_events` na transação do handler, com o `seq` reservado na saga.
- [x] Relay: a cada 1 s, lote de 100 por `createdAt`; por `orderId`, publica em ordem crescente de `seq` e não publica `seq` n+1 enquanto o n estiver `PENDING`.
- [x] Liderança com ShedLock (lease); só a instância líder publica.
- [x] Roteamento `eventType` → fila de destino; envio com `SqsTemplate` (`MessageGroupId = orderId`, `MessageDeduplicationId = eventId`); marca `SENT` + `sentAt`. Erro interrompe o lote e grava `attempts` + `lastError`.

### Etapa 7 — Consulta operacional (opcional)
- [x] `GET /transaction/{orderId}` com a transação e o histórico, para depuração e demonstração.

### Etapa 8 — Observabilidade
- [x] Agente OpenTelemetry; trace extraído dos *message attributes* no consumo e propagado nos comandos publicados pelo relay.
- [x] `/actuator/prometheus` + métricas: `outbox_oldest_pending_age_seconds`, `saga_started_total`, `saga_finished_total{status}`, `saga_messages_total{type, outcome}` e gauge de sagas ativas por `currentStep`.
- [x] Logs JSON com `orderId`, `transactionId`, `eventId`, `currentStep`.
- [x] Dashboard da saga no Grafana (sagas por passo, compensações, descartes, outbox pendente, profundidade das filas e DLQs via CloudWatch) e alerta de DLQ > 0.

### Etapa 9 — Testes
- [x] Unitários: máquina de estados (cada evento × cada passo: aplica ou descarta), cálculo de `seq`, montagem dos comandos.
- [x] Integração (Testcontainers MongoDB replica set + LocalStack): atomicidade saga + histórico + outbox (falha no meio não grava nada), deduplicação, guarda de passo, `diagnosisCompleted` repetido (`E11000`), ordem do relay por `orderId`, liderança com 2 instâncias.
- [x] BDD (Cucumber) do fluxo completo, simulando `orders`, `stock` e `billing` pelas mensagens que publicariam: caminho feliz, pendência de estoque, orçamento recusado, pagamento não confirmado, pagamento confirmado pelo job, duplicatas e mensagens fora de ordem. Roda no CI de todo PR.
- [x] Cobertura ≥ 80% (JaCoCo, `check` no `verify`).
- [x] Quality gate do SonarQube Cloud no PR (`sonar:sonar` com `sonar.qualitygate.wait=true` no job *Build, testes e Sonar* do `deploy.yml`).
  - Pendente fora do código: projeto `mechanical-hub-orchestrator` no SonarQube Cloud, `SONAR_TOKEN`/`SONAR_ORGANIZATION` na organização e o job como checagem obrigatória da `main`.

### Etapa 10 — Deploy
- [x] Dockerfile multi-stage (`Dockerfile` + `.dockerignore`, JRE 25, usuário sem privilégios, agente OpenTelemetry).
- [x] Manifestos: Deployment, Service, ConfigMap, Secret (URI do Mongo), HPA (1–2), requests/limits, probes no actuator (`k8s/`; Service NodePort 30083, readiness inclui o MongoDB).
- [x] Pipeline: build → testes (unitários, integração, BDD) → Sonar → ECR → deploy EKS → smoke test (health + conexão com Mongo e SQS) (`.github/workflows/deploy.yml` com `workflow_call`, `ci/remote-state`, `ci/smoke-test.sh` com sonda SQS; job `orchestrator` no `deploy-all.yml` do `mechanical-hub-infra`).

### Etapa 11 — Integração e entrega
- [ ] Fluxo real com `orders`, `stock` e `billing` no EKS: caminho feliz, pendência de estoque, recusa, pagamento não confirmado.
  - Depende do ambiente no Lab e do `stock` com pipeline na `main` (ainda fora do `deploy-all`). Roteiro: rodar o *Deploy completo* e conduzir cada cenário pelas rotas do `orders`, acompanhando por `GET /transaction/{orderId}` e pelo painel da saga.
- [x] README (responsabilidade, papel na saga, máquina de estados, como rodar e como reprocessar a DLQ), evidência de cobertura e relatório do BDD.

## 6. Dependências externas

| Precisa de | De quem | Para |
|---|---|---|
| Filas SQS + DLQs (URLs nos outputs) | `mechanical-hub-infra` | Etapas 4 e 6 na AWS |
| Driver EBS CSI validado (ou armazenamento efêmero) | `mechanical-hub-infra` | Etapa 2 na AWS |
| MongoDB StatefulSet (replica set) e usuário do serviço | `mechanical-hub-database` | Etapa 2 na AWS |
| ECR | `mechanical-hub-infra` | Etapa 10 |
| Contratos das mensagens combinados | `orders`, `stock`, `billing` | Etapas 3–5 |
| Rota no API Gateway (só se a Etapa 7 for exposta) | `mechanical-hub-auth` | Etapa 7 |
| Serviços de domínio no ar | `orders`, `stock`, `billing` | Etapa 11 |
