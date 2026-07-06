# triage-indexer

Indexadores da memória de triagens (Fase D). Reconciliam os relatórios que a
borda persiste no Garage para collections do Qdrant, habilitando recuperação
semântica de incidentes passados.

Um code base, **dois entrypoints** sobre o núcleo comum (`_common.py`):

| Entrypoint | Collection | Unidade | Serve |
|-----------|-----------|---------|-------|
| `triage-index-gestalt` | `triage_incidents` | 1 ponto por relatório | busca por **gestalt** (regressão: "esse incidente já aconteceu?") |
| `triage-index-facets` | `triage_facets` | 1 ponto por seção | busca por **faceta** (sintoma/causa/hint de investigação) |

Rodam em paralelo como dois steps de um Argo Workflow (fan-out após um
downloader rclone compartilhado), cada um com sua métrica e retry — falha
isolada e visível.

## Compartilhado vs. específico

`_common.py` tem tudo que os dois modos dividem: parse do front-matter (YAML
restrito da borda, sem PyYAML), corte do preâmbulo (scan até o 1º cabeçalho),
carga do e5-large, upsert idempotente por `uuid5(dedup_key[#suffix])`, GC por
`run_id`, guarda anti-silent-failure, e a criação de índices de payload
(habilitam o filtro do Tier 0/1). O único ponto de variação é `build_points`:
gestalt emite 1 ponto (corpo inteiro); facets emite 1 por seção.

- **Preâmbulo** (fronteira): scan de linha, sem mistune. Achar onde o documento
  começa é localizar uma linha divisória.
- **Seções** (estrutura): mistune. Precisa dos níveis de heading, aninhamento
  (`###` dentro de `##`), e robustez contra `## x` dentro de code fence.

Facetas: `symptom`, `evidence`, `cause`, `next_step` (as 4 seções que todo
relatório tem). Confiança fica de fora (2 linhas viram ruído no top-k).

Não extrai `verdict`/`confidence` (ADR-0008): Fatia B (classificador), depois.

## Testes

```
pip install -e . && python -m pytest tests/
```

Cobrem parse, preâmbulo, e o corte de seções (aninhamento, code fence,
variações de nome). FastEmbed/Qdrant são I/O, validados por integração.
