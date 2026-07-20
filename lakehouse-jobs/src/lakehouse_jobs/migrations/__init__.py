"""Registro ordenado de migrations. Regras:
- NUNCA remover nem reordenar entradas: o id é a identidade no estado.
- Toda migration deve ser idempotente (IF NOT EXISTS / guards): o runner e
  at-least-once (crash entre aplicar e registrar => re-execucao).
- Migrations ESCREVEM na tabela => contrato single-writer: rodar sempre com o
  gerador suspenso (spec.suspend=true).
"""

from lakehouse_jobs.migrations import m001_create_accounts

REGISTRY = [
    ("m001_create_accounts", m001_create_accounts.apply),
]
