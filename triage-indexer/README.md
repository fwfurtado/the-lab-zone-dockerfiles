# triage-indexer

Indexa a memória de triagens (Fase D, Fatia A): reconcilia os relatórios que a
borda de triagem persiste no Garage para uma collection do Qdrant
(`triage_reports`), habilitando recuperação semântica de incidentes passados.

Espelha o [`qa-indexer`](../qa-indexer): mesmo `FastEmbedProvider`/e5-large
local, mesmo upsert idempotente por `uuid5`, mesmo GC por `run_id`. Diverge em:

- **Fonte**: os `.md` chegam do Garage (baixados por um container rclone no
  `containerSet`), não de git.
- **Identidade**: o id do ponto deriva do `dedup_key` do front-matter
  (identidade estável do incidente), não do path.
- **Conteúdo**: parseia o front-matter YAML, corta o preâmbulo do modelo e
  embeda só o corpo do relatório; os fatos do front-matter (namespace,
  alertnames, confirmation, ...) vão ao payload para filtro híbrido.

Não extrai `verdict`/`confidence` (ADR-0008): entram quando o classificador
(Fatia B) existir; o reconcile re-embeda a partir do `.md` cru.

## Variáveis

| Env | Default | Descrição |
|-----|---------|-----------|
| `QDRANT_URL` | (obrigatória) | endpoint do Qdrant |
| `QDRANT_API_KEY` | — | api key do Qdrant |
| `COLLECTION_NAME` | `triage_reports` | collection destino (separada do `qa_docs`) |
| `EMBEDDING_MODEL` | `intfloat/multilingual-e5-large` | mesmo do qa-indexer e do mcp-server-qdrant |
| `REPO_DIR` | `/workspace/repo` | onde o rclone deixou os `.md` |
| `CHUNK_CHARS` | `2000` | maior que o qa-indexer; relatório é unidade coesa (calibrável) |
| `CHUNK_OVERLAP` | `200` | overlap para parágrafos gigantes |
| `EMBED_BATCH` | `8` | tamanho do batch de embedding |

## Testes

```
QDRANT_URL=x python -m pytest test_index.py
```

Cobrem o parser de front-matter e o corte de preâmbulo (a lógica com regra).
FastEmbed/Qdrant são I/O, validados por integração no cluster.
