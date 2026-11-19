# Spec de Desenvolvimento — `mechanical-hub-billing`

Guia prático para construir o serviço. Regras de negócio e detalhes da integração ficam nos documentos citados; aqui está **o que construir e em que ordem**.

- Stack: Kotlin, Java 25, Spring Boot 4.1.1 (Maven), PostgreSQL 16 (Flyway), Amazon SQS FIFO (Spring Cloud AWS), mensagens em **JSON**, Mercado Pago Orders API (SDK `com.mercadopago:sdk-java` ou cliente HTTP).
- Código de referência: monolito `mechanical-hub` (Clean Architecture, `GatewayAuthenticationFilter`). Não há integração de pagamento no monolito.
- Padrões de desenvolvimento: seguir [`development-standards.md`](../engineering/development-standards.md) (Kotlin, Clean Architecture e testes) em todas as etapas.
- Referências: [`mp_payment_api_integration.md`](../design/integrations/mp_payment_api_integration.md), [RFC-0007](../rfc/RFC-0007-integracao-mercado-pago.md), [`td_finished_os.md`](../design/distributedTransaction/td_finished_os.md) 9, 14 e 17, [`td_payment_failed.md`](../design/distributedTransaction/td_payment_failed.md) F1–F3′, [`data_model.md`](../design/data_model.md) §4 e §6, [`failure_handling.md`](../design/failure_handling.md) §5, [`infrastructure.md`](../infrastructure/infrastructure.md).

## 1. Escopo

**Faz:** catálogo de preços (mão de obra por reparo, preço unitário por material), orçamento, criação da cobrança no Mercado Pago, confirmação do pagamento (webhook) e detecção do vencimento (job de 24h).

**Não faz:** dados do cliente, status da ordem e notificação (`orders`), materiais e quantidades (`stock`), estado da saga (`orchestrator`), tela de pagamento (Checkout Pro do Mercado Pago).

**Fora do escopo por enquanto (foco no fluxo):**
- Cadastro de preços: catálogo fixo nas seeds (seção 4, FR-08).
- Reembolso e histórico de preços com vigência; o preço do momento fica congelado em `budget_items`.
- Envio de dados do pagador (`payer` omitido; o cliente informa na tela do Checkout Pro).

## 2. Endpoints

| Método e rota | Acesso | Efeito | Ref. |
|---|---|---|---|
| `POST /webhooks/payment` | Público (sem autorizador no API Gateway) | Valida `x-signature` (HMAC SHA-256 com `MP_WEBHOOK_SECRET`) → `401` se inválida; consulta `GET /v1/orders/{data.id}`; se `processed` + `accredited`, `PENDING → APPROVED` com `FOR UPDATE` e outbox `paymentConfirmed`; responde em até 22 s | td_finished_os 17 |

Notificação repetida ou pagamento fora de `PENDING` → `200` sem efeito (`CANCELED` + `processed` → log de erro e alerta). Falha transitória (banco ou consulta ao MP) → `500`, e o Mercado Pago reenvia.

## 3. Mensagens

Filas: consome `mechanical-hub-billing-commands.fifo`; publica em `mechanical-hub-orchestrator-events.fifo`. `MessageGroupId = orderId`, `MessageDeduplicationId = eventId`. Envelope JSON: `eventId`, `transactionId`, `orderId`, `type`, `version`, `timestamp` + payload; trace nos *message attributes*.

### Consome (idempotente por `idempotencyKey = {transactionId}:{STEP}` em `inbox_events`)

| Comando | Efeito | Responde | Ref. |
|---|---|---|---|
| `generateBudget` | Busca preço de mão de obra de cada `repairId` e unitário de cada `materialId`; calcula `laborTotal`, `materialTotal`, `orderCost`; grava `budgets` + `budget_items` com o preço do momento. Reparo ou material sem preço → erro permanente | `budgetGenerated` (`budgetId`, `orderCost`) | td_finished_os 9 |
| `generatePaymentLink` | Reaproveita pagamento `PENDING` da transação se existir; senão chama `POST /v1/orders` **fora da transação** (`X-Idempotency-Key = {transactionId}:GENERATE_PAYMENT_LINK`, `expiration_time` = agora + 24h05, boleto excluído); depois grava `payments` (`due_at` = +24h, `expires_at` = +24h05) | `paymentLinkGenerated` (`paymentLink = checkout_url`, `expiresAt = due_at`) | td_finished_os 14 |

### Publica (sempre via outbox, na mesma transação da alteração)

| Evento | Quando | Payload |
|---|---|---|
| `budgetGenerated` | `generateBudget` | `transactionId`, `orderId`, `budgetId`, `orderCost` |
| `paymentLinkGenerated` | `generatePaymentLink` | `transactionId`, `orderId`, `paymentLink`, `expiresAt` |
| `paymentConfirmed` | Webhook com order paga, ou job que encontra a order paga (caso B1) | `transactionId`, `orderId`, `paymentId` |
| `paymentFailed` | Job confirma vencimento (`reason = EXPIRED`, casos A e B2) ou falha (`reason = FAILED`, caso B3) | `transactionId`, `orderId`, `reason` |

### Job de vencimento (a cada 5 min)

Só na instância líder (`pg_try_advisory_lock` com chave própria, diferente da do relay). Seleciona até 100 pagamentos `PENDING` com `due_at <= now()` sem lock de linha; para cada um, `POST /v1/orders/{id}/cancel` (`X-Idempotency-Key = {transactionId}:CANCEL_MP_ORDER`) e decide pela tabela de td_payment_failed F2 (cancelada → falha; `409` → consulta e decide entre pago, expirado, falho ou aguardar). Escrita final com `FOR UPDATE` e transição condicional `PENDING → APPROVED/CANCELED`. Transitório → próximo ciclo; `401`/`404` → log de erro e alerta.

## 4. Banco de dados

Migrations Flyway novas (V1 em diante) no próprio repositório; RDS próprio, provisionado pelo `mechanical-hub-database`.

- Domínio: `service_prices` (PK `service_id` = `repairId`, `labor_price`), `material_prices` (PK `material_id`, `unit_price`), `budgets`, `budget_items` (`item_type` `REPAIR`/`MATERIAL`, `item_ref`, `quantity`, `unit_price`, `total_price`), `payments` (UK `mp_order_id`; `status` `PENDING`/`APPROVED`/`CANCELED`, `mp_status`, `due_at`, `expires_at`, `paid_at`, `canceled_at`).
- Controle: `outbox_events`, `inbox_events`.
- Índices: `payments (status, due_at)` para o job; `payments (transaction_id)`.
- Seeds: `service_prices` e `material_prices` exatamente conforme [`seed_catalog.md`](../design/seed_catalog.md) (mesmos UUIDs de `orders` e `stock`).
- Valores monetários em `NUMERIC(12,2)` e `BigDecimal`; enviados ao Mercado Pago como string.

Fecha o item 9 dos pontos em aberto de [`data_model.md`](../design/data_model.md): duas tabelas de preço, sem vigência.

## 5. Atividades

### Etapa 0 — Configuração do projeto
- [x] Gerar o projeto pelo Spring Initializr na raiz do repositório (Kotlin, Java 25, Spring Boot 4.1.1, Maven):
  ```bash
  curl -sS https://start.spring.io/starter.tgz \
    -d type=maven-project -d language=kotlin \
    -d bootVersion=4.1.1.RELEASE -d javaVersion=25 \
    -d groupId=com.fiap -d artifactId=mechanical-hub-billing -d name=mechanical-hub-billing \
    -d packageName=com.fiap.mechanicalhub.billing \
    -d dependencies=web,security,validation,actuator,prometheus,data-jpa,postgresql,flyway,docker-compose,testcontainers \
    | tar -xzf -
  ```
- [x] Conferir que `pom.xml`, `mvnw` e `src/` ficaram na raiz, ao lado do `README.md` (sem subpasta extra), e remover o `HELP.md` gerado.
- [x] Adicionar ao `pom.xml` o que o Initializr não oferece:
  - Spring Cloud AWS SQS: BOM `io.awspring.cloud:spring-cloud-aws-dependencies` + `spring-cloud-aws-starter-sqs`, em versão compatível com o Spring Boot 4.1 (conferir a matriz de compatibilidade do projeto antes de fixar).
  - Testcontainers LocalStack (`org.testcontainers:localstack`, escopo `test`).
  - `net.logstash.logback:logstash-logback-encoder` (logs JSON, igual ao monolito).
  - SDK do Mercado Pago (`com.mercadopago:sdk-java`), se a opção for pelo SDK em vez de cliente HTTP.
  - WireMock (`org.wiremock:wiremock-standalone`, escopo `test`).
- [x] Plugins `jacoco-maven-plugin` e `sonar-maven-plugin` com a mesma configuração do monolito.
- [x] O monolito é em Java e serve só de referência de regras; o código novo é Kotlin idiomático (data classes, null-safety, sem Lombok).
- [x] `./mvnw -q verify` passando com o projeto vazio (Docker rodando, por causa do Testcontainers).

### Etapa 1 — Base do projeto
- [x] Repositório criado, `main` protegida (PR + checagens obrigatórias).
- [x] Projeto Spring Boot com camadas `domain/`, `application/`, `infrastructure/`.
- [x] `docker-compose` com PostgreSQL 16, LocalStack (filas FIFO + DLQs criadas no init) e WireMock simulando o Mercado Pago.
- [x] Configuração por variáveis de ambiente (banco, URLs das filas, URL base do MP); secrets `MP_ACCESS_TOKEN` e `MP_WEBHOOK_SECRET`; sem chave AWS (usa `LabRole`).
- [ ] Aplicação Checkout Pro, credenciais de teste e usuários de teste (vendedor e comprador) criados no painel do Mercado Pago.

### Etapa 2 — Banco
- [x] Migrations de domínio, controle e seeds (seção 4).

### Etapa 3 — Domínio
- [x] Cálculo do orçamento (`laborTotal`, `materialTotal`, `orderCost`) e itens com preço congelado; ausência de preço → `BusinessRuleException` (permanente).
- [x] `PaymentStatusEnum` (`PENDING`, `APPROVED`, `CANCELED`) e transições condicionais `PENDING → APPROVED` e `PENDING → CANCELED`.
- [x] Prazos: `due_at` = criação + 24h, `expires_at` = criação + 24h05.
- [x] Decisão do job (casos A, B1–B4) como função pura sobre a resposta do Mercado Pago.

### Etapa 4 — Integração com o Mercado Pago
- [x] Porta `PaymentGateway` (`createOrder`, `getOrder`, `cancelOrder`) na camada `application`.
- [x] Adapter com o SDK ou cliente HTTP: timeouts de conexão 2 s e leitura 5 s, **sem retry no cliente**; timeout/5xx/429 → transitório, 400/401/403/404 → permanente; `409` no cancelamento tratado como caso B.
- [x] Payload de criação conforme [`mp_payment_api_integration.md`](../design/integrations/mp_payment_api_integration.md) (A): `type = online`, `processing_mode = manual`, `external_reference = orderId`, item único, boleto excluído.
- [x] Validador de `x-signature` (manifesto `id`, `request-id`, `ts`; HMAC SHA-256).

### Etapa 5 — Webhook
- [x] `POST /webhooks/payment` público no Spring Security (`permitAll`), demais rotas exigindo os headers do gateway.
- [x] Fluxo da seção 2, com a consulta ao MP fora da transação e a escrita final com `FOR UPDATE`.

### Etapa 6 — Outbox e relay
- [x] Gravação no `outbox_events` na transação do caso de uso.
- [x] Relay: a cada 1 s, lote de 100 por `sequence`, liderança com `pg_try_advisory_lock`, envio com `SqsTemplate`, marca `SENT`; erro interrompe o lote e incrementa `attempts`.

### Etapa 7 — Consumidor de comandos
- [x] `@SqsListener` (long polling 20 s, visibility timeout 30 s); apaga a mensagem só após o `COMMIT`.
- [x] Deduplicação por `inbox_events`; duplicata com `response_payload` regrava a resposta no outbox.
- [x] Handlers de `generateBudget` e `generatePaymentLink` (seção 3); no segundo, deduplicação e busca de pagamento `PENDING` antes da chamada ao MP, e a gravação em transação separada depois dela.
- [x] Retry bloqueante: erro transitório não apaga a mensagem e ajusta o visibility timeout pela `ApproximateReceiveCount` (1 s → 8 s); erro permanente vai direto à DLQ; DLQ nativa com `maxReceiveCount = 6`.
- [x] `SET LOCAL lock_timeout = '5s'` nas transações do consumidor.
- [x] Job diário apagando `inbox_events` com mais de 30 dias.

### Etapa 8 — Job de vencimento
- [x] `@Scheduled` a cada 5 min com liderança própria (seção 3).
- [x] Cancelamento, consulta e decisão pela tabela de td_payment_failed F2; escrita final F3 / F3′.

### Etapa 9 — Observabilidade
- [x] Agente OpenTelemetry; trace propagado pelo SQS e nas chamadas HTTP ao Mercado Pago. (`-javaagent` anexado no Dockerfile via `JAVA_TOOL_OPTIONS`, config em `otel/agent.properties`, etapa 11.)
- [x] `/actuator/prometheus` + métricas `outbox_oldest_pending_age_seconds`, pagamentos por status, `paymentFailed` por `reason`, latência e erros por operação do MP, webhooks por resultado (`401`, `200`, `500`).
- [x] Logs JSON com `orderId`, `transactionId`, `eventId`, `mpOrderId` (sem tokens nem corpo de assinatura).
- [ ] Alerta para `401`/`404` do Mercado Pago e para `processed` em pagamento `CANCELED`. (Contadores `billing_alerts_total{type}` e log `ALERTA` prontos; falta a regra de alerta no Prometheus/infra.) Regra pronta em `monitoring/billing-alerts.rules.yaml`; falta copiá-la para o `mechanical-hub-infra`.

### Etapa 10 — Testes
- [x] Unitários: cálculo do orçamento, transições do pagamento, decisão do job (todos os casos de F2), validador de assinatura.
- [x] Integração (Testcontainers PostgreSQL + LocalStack + WireMock): orçamento, link com reaproveitamento de `PENDING` e mesma `X-Idempotency-Key`, webhook (assinatura inválida, pago, repetido), job (casos A, B1–B4, transitório), corrida job × webhook (um único `paymentConfirmed`).
- [ ] Teste manual no sandbox: pagamento aprovado (`APRO`), recusado (`OTHE`) e expiração com `expiration_time` curto.
- [ ] Cobertura ≥ 80% (JaCoCo) e quality gate do SonarQube Cloud no PR. (JaCoCo medido: ~92%; falta o quality gate no PR.)

### Etapa 11 — Deploy
- [x] Dockerfile multi-stage.
- [x] Manifestos: Deployment, Service (NodePort 30082), ConfigMap, Secret (banco, `MP_ACCESS_TOKEN`, `MP_WEBHOOK_SECRET`), HPA (1–2), requests/limits.
- [x] Pipeline: build → testes → Sonar → ECR → deploy EKS → smoke test.
- [ ] URL do webhook (output `webhook_url` do `mechanical-hub-auth`) configurada no painel do Mercado Pago a cada recriação do ambiente.

### Etapa 12 — Integração e entrega
- [ ] Fluxo com o orquestrador: caminho feliz com pagamento real no sandbox, pagamento não confirmado em 24h (com prazo reduzido por configuração para a demonstração), pagamento encontrado pelo job.  (Prazo reduzido já configurável via `BILLING_PAYMENT_DUE_DELAY`; falta executar o fluxo com o orquestrador e o sandbox.)
- [x] README (responsabilidade, papel na saga, integração com o Mercado Pago, como rodar e como testar no sandbox), collection Postman, evidência de cobertura.

## 6. Dependências externas

| Precisa de | De quem | Para |
|---|---|---|
| Filas SQS + DLQs (URLs nos outputs) | `mechanical-hub-infra` | Etapas 6–7 na AWS |
| RDS próprio e usuário do serviço | `mechanical-hub-database` | Etapa 2 na AWS |
| ECR, target group e NodePort 30082 | `mechanical-hub-infra` | Etapa 11 |
| Rota pública `POST /webhooks/payment` repassando os headers do MP | `mechanical-hub-auth` | Etapas 5 e 11 |
| Aplicação, credenciais e webhook configurados | Painel do Mercado Pago | Etapas 1, 4 e 11 |
| Ids do catálogo combinados | `orders`, `stock` | Etapa 2 |
| Orquestrador no ar | `mechanical-hub-orchestrator` | Etapa 12 |
