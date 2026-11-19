-- Seed do schema `catalog` (ADR-0001, D8). Executada pelo initdb depois de `catalog/schema.sql`.
-- Reexecutável: `projects` por upsert e, por repositório, `DELETE` + `INSERT` das orientações,
-- tudo numa transação. Para aplicar de novo: `psql -f catalog/seed.sql`.
-- `Yaane-h/mechanical-hub-auth` fica de fora de propósito, para demonstrar `unknown_repo` (RF10).

BEGIN;

INSERT INTO catalog.projects (repository, name, responsible, description) VALUES
  ('Yaane-h/mechanical-hub-orders', 'Ordens de Serviço', '@Yaane-h',
   'Abre e acompanha ordens de serviço, cuida dos reparos, clientes e veículos, e expõe a API para mecânicos e clientes'),
  ('Yaane-h/mechanical-hub-billing', 'Orçamento e Pagamento', '@Yaane-h',
   'Calcula o orçamento, gera a cobrança no Mercado Pago e confirma o pagamento'),
  ('Yaane-h/mechanical-hub-stock', 'Estoque', '@Yaane-h',
   'Controla o saldo de materiais, reservas, baixas, devoluções e pendências de estoque'),
  ('Yaane-h/mechanical-hub-orchestrator', 'Orquestrador da saga', '@Yaane-h',
   'Conduz a saga de cada ordem de serviço entre orders, stock e billing, incluindo as compensações')
ON CONFLICT (repository) DO UPDATE SET
  name        = EXCLUDED.name,
  responsible = EXCLUDED.responsible,
  description = EXCLUDED.description;

DELETE FROM catalog.triage_guidelines
WHERE repository IN (
  'Yaane-h/mechanical-hub-orders',
  'Yaane-h/mechanical-hub-billing',
  'Yaane-h/mechanical-hub-stock',
  'Yaane-h/mechanical-hub-orchestrator'
);

-- category NULL = vale para todas as categorias.
INSERT INTO catalog.triage_guidelines (repository, category, guideline) VALUES
  ('Yaane-h/mechanical-hub-orders', NULL,
   'Peça o número da ordem de serviço (`orderNumber` ou `orderId`) e o status em que ela estava.'),
  ('Yaane-h/mechanical-hub-orders', 'bug',
   'Erros em mudança de status devem pedir o status atual e o status pedido.'),
  ('Yaane-h/mechanical-hub-orders', 'melhoria',
   'Pedidos de cadastro de clientes, veículos ou funcionários pela API estão fora do escopo atual.'),
  ('Yaane-h/mechanical-hub-billing', NULL,
   'Nunca peça dados de cartão ou dados bancários.'),
  ('Yaane-h/mechanical-hub-billing', 'bug',
   'Problemas de pagamento devem pedir o `orderId` e o horário da tentativa.'),
  ('Yaane-h/mechanical-hub-stock', 'bug',
   'Problemas de saldo devem pedir o `materialId` e o saldo mostrado em `GET /stock`.'),
  ('Yaane-h/mechanical-hub-orchestrator', NULL,
   'Ordem parada no meio do fluxo deve pedir o `orderId` e o último status visto.'),
  ('Yaane-h/mechanical-hub-orchestrator', 'duvida',
   'Dúvidas sobre o fluxo devem apontar a sequência de passos da saga.');

COMMIT;
