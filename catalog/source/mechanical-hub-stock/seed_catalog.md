# Catálogo de Seeds

Fonte única dos dados de catálogo usados nas seeds de `orders`, `stock` e `billing`. Cada serviço escreve a própria migration de seed a partir destas tabelas, com **os mesmos UUIDs**. Os valores vêm do monolito (`mechanical-hub`, `V16__seed_mechanical_hub.sql`).

Alterar um ID ou valor aqui exige alterar a seed dos três serviços.

## 1. Reparos (`repairId`)

| `repairId` | Nome | Descrição | Mão de obra (R$) |
|---|---|---|---|
| `03d94934-6201-449b-b8db-490546c35d34` | Troca de Óleo e Filtro | Substituição do óleo do motor e filtro de óleo conforme especificação do fabricante. | 130.00 |
| `b9090e29-c8f8-498f-b536-bb70cb69113e` | Manutenção de Sistema de Freios | Substituição de pastilhas de freio dianteiras e fluido de freio para garantir eficiência na frenagem. | 210.00 |
| `192c4db5-61b3-4cf7-b02b-54e948a9ce5d` | Substituição de Correia Dentada | Troca da correia dentada e tensores conforme intervalo recomendado pelo fabricante, prevenindo falhas no motor. | 380.00 |
| `eeff2222-2222-2222-2222-000000000001` | Revisão do Sistema de Ignição | Substituição de velas, cabos de vela e bobina de ignição para restaurar o desempenho do motor. | 200.00 |

Mão de obra = `base_price + labor_cost` do monolito, para que o orçamento (`laborTotal + materialTotal`) feche com o `total_price` antigo (seção 5).

## 2. Materiais (`materialId`)

| `materialId` | Nome | Descrição | Preço unitário (R$) | Estoque mínimo | Saldo inicial `AVAILABLE` |
|---|---|---|---|---|---|
| `789492e9-c859-4ad7-8a9a-39d3ffb12519` | Óleo de Motor 5W-30 Sintético (1L) | Óleo lubrificante sintético para motores flex e a gasolina, viscosidade 5W-30. | 38.90 | 20 | 5 |
| `eb694988-23ef-413b-b68a-c510f6346534` | Filtro de Óleo Universal | Filtro de óleo rosqueável compatível com motores 1.0 a 2.0 de múltiplas marcas. | 22.50 | 10 | 5 |
| `99370d9f-1cbc-468d-8043-94588108b7c2` | Pastilha de Freio Dianteira | Jogo de pastilhas de freio dianteiras com indicador de desgaste, compatível com veículos populares. | 89.90 | 8 | **0** |
| `a7f744e8-2a29-41ba-85f4-6eb13317af07` | Fluido de Freio DOT 4 (500ml) | Fluido de freio sintético DOT 4, alta temperatura de ebulição, para sistemas ABS e convencionais. | 32.00 | 10 | **0** |
| `88221fb1-880c-4eb8-b0ed-be3e59d34269` | Correia Dentada | Correia dentada para motores 1.0 a 1.6, fabricada em borracha reforçada com fibra de aramida. | 95.00 | 5 | 3 |
| `18c8a5ca-1d69-4690-a9c5-0877d928b96a` | Tensor de Correia Dentada | Tensor automático para correia dentada, garante tensão adequada e reduz desgaste prematuro. | 75.00 | 5 | 1 |
| `b6313df9-9c00-4750-84ad-1af4912f9373` | Rolamento Esticador de Correia | Rolamento esticador para kit de correia dentada, compatível com motores de 1.0 a 2.0. | 55.00 | 5 | 5 |
| `eeff1111-1111-1111-1111-000000000001` | Vela de Ignição | Vela de ignição para motores 1.0 a 1.6 flex, resistência iridium para melhor desempenho. | 18.50 | 10 | 5 |
| `eeff1111-1111-1111-1111-000000000002` | Cabo de Vela de Ignição | Jogo de cabos de vela com blindagem eletromagnética, compatível com motores 1.0 a 2.0. | 55.00 | 5 | 3 |
| `eeff1111-1111-1111-1111-000000000003` | Bobina de Ignição | Bobina de ignição individual para motores com sistema de ignição direta (DIS). | 135.00 | 3 | 2 |

Saldo `RESERVED` inicial: **0** para todos. O banco do `stock` nasce sem ordens, então não há reserva a sustentar (no monolito, as reservas vinham das ordens de exemplo).

## 3. Materiais por reparo

| `repairId` | `materialId` | Quantidade |
|---|---|---|
| `03d94934-…-490546c35d34` (Troca de Óleo) | `789492e9-…-39d3ffb12519` (Óleo) | 4 |
| `03d94934-…-490546c35d34` (Troca de Óleo) | `eb694988-…-c510f6346534` (Filtro de Óleo) | 1 |
| `b9090e29-…-bb70cb69113e` (Freios) | `99370d9f-…-94588108b7c2` (Pastilha) | 1 |
| `b9090e29-…-bb70cb69113e` (Freios) | `a7f744e8-…-6eb13317af07` (Fluido de Freio) | 1 |
| `192c4db5-…-54e948a9ce5d` (Correia) | `88221fb1-…-be3e59d34269` (Correia Dentada) | 1 |
| `192c4db5-…-54e948a9ce5d` (Correia) | `18c8a5ca-…-0877d928b96a` (Tensor) | 1 |
| `192c4db5-…-54e948a9ce5d` (Correia) | `b6313df9-…-1af4912f9373` (Rolamento) | 1 |
| `eeff2222-…-000000000001` (Ignição) | `eeff1111-…-000000000001` (Vela) | 1 |
| `eeff2222-…-000000000001` (Ignição) | `eeff1111-…-000000000002` (Cabo de Vela) | 1 |
| `eeff2222-…-000000000001` (Ignição) | `eeff1111-…-000000000003` (Bobina) | 1 |

IDs completos nas seções 1 e 2. O `id` próprio de cada linha de `service_materials` pode ser gerado pela migration (não trafega entre serviços).

## 4. O que cada serviço semeia

| Serviço | Tabela | Origem |
|---|---|---|
| `orders` | `services` (sem preço) | Seção 1: `repairId`, nome, descrição |
| `orders` | `profiles`, `users`, `customers`, `vehicles` | Monolito: `V3__seed_profiles.sql`, `V15__seed_default_users.sql`, `V16__seed_mechanical_hub.sql`, mantendo os UUIDs (dados só do `orders`) |
| `stock` | `materials` (sem preço) | Seção 2: `materialId`, nome, descrição, estoque mínimo |
| `stock` | `stock` | Seção 2: saldo `AVAILABLE`; `RESERVED` = 0 |
| `stock` | `service_materials` | Seção 3 |
| `billing` | `service_prices` | Seção 1: `service_id` = `repairId`, `labor_price` = mão de obra |
| `billing` | `material_prices` | Seção 2: `material_id`, `unit_price` |

Nenhum serviço semeia ordens de serviço, movimentações, pendências, orçamentos ou pagamentos: esses dados nascem pela saga.

## 5. Cenários de demonstração

| Reparo | Resultado esperado na reserva | Orçamento (`orderCost`) |
|---|---|---|
| Troca de Óleo e Filtro | Reserva completa (óleo 5 ≥ 4, filtro 5 ≥ 1) | 130.00 + 155.60 + 22.50 = **308.10** |
| Manutenção de Sistema de Freios | **Pendência de estoque** (pastilha e fluido com saldo 0) → `td_stock_pending.md`; liberada por `POST /stock/entry` | 210.00 + 89.90 + 32.00 = **331.90** |
| Substituição de Correia Dentada | Reserva completa na 1ª ordem; **pendência na 2ª** (tensor com saldo 1) | 380.00 + 95.00 + 75.00 + 55.00 = **605.00** |
| Revisão do Sistema de Ignição | Reserva completa | 200.00 + 18.50 + 55.00 + 135.00 = **408.50** |
