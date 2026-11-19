# Spec de Desenvolvimento — `mechanical-hub-orders`

Guia prático para construir o serviço. Regras de negócio e algoritmos detalhados ficam nos documentos citados; aqui está **o que construir e em que ordem**.

- Stack: Kotlin, Java 25, Spring Boot 4.1.1 (Maven), PostgreSQL 16 (Flyway), Amazon SQS FIFO (Spring Cloud AWS), mensagens em **JSON**.
- Código de referência: monolito `mechanical-hub` (Clean Architecture, `ExternalController`, adapters de e-mail/WhatsApp, `GatewayAuthenticationFilter`, estratégias em `domain/strategies/order_transition`).
- Padrões de desenvolvimento: seguir [`development-standards.md`](../engineering/development-standards.md) (Kotlin, Clean Architecture e testes) em todas as etapas.
- Referências: [`order_status.md`](../business/order_status.md), [`td_finished_os.md`](../design/distributedTransaction/td_finished_os.md), [`td_stock_pending.md`](../design/distributedTransaction/td_stock_pending.md), [`td_budget_refused.md`](../design/distributedTransaction/td_budget_refused.md), [`td_payment_failed.md`](../design/distributedTransaction/td_payment_failed.md), [`data_model.md`](../design/data_model.md) §2 e §6, [`failure_handling.md`](../design/failure_handling.md), [`infrastructure.md`](../infrastructure/infrastructure.md).

## 1. Escopo

**Faz:** ordem de serviço e seu status, reparos da ordem (`order_tasks`), dados de clientes, veículos e funcionários, catálogo de reparos (somente leitura, sem preço), API para mecânico e cliente, notificações, início da saga e execução dos comandos do orquestrador.

**Não faz:** estoque e materiais (`stock`), preços e pagamento (`billing`), histórico da saga (`orchestrator`), cadastro de catálogo, relatórios.

**Fora do escopo por enquanto (foco no fluxo):**
- Endpoints de cadastro de clientes, veículos e funcionários: esses dados vêm das seeds (seção 4).
- Status `RECEIVED` e a transição `RECEIVED → IN_DIAGNOSIS`: o status continua no enum e em [`order_status.md`](../business/order_status.md), mas não é implementado agora. A ordem nasce em `IN_DIAGNOSIS`.

## 2. Endpoints

### Mecânico / administrador (protegidos pelo API Gateway; identidade via `x-user-id`, `x-user-role`, `x-user-name`)

| Método e rota | Body | Efeito | Ref. |
|---|---|---|---|
| `POST /orders` | `{ customerId, vehicleId, serviceIds[], requestDescription }` (formato do `/open` do monolito) | Abre a ordem já em `IN_DIAGNOSIS`, vinculada a cliente e veículo existentes e com os reparos informados; valida que o veículo pertence ao cliente e que cada reparo existe e está ativo. Não cria cliente nem veículo | td_finished_os 1–3 |
| `PATCH /orders/{orderId}/repairs` | `{ repairs: [{ repairId }] }` | Adiciona reparos; só em `IN_DIAGNOSIS` | td_finished_os 3 |
| `PATCH /orders/{orderId}/status` | `{ status }` | `PREPARING`, `IN_PROGRESS`, `FINISHED`, `DELIVERED` → método de domínio | td_finished_os §0.5 |
| `POST /orders/status` | `{ orderId, repairId, status }` | Status do reparo `PENDING → STARTED → FINISHED`; só com a ordem em `IN_PROGRESS`; não movimenta estoque | td_finished_os 19 |
| `GET /orders/{orderId}`, `GET /orders?status=&customerId=&responsibleUserId=` | — | Consulta (inclui `has_stock_pending`) | — |
| `GET /services` | — | Catálogo de reparos | — |
| `POST /orders/{orderId}/approval-token` | — | Só `ADMINISTRATOR`. Gera um token de aprovação novo, invalida o anterior (só o hash é salvo) e devolve `approvalToken` e `approvalLink` com `Cache-Control: no-store`; só em `WAITING_APPROVAL` (senão `409`). Não publica evento nem notifica o cliente. Permite simular a aprovação do cliente sem logar o token ([logging-standard](../../engineering/logging-standard.md)). Sujeita à flag `APPROVAL_TOKEN_REISSUE_ENABLED` (abaixo) | — |

### Cliente (públicos, prefixo `/mechanical-hub`, header `X-Approval-Token`)

| Método e rota | Efeito | Ref. |
|---|---|---|
| `GET /mechanical-hub/service-orders/{orderNumber}` | Resumo, status e orçamento | td_finished_os 11 |
| `POST /mechanical-hub/service-orders/{orderId}/approve` | `approve()` + outbox `orderApproved` | td_finished_os 12 |
| `POST /mechanical-hub/service-orders/{orderId}/reject` | `reject()` + outbox `orderRejected` | td_budget_refused R1 |

Token ausente/inválido → `404`. Transição inválida ou conflito de `version` → `409`. Falha transitória de banco → `503`.

**Flag `APPROVAL_TOKEN_REISSUE_ENABLED`** (padrão `true`): controla a disponibilidade de `POST /orders/{orderId}/approval-token`. Com `false` o controller não é criado e a rota não existe (`404` para o administrador, `403` para os demais). Valor vem da variável de repositório do GitHub de mesmo nome, lida no passo "Apply manifests" do `deploy.yml` (sem a variável, `true`), passa pelo `envsubst` do `configmap.yaml` e chega ao pod; trocar o valor exige novo deploy. Para desligar depois da demonstração: criar a variável com `false` e rodar o deploy.

## 3. Mensagens

Filas: publica em `mechanical-hub-orchestrator-events.fifo`; consome `mechanical-hub-orders-commands.fifo`. `MessageGroupId = orderId`, `MessageDeduplicationId = eventId`. Envelope JSON: `eventId`, `transactionId`, `orderId`, `type`, `version`, `timestamp` + payload; trace nos *message attributes*.

### Publica (sempre via outbox, na mesma transação da alteração)

| Evento | Quando | Payload |
|---|---|---|
| `diagnosisCompleted` | `PATCH status → PREPARING` | `orderId`, `repairs[{repairId}]` |
| `orderApproved` | cliente aprova | `transactionId`, `orderId` |
| `orderRejected` | cliente recusa | `transactionId`, `orderId` |
| `orderFinished` | `PATCH status → FINISHED` | `transactionId`, `orderId` |
| `orderCanceled` | consumo de `cancelOrder` | `transactionId`, `orderId` |

### Consome (idempotente por `idempotencyKey = {transactionId}:{STEP}` em `inbox_events`)

| Comando | Efeito | Após o `COMMIT` | Ref. |
|---|---|---|---|
| `sendApproval` | `requestApproval()` → `WAITING_APPROVAL`; grava `budget`, `saga_transaction_id`, `approval_token_hash` (token novo só no 1º processamento) | Notifica cliente com orçamento e link de decisão | td_finished_os 11 |
| `markStockPending` | `has_stock_pending = true`, grava `saga_transaction_id` | — | td_stock_pending P3 |
| `clearStockPending` | `has_stock_pending = false` | — | td_stock_pending P6 |
| `sendPaymentLink` | `awaitPayment()` → `AWAITING_PAYMENT` | Notifica cliente com link de pagamento | td_finished_os 15 |
| `releaseOrder` | `releaseForExecution()` → `READY_FOR_EXECUTION` | Notifica mecânico | td_finished_os 18 |
| `cancelOrder` | `cancel()` → `CANCELED` + outbox `orderCanceled` | — | td_payment_failed F5 |

## 4. Banco de dados

Migrations Flyway novas (V1 em diante) no próprio repositório; RDS existente da Fase 3 vira o banco do `orders`.

- Domínio: `profiles`, `users`, `customers`, `vehicles`, `services` (sem preço), `service_orders` (+ `saga_transaction_id`, `approval_token_hash`, `version`, `has_stock_pending`), `order_tasks`.
- Controle: `outbox_events`, `inbox_events`.
- `users`/`profiles` com as colunas lidas pela Lambda `authenticate` (`document_number` etc.) e `GRANT SELECT` para a role `mechanical_hub_auth`.
- Seeds: catálogo de reparos (`services`) exatamente conforme [`seed_catalog.md`](../design/seed_catalog.md) (mesmos `repairId` de `stock` e `billing`); perfis, funcionários, clientes (com e-mail) e veículos a partir das seeds do monolito, mantendo os UUIDs (seção 4 do catálogo). Sem endpoints de cadastro, as seeds são a única origem desses dados.

## 5. Atividades

### Etapa 0 — Configuração do projeto
- [x] Gerar o projeto pelo Spring Initializr na raiz do repositório (Kotlin, Java 25, Spring Boot 4.1.1, Maven):
  ```bash
  curl -sS https://start.spring.io/starter.tgz \
    -d type=maven-project -d language=kotlin \
    -d bootVersion=4.1.1 -d javaVersion=25 \
    -d groupId=com.fiap -d artifactId=mechanical-hub-orders -d name=mechanical-hub-orders \
    -d packageName=com.fiap.mechanicalhub.orders \
    -d dependencies=web,security,validation,actuator,prometheus,data-jpa,postgresql,flyway,docker-compose,testcontainers,springdoc-openapi \
    | tar -xzf -
  ```
- [x] Conferir que `pom.xml`, `mvnw` e `src/` ficaram na raiz, ao lado do `README.md` (sem subpasta extra), e remover o `HELP.md` gerado.
- [x] Adicionar ao `pom.xml` o que o Initializr não oferece:
  - Spring Cloud AWS SQS: BOM `io.awspring.cloud:spring-cloud-aws-dependencies` + `spring-cloud-aws-starter-sqs`, em versão compatível com o Spring Boot 4.1 (conferir a matriz de compatibilidade do projeto antes de fixar).
  - Testcontainers LocalStack (`org.testcontainers:localstack`, escopo `test`).
  - `net.logstash.logback:logstash-logback-encoder` (logs JSON, igual ao monolito).
- [x] Plugins `jacoco-maven-plugin` e `sonar-maven-plugin` com a mesma configuração do monolito.
- [x] O monolito é em Java e serve só de referência de regras; o código novo é Kotlin idiomático (data classes, null-safety, sem Lombok).
- [x] `./mvnw -q verify` passando com o projeto vazio (Docker rodando, por causa do Testcontainers).

### Etapa 1 — Base do projeto
- [x] Repositório criado, `main` protegida (PR + checagens obrigatórias).
- [x] Projeto Spring Boot com camadas `domain/`, `application/`, `infrastructure/`.
- [x] `docker-compose` com PostgreSQL 16 e LocalStack (filas FIFO + DLQs criadas no init).
- [x] Configuração por variáveis de ambiente (banco, URLs das filas); sem chave AWS (usa `LabRole`).

### Etapa 2 — Banco
- [x] Migrations de domínio, controle e seeds (seção 4).

### Etapa 3 — Domínio
- [x] `OrderStatusEnum` (12 status, `displayName` em português; `RECEIVED` existe mas não é usado) e `TaskStatusEnum`.
- [x] Criação da ordem (`ServiceOrder.open(...)`) direto em `IN_DIAGNOSIS`, com os reparos em `PENDING`.
- [x] Agregado `ServiceOrder` com os métodos de transição de [`order_status.md`](../business/order_status.md), cada um validando o status de origem; inválida → exceção mapeada para `409`.
- [x] Regras: `finishDiagnosis` exige ≥ 1 reparo; `requestApproval` e `startExecution` exigem `has_stock_pending = false`; `finish` exige todos os reparos `FINISHED`; reparos editáveis só em `IN_DIAGNOSIS`.
- [x] `@Version` em `service_orders`.
- [x] Token de aprovação: 32 bytes `SecureRandom` em base64url, salvo só o SHA-256, comparação em tempo constante.

### Etapa 4 — API REST
- [x] Endpoints do mecânico (seção 2).
- [x] `ExternalController` do cliente com `permitAll` em `/mechanical-hub/service-orders/**`.
- [x] Tratamento de erros padronizado (`404`, `409`, `503`).
- [x] Swagger (springdoc).
- [x] Reemissão do token de aprovação (`POST /orders/{orderId}/approval-token`): só `ADMINISTRATOR`, atrás da flag `APPROVAL_TOKEN_REISSUE_ENABLED` (padrão `true`).

### Etapa 5 — Outbox e relay
- [x] Gravação no `outbox_events` na transação do caso de uso.
- [x] Relay: a cada 1 s, lote de 100 por `sequence`, liderança com `pg_try_advisory_lock`, envio com `SqsTemplate`, marca `SENT`; erro interrompe o lote e incrementa `attempts`.

### Etapa 6 — Consumidor de comandos
- [x] `@SqsListener` (long polling 20 s, visibility timeout 30 s); apaga a mensagem só após o `COMMIT`.
- [x] Deduplicação por `inbox_events`; duplicata com `response_payload` regrava a resposta no outbox.
- [x] Handlers dos 6 comandos (seção 3).
- [x] Retry bloqueante: erro transitório não apaga a mensagem e ajusta o visibility timeout pela `ApproximateReceiveCount` (1 s → 8 s); erro permanente vai direto à DLQ; DLQ nativa com `maxReceiveCount = 6`.
- [x] `SET LOCAL lock_timeout = '5s'` nas transações do consumidor.
- [x] Job diário apagando `inbox_events` com mais de 30 dias.

### Etapa 7 — Notificações
- [x] Porta de notificação + adapter (reaproveitar e-mail/WhatsApp do monolito).
- [x] Orçamento com link `https://<frontend>/orders/{orderId}/approval?token={approvalToken}`, link de pagamento, ordem liberada ao mecânico. Envio após o `COMMIT`; falha no envio só gera log.

### Etapa 8 — Observabilidade
- [x] Agente OpenTelemetry; trace propagado pelo SQS.
- [x] `/actuator/prometheus` + métrica `outbox_oldest_pending_age_seconds`.
- [x] Logs JSON com `orderId`, `transactionId`, `eventId`.

### Etapa 9 — Testes
- [x] Unitários: transições válidas/inválidas, regras do agregado, token, casos de uso.
- [x] Reemissão do token: caso de uso, controller, restrição a `ADMINISTRATOR` (`403`/`401`) e flag desligada (`404`), em slice ``.
- [x] Integração (Testcontainers PostgreSQL + LocalStack): outbox/relay, consumo idempotente, aprovação × recusa concorrentes, endpoints externos.
- [ ] Cobertura ≥ 80% (JaCoCo) e quality gate do SonarQube Cloud no PR.

### Etapa 10 — Deploy
- [x] Dockerfile multi-stage.
- [x] Manifestos: Deployment, Service, ConfigMap, Secret, HPA (1–2), requests/limits.
- [x] Pipeline: build → testes → Sonar → ECR → deploy EKS → smoke test.
- [x] Flag `APPROVAL_TOKEN_REISSUE_ENABLED` no ConfigMap, alimentada pela variável de repositório (padrão `true`).
- [ ] Rotas do `orders` no API Gateway (`mechanical-hub-auth`) e login da Lambda validado contra o banco novo.

### Etapa 11 — Integração e entrega
- [ ] Fluxo com o orquestrador: caminho feliz, pendência de estoque, recusa, pagamento não confirmado.
- [x] README (responsabilidade, papel na saga, como rodar), collection Postman, evidência de cobertura.

## 6. Dependências externas

| Precisa de | De quem | Para |
|---|---|---|
| Filas SQS + DLQs (URLs nos outputs) | `mechanical-hub-infra` | Etapas 5–6 na AWS |
| RDS, usuário do serviço e role `mechanical_hub_auth` | `mechanical-hub-database` | Etapa 2 na AWS |
| ECR e target group | `mechanical-hub-infra` | Etapa 10 |
| Rotas no API Gateway | `mechanical-hub-auth` | Etapa 10 |
| Ids do catálogo combinados | `stock`, `billing` | Etapa 2 |
| Orquestrador no ar | `mechanical-hub-orchestrator` | Etapa 11 |
