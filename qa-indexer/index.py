#!/usr/bin/env python3
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
COLLECTION = os.environ.get("COLLECTION_NAME", "qa_docs")
MODEL = os.environ.get("EMBEDDING_MODEL", "intfloat/multilingual-e5-large")
REPO_DIR = os.environ.get("REPO_DIR", "/workspace/repo")
GLOBS = os.environ.get("DOC_GLOBS", "**/*.md").split(",")
CHUNK = int(os.environ.get("CHUNK_CHARS", "1000"))
OVERLAP = int(os.environ.get("CHUNK_OVERLAP", "150"))
BATCH = int(os.environ.get("EMBED_BATCH", "32"))
RUN_ID = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
NS = uuid.UUID("6ba7b811-9dad-11d1-80b4-00c04fd430c8")  # uuid url namespace


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
    docs = []
    for g in GLOBS:
        for path in glob.glob(os.path.join(REPO_DIR, g.strip()), recursive=True):
            if not os.path.isfile(path):
                continue
            rel = os.path.relpath(path, REPO_DIR)
            try:
                txt = open(path, encoding="utf-8").read()
            except Exception:
                continue
            for i, ch in enumerate(chunk_text(txt)):
                docs.append((rel, i, ch))
    return docs


async def main():
    provider = FastEmbedProvider(MODEL)
    vname, vsize = provider.get_vector_name(), provider.get_vector_size()
    client = AsyncQdrantClient(url=QDRANT_URL, api_key=API_KEY)

    # garante o schema CERTO (vetor nomeado); recria se incompatível (conserta a collection do curl)
    recreate = True
    if await client.collection_exists(COLLECTION):
        vectors = (await client.get_collection(COLLECTION)).config.params.vectors
        if isinstance(vectors, dict) and vname in vectors:
            recreate = False
        else:
            print(
                f"[indexer] schema incompatível em {COLLECTION}; recriando", flush=True
            )
            await client.delete_collection(COLLECTION)
    if recreate:
        await client.create_collection(
            collection_name=COLLECTION,
            vectors_config={
                vname: models.VectorParams(size=vsize, distance=models.Distance.COSINE)
            },
        )
        print(
            f"[indexer] collection {COLLECTION} criada (vetor '{vname}', dim {vsize})",
            flush=True,
        )

    docs = collect_docs()
    if not docs:
        print(
            "[indexer] ERRO: 0 documentos — falhando de propósito (guarda anti-silent-failure)",
            flush=True,
        )
        sys.exit(1)

    total = 0
    for s in range(0, len(docs), BATCH):
        batch = docs[s : s + BATCH]
        vecs = await provider.embed_documents(
            [d[2] for d in batch]
        )  # passage_embed (prefixo automático)
        points = [
            models.PointStruct(
                id=str(uuid.uuid5(NS, f"{rel}#{i}")),  # determinístico -> idempotente
                vector={vname: v},
                payload={
                    "document": ch,
                    "metadata": {"path": rel, "chunk": i, "run_id": RUN_ID},
                },
            )
            for (rel, i, ch), v in zip(batch, vecs)
        ]
        await client.upsert(collection_name=COLLECTION, points=points)
        total += len(points)
    print(f"[indexer] upsert de {total} chunks (run {RUN_ID})", flush=True)

    # GC: remove chunks de runs antigas (docs apagados/encolhidos)
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
    print(f"[indexer] GC ok; run corrente {RUN_ID}", flush=True)

    with open("/workspace/indexed_count", "w") as f:
        f.write(str(total))


if __name__ == "__main__":
    asyncio.run(main())
