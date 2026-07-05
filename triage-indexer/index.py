#!/usr/bin/env python3
"""Indexador da memória de triagens (Fase D, Fatia A).

Reconcilia os relatórios de triagem que a borda persistiu no Garage
(triage/{ns}/{alertname}/{ts}__{dedup_key}.md) para uma collection do Qdrant,
para recuperação semântica de incidentes passados.

Espelha o qa-indexer (mesmo FastEmbedProvider/e5-large local, mesmo upsert
idempotente por uuid5, mesmo GC por run_id, mesma guarda anti-silent-failure).
Diverge em três pontos, todos específicos de triagem:

  1. Fonte: os .md já estão no /workspace/repo (baixados do Garage por um
     container rclone anterior no containerSet), não vêm de git.
  2. Identidade: o id do ponto deriva do dedup_key do front-matter (identidade
     estável do incidente), não do path — re-triagem do mesmo incidente
     sobrescreve em vez de duplicar.
  3. Conteúdo: cada .md tem front-matter YAML + preâmbulo do modelo + relatório.
     Embeda-se SÓ o corpo do relatório (front-matter e preâmbulo removidos), e
     os fatos do front-matter (namespace, alertnames, confirmation, ...) vão ao
     payload para o filtro híbrido vetor+metadado do Tier 0/1.

Não extrai verdict/confidence: o front-matter da v1 não os tem (ADR-0008); eles
entram quando a Fatia B (classificador) existir, e o reconcile re-embeda.
"""

import asyncio
import glob
import os
import sys
import uuid
from datetime import datetime, timezone

from mcp_server_qdrant.embeddings.fastembed import FastEmbedProvider
from qdrant_client import AsyncQdrantClient, models

QDRANT_URL = os.environ["QDRANT_URL"]
API_KEY = os.environ.get("QDRANT_API_KEY")
COLLECTION = os.environ.get("COLLECTION_NAME", "triage_reports")
MODEL = os.environ.get("EMBEDDING_MODEL", "intfloat/multilingual-e5-large")
REPO_DIR = os.environ.get("REPO_DIR", "/workspace/repo")
GLOBS = os.environ.get("DOC_GLOBS", "**/*.md").split(",")
# Chunk maior que o qa-indexer (1000): um relatório de triagem é uma unidade de
# raciocínio coesa (sintoma -> evidência -> causa -> confiança); fragmentar
# demais separa a causa da evidência que a sustenta. Calibrável — como o design
# é reconcile, mudar e re-embedar tudo é de graça.
CHUNK = int(os.environ.get("CHUNK_CHARS", "2000"))
OVERLAP = int(os.environ.get("CHUNK_OVERLAP", "200"))
BATCH = int(os.environ.get("EMBED_BATCH", "8"))
RUN_ID = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
NS = uuid.UUID("6ba7b811-9dad-11d1-80b4-00c04fd430c8")  # uuid url namespace


def parse_document(text):
    """Separa (front_matter: dict, body: str) de um .md de triagem.

    O front-matter é o YAML restrito que a borda Go emite (garage_document.go):
    delimitado por '---', uma linha 'chave: valor' por campo, aspas duplas nas
    strings, e listas como '  - "item"'. Parseia-se exatamente esse formato, sem
    PyYAML — o parser casa o que a borda produz, e não depende de dependência na
    imagem base.

    O corpo tem o PREÂMBULO do modelo antes do relatório ("Tenho evidência
    suficiente... Vou consolidar." e um '---' solto). Corta-se determinística-
    mente até a primeira linha de cabeçalho markdown ('# ' ou '## '): é onde o
    relatório estruturado começa. Se não houver cabeçalho, usa-se o corpo todo.
    """
    lines = text.splitlines()
    fm = {}
    body_start = 0

    # Front-matter: só se o arquivo abre com '---' na primeira linha.
    if lines and lines[0].strip() == "---":
        i = 1
        pending_list_key = None
        while i < len(lines) and lines[i].strip() != "---":
            raw = lines[i]
            stripped = raw.strip()
            if stripped.startswith("- "):
                # Item de lista do campo anterior.
                if pending_list_key is not None:
                    fm[pending_list_key].append(_unquote(stripped[2:].strip()))
            elif ":" in stripped:
                key, _, val = stripped.partition(":")
                key = key.strip()
                val = val.strip()
                if val == "" or val == "[]":
                    # Início de lista (val vazio) ou lista vazia inline.
                    fm[key] = []
                    pending_list_key = key if val == "" else None
                else:
                    fm[key] = _unquote(val)
                    pending_list_key = None
            i += 1
        body_start = i + 1  # pula o '---' de fechamento

    # Corta o preâmbulo: pula até o primeiro cabeçalho markdown.
    body_lines = lines[body_start:]
    header_idx = None
    for idx, line in enumerate(body_lines):
        if line.lstrip().startswith("#"):
            header_idx = idx
            break
    body = "\n".join(body_lines[header_idx:] if header_idx is not None else body_lines)
    return fm, body.strip()


def _unquote(s):
    """Remove aspas duplas externas e desfaz o escape mínimo do yamlString Go."""
    s = s.strip()
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        s = s[1:-1]
        s = s.replace('\\"', '"').replace("\\n", "\n").replace("\\t", "\t")
        s = s.replace("\\r", "\r").replace("\\\\", "\\")
    return s


def chunk_text(text):
    paras, chunks, cur = [p.strip() for p in text.split("\n\n") if p.strip()], [], ""
    for p in paras:
        if len(cur) + len(p) + 2 <= CHUNK:
            cur = (cur + "\n\n" + p).strip()
        else:
            if cur:
                chunks.append(cur)
            if len(p) <= CHUNK:
                cur = p
            else:  # parágrafo gigante: janela com overlap
                for i in range(0, len(p), CHUNK - OVERLAP):
                    chunks.append(p[i : i + CHUNK])
                cur = ""
    if cur:
        chunks.append(cur)
    return chunks


def collect_docs():
    """Coleta (dedup_key, chunk_idx, chunk, front_matter) de cada .md.

    O dedup_key é a identidade do ponto; sem ele o documento não é indexável de
    forma idempotente, então relatórios sem dedup_key no front-matter são
    pulados (com aviso) em vez de indexados com id derivado do path.
    """
    docs = []
    for g in GLOBS:
        for path in glob.glob(os.path.join(REPO_DIR, g.strip()), recursive=True):
            if not os.path.isfile(path):
                continue
            try:
                txt = open(path, encoding="utf-8").read()
            except Exception:
                continue
            fm, body = parse_document(txt)
            dedup = fm.get("dedup_key") or fm.get("incident_key")
            if not dedup:
                print(f"[triage-indexer] AVISO: sem dedup_key, pulando {path}", flush=True)
                continue
            if not body:
                print(f"[triage-indexer] AVISO: corpo vazio, pulando {path}", flush=True)
                continue
            for i, ch in enumerate(chunk_text(body)):
                docs.append((dedup, i, ch, fm))
    return docs


def _payload(dedup, i, ch, fm):
    """Payload do ponto: o chunk + os fatos do front-matter para filtro híbrido."""
    return {
        "document": ch,
        "metadata": {
            "dedup_key": dedup,
            "incident_key": fm.get("incident_key", dedup),
            "namespace": fm.get("namespace", ""),
            "alertnames": fm.get("alertnames", []),
            "fired_at": fm.get("fired_at", ""),
            "triaged_at": fm.get("triaged_at", ""),
            # Gancho da decisão 4 (qualidade de memória): permite ao Tier 1
            # filtrar/despriorizar diagnósticos refutados.
            "confirmation": fm.get("confirmation", "unverified"),
            "chunk": i,
            "run_id": RUN_ID,
        },
    }


async def main():
    provider = FastEmbedProvider(MODEL)
    vname, vsize = provider.get_vector_name(), provider.get_vector_size()
    client = AsyncQdrantClient(url=QDRANT_URL, api_key=API_KEY)

    # Garante o schema certo (vetor nomeado); recria se incompatível.
    recreate = True
    if await client.collection_exists(COLLECTION):
        vectors = (await client.get_collection(COLLECTION)).config.params.vectors
        if isinstance(vectors, dict) and vname in vectors:
            recreate = False
        else:
            print(f"[triage-indexer] schema incompatível em {COLLECTION}; recriando", flush=True)
            await client.delete_collection(COLLECTION)
    if recreate:
        await client.create_collection(
            collection_name=COLLECTION,
            vectors_config={vname: models.VectorParams(size=vsize, distance=models.Distance.COSINE)},
        )
        print(f"[triage-indexer] collection {COLLECTION} criada (vetor '{vname}', dim {vsize})", flush=True)

    docs = collect_docs()
    if not docs:
        # Guarda anti-silent-failure: 0 documentos com a collection já populada
        # provavelmente é falha do rclone (fonte vazia), não corpus vazio. Falhar
        # evita que o GC abaixo apague a collection inteira. Na PRIMEIRA run
        # (collection recém-criada) 0 docs é legítimo — corpus ainda não existe.
        if recreate:
            print("[triage-indexer] 0 documentos na primeira run (corpus vazio ainda); ok", flush=True)
            with open("/workspace/indexed_count", "w") as f:
                f.write("0")
            return
        print("[triage-indexer] ERRO: 0 documentos com collection populada — falha da fonte? Abortando (guarda anti-silent-failure)", flush=True)
        sys.exit(1)

    total = 0
    for s in range(0, len(docs), BATCH):
        batch = docs[s : s + BATCH]
        vecs = await provider.embed_documents([d[2] for d in batch])  # passage_embed
        points = [
            models.PointStruct(
                id=str(uuid.uuid5(NS, f"{dedup}#{i}")),  # dedup_key -> idempotente por incidente
                vector={vname: v},
                payload=_payload(dedup, i, ch, fm),
            )
            for (dedup, i, ch, fm), v in zip(batch, vecs)
        ]
        await client.upsert(collection_name=COLLECTION, points=points)
        total += len(points)
    print(f"[triage-indexer] upsert de {total} chunks (run {RUN_ID})", flush=True)

    # GC: remove chunks de runs antigas (relatórios apagados/encolhidos no Garage).
    await client.delete(
        collection_name=COLLECTION,
        points_selector=models.FilterSelector(
            filter=models.Filter(
                must_not=[
                    models.FieldCondition(
                        key="metadata.run_id", match=models.MatchValue(value=RUN_ID)
                    )
                ]
            )
        ),
    )
    print(f"[triage-indexer] GC ok; run corrente {RUN_ID}", flush=True)

    with open("/workspace/indexed_count", "w") as f:
        f.write(str(total))


if __name__ == "__main__":
    asyncio.run(main())
