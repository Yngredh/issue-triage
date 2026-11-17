# Spec de Desenvolvimento — `mechanical-hub-stock`

Guia prático para construir o serviço. Regras de negócio e algoritmos detalhados ficam nos documentos citados; aqui está **o que construir e em que ordem**.

- Stack: Kotlin, Java 25, Spring Boot 4.1.1 (Maven), PostgreSQL 16 (Flyway), Amazon SQS FIFO (Spring Cloud AWS), mensagens em **JSON**.
- Código de referência: monolito `mechanical-hub` (Clean Architecture; casos de uso `ReserveStockForServiceOrderUseCase`, `RegisterStockEntryUseCase`, `ResolveMaterialPendingItemsUseCase`, `RestoreReservedStockItemsUseCase`, `RegisterStockOutUseCase`; alerta `SendStockShortageAlertUseCase`).
- Padrões de desenvolvimento: seguir [`development-standards.md`](../engineering/development-standards.md) (Kotlin, Clean Architecture e testes) em todas as etapas.
- Referências: [`td_finished_os.md`](../design/distributedTransaction/td_finished_os.md) 7 e 22, [`td_stock_pending.md`](../design/distributedTransaction/td_stock_pending.md), [`td_budget_refused.md`](../design/distributedTransaction/td_budget_refused.md) R3, [`td_payment_failed.md`](../design/distributedTransaction/td_payment_failed.md) F7, [`data_model.md`](../design/data_model.md) §3 e §6, [`failure_handling.md`](../design/failure_handling.md), [`infrastructure.md`](../infrastructure/infrastructure.md).

## 1. Escopo

**Faz:** saldo de materiais por status (`AVAILABLE`, `RESERVED`), reserva, baixa e devolução dos materiais de uma ordem, pendências de estoque com fila por antiguidade, entrada de material, alerta de falta aos administradores do estoque.

**Não faz:** preço de material (`billing`), reparos e ordens (`orders`), estado da saga (`orchestrator`).

**Fora do escopo por enquanto (foco no fluxo):**
- Cadastro de materiais e de materiais por reparo: catálogo fixo nas seeds (seção 4, FR-08).
- Consulta dos itens pendentes por ordem ou por material (FR-05) e prazo máximo de pendência (FR-01).
- Movimentação `EXCLUDED` (existe no enum, sem fluxo na Fase 4).

## 2. Endpoints

### Administrador do estoque (protegidos pelo API Gateway; identidade via `x-user-id`, `x-user-role`, `x-user-name`)

| Método e rota | Body / headers | Efeito | Ref. |
|---|---|---|---|
| `POST /stock/entry` | `{ materialId, quantity }` + `Idempotency-Key` (obrigatório) | Soma ao `AVAILABLE`, registra `ENTRY` e resolve pendências do material por antiguidade; publica `stockReserved` para cada ordem que zerar as pendências | td_stock_pending P4 |
| `GET /stock` | — | Saldo `AVAILABLE` e `RESERVED` por material (acompanhamento e demonstração) | — |
| `GET /stock/{materialId}/movements` | — | Movimentações do material | — |

Sem `Idempotency-Key` → `400`. Mesma chave com outro corpo → `422`. Mesma chave e mesmo corpo → resposta armazenada. Material inexistente → `404`. Falha transitória de banco → `503`.

## 3. Mensagens

Filas: consome `mechanical-hub-stock-commands.fifo`; publica em `mechanical-hub-orchestrator-events.fifo`. `MessageGroupId = orderId`, `MessageDeduplicationId = eventId`. Envelope JSON: `eventId`, `transactionId`, `orderId`, `type`, `version`, `timestamp` + payload; trace nos *message attributes*.

### Consome (idempotente por `idempotencyKey = {transactionId}:{STEP}` em `inbox_events`)

| Comando | Efeito | Responde | Ref. |
|---|---|---|---|
| `reserveStock` | Soma as quantidades de `service_materials` dos reparos por material; em ordem crescente de `material_id`, move `AVAILABLE → RESERVED` (`RESERVED`). Material com saldo insuficiente: reserva o disponível e grava o faltante em `stock_pending_items` (com `saga_transaction_id`) | `stockReserved` com `reservedMaterials`, ou `stockPending` (sem materiais) + alerta aos administradores após o `COMMIT` | td_finished_os 7, td_stock_pending P1 |
| `consumeStock` | Para cada material de `reservedMaterials`: baixa do `RESERVED` (`OUT`); `AVAILABLE` não muda | `stockConsumed` | td_finished_os 22 |
| `restoreStock` | Lock da ordem; devolve `RESERVED → AVAILABLE` (`RETURN`) de todos os materiais; depois, para cada material devolvido, roda a resolução de pendências por antiguidade | `stockRestored` + `stockReserved` de cada outra ordem que zerar as pendências | td_budget_refused R3, td_payment_failed F7 |

`RESERVED` menor que a quantidade a baixar ou devolver → erro permanente (DLQ e alerta).

### Publica (sempre via outbox, na mesma transação da alteração)

| Evento | Quando | Payload |
|---|---|---|
| `stockReserved` | `reserveStock` sem faltas; pendência zerada por entrada ou devolução | `transactionId`, `orderId`, `reservedMaterials[{materialId, quantity}]` (soma das movimentações `RESERVED` da ordem) |
| `stockPending` | `reserveStock` com ao menos um faltante | `transactionId`, `orderId` |
| `stockConsumed` | `consumeStock` | `transactionId`, `orderId` |
| `stockRestored` | `restoreStock` | `transactionId`, `orderId` |

## 4. Banco de dados

Migrations Flyway novas (V1 em diante) no próprio repositório; RDS próprio, provisionado pelo `mechanical-hub-database`.

- Domínio: `materials` (sem preço), `stock` (UK `material_id + status`), `service_materials` (`service_id` = `repairId`, sem FK; UK `service_id + material_id`), `stock_movements` (`service_order_id` sem FK, `NULL` em `ENTRY`), `stock_pending_items` (+ `saga_transaction_id`).
- Controle: `outbox_events`, `inbox_events`, `idempotency_requests`.
- Seeds: `materials`, `stock` (saldo `AVAILABLE` inicial, `RESERVED` = 0) e `service_materials` exatamente conforme [`seed_catalog.md`](../design/seed_catalog.md) (mesmos UUIDs; inclui materiais com saldo 0 e baixo para demonstrar a pendência).
- Invariante a preservar em todo código: se existe pendência de um material, o `AVAILABLE` dele é 0.

## 5. Atividades

### Etapa 0 — Configuração do projeto
- [x] Gerar o projeto pelo Spring Initializr na raiz do repositório (Kotlin, Java 25, Spring Boot 4.1.1, Maven):
  ```bash
  curl -sS https://start.spring.io/starter.tgz \
    -d type=maven-project -d language=kotlin \
    -d bootVersion=4.1.1.RELEASE -d javaVersion=25 \
    -d groupId=com.fiap -d artifactId=mechanical-hub-stock -d name=mechanical-hub-stock \
    -d packageName=com.fiap.mechanicalhub.stock \
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
- [x] `StockStatusEnum` (`AVAILABLE`, `RESERVED`) e `StockMovementTypeEnum` (`ENTRY`, `RESERVED`, `RETURN`, `EXCLUDED`, `OUT`).
- [x] Cálculo da necessidade por material a partir da lista de reparos (soma por `material_id`).
- [x] Regra de reserva parcial: reserva `min(AVAILABLE, necessário)`, faltante vira pendência.
- [x] Rotina única de resolução de pendências por antiguidade (`created_at, id`), usada pela entrada e pela devolução: reserva `min(AVAILABLE, item.quantity)`, apaga ou reduz o item, para quando o `AVAILABLE` chega a 0.
- [x] Montagem de `reservedMaterials` pela soma das movimentações `RESERVED` da ordem.

### Etapa 4 — Concorrência
- [x] Locks de material sempre em ordem crescente de `material_id` (`SELECT ... FOR UPDATE`).
- [x] `pg_advisory_xact_lock(hash(orderId))` por ordem na resolução de pendências e no `restoreStock`; no `restoreStock`, travar todas as linhas de material antes de qualquer lock de outra ordem.
- [x] Contagem de pendências da ordem feita sob o lock da ordem (exatamente uma transação publica o `stockReserved`).
- [x] `SET LOCAL lock_timeout = '5s'` em consumidor e endpoint; deadlock e `lock_timeout` são erros transitórios (`503` no REST, retry na fila).

### Etapa 5 — API REST
- [x] `POST /stock/entry` com idempotência por `idempotency_requests` na mesma transação (seção 2).
- [x] Consultas de saldo e movimentações.
- [x] Tratamento de erros padronizado (`400`, `404`, `422`, `503`).
- [x] Swagger (springdoc).

### Etapa 6 — Outbox e relay
- [x] Gravação no `outbox_events` na transação do caso de uso.
- [x] Relay: a cada 1 s, lote de 100 por `sequence`, liderança com `pg_try_advisory_lock`, envio com `SqsTemplate`, marca `SENT`; erro interrompe o lote e incrementa `attempts`.

### Etapa 7 — Consumidor de comandos
- [x] `@SqsListener` (long polling 20 s, visibility timeout 30 s); apaga a mensagem só após o `COMMIT`.
- [x] Deduplicação por `inbox_events`; duplicata com `response_payload` regrava a resposta no outbox.
- [x] Handlers dos 3 comandos (seção 3).
- [x] Retry bloqueante: erro transitório não apaga a mensagem e ajusta o visibility timeout pela `ApproximateReceiveCount` (1 s → 8 s); erro permanente vai direto à DLQ; DLQ nativa com `maxReceiveCount = 6`.
- [x] Jobs diários: `inbox_events` com mais de 30 dias e `idempotency_requests` com mais de 7 dias.

### Etapa 8 — Notificações
- [x] Porta de notificação + adapter (reaproveitar o alerta de falta de material do monolito).
- [x] Alerta aos administradores após o `COMMIT` do `stockPending` (materiais e quantidades faltantes); falha no envio só gera log.

### Etapa 9 — Observabilidade
- [x] Agente OpenTelemetry; trace propagado pelo SQS.
- [x] `/actuator/prometheus` + métricas `outbox_oldest_pending_age_seconds`, `stock_pending_items_total` e saldo `AVAILABLE`/`RESERVED` por material.
- [x] Logs JSON com `orderId`, `transactionId`, `eventId`, `materialId`.

### Etapa 10 — Testes
- [x] Unitários: necessidade por material, reserva parcial, resolução por antiguidade (exemplo da tabela de td_stock_pending P4), montagem de `reservedMaterials`.
- [x] Integração (Testcontainers PostgreSQL + LocalStack): reserva com e sem faltas, baixa, devolução com redistribuição, consumo idempotente, `POST /stock/entry` repetido e com corpo diferente.
- [x] Concorrência: reservas simultâneas do mesmo material; duas entradas de materiais diferentes que zeram a mesma ordem (um único `stockReserved`); entrada concorrente com `restoreStock`; invariante `pendência ⇒ AVAILABLE = 0` após cada cenário.
- [ ] Cobertura ≥ 80% (JaCoCo) e quality gate do SonarQube Cloud no PR.

### Etapa 11 — Deploy
- [x] Dockerfile multi-stage.
- [x] Manifestos: Deployment, Service (NodePort 30081), ConfigMap, Secret, HPA (1–2), requests/limits.
- [x] Pipeline: build → testes → Sonar → ECR → deploy EKS → smoke test.

### Etapa 12 — Integração e entrega
- [x] Fluxo com o orquestrador: caminho feliz, pendência resolvida por entrada, recusa e pagamento não confirmado com redistribuição para ordem pendente.
- [x] README (responsabilidade, papel na saga, como rodar), collection Postman, evidência de cobertura.

## 6. Dependências externas

| Precisa de | De quem | Para |
|---|---|---|
| Filas SQS + DLQs (URLs nos outputs) | `mechanical-hub-infra` | Etapas 6–7 na AWS |
| RDS próprio e usuário do serviço | `mechanical-hub-database` | Etapa 2 na AWS |
| ECR, target group e NodePort 30081 | `mechanical-hub-infra` | Etapa 11 |
| Rota `/stock/**` no API Gateway (perfil administrador) | `mechanical-hub-auth` | Etapa 11 |
| Ids do catálogo combinados | `orders`, `billing` | Etapa 2 |
| Orquestrador no ar | `mechanical-hub-orchestrator` | Etapa 12 |
