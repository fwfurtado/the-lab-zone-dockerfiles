"""Núcleo compartilhado dos indexers de triagem (Fase D).

Contém tudo que gestalt e facets têm em comum: parse do front-matter, corte do
preâmbulo, carga do modelo de embedding, e o reconcile (garantir collection,
upsert em batch, GC por run_id, guarda anti-silent-failure).

A ÚNICA coisa que os dois modos NÃO compartilham é como transformar
(front_matter, body) numa lista de pontos — gestalt faz um ponto do corpo
inteiro, facets fatia por seção. Essa função é injetada em `reconcile` como o
parâmetro `build_points`. Todo o resto vive aqui, num lugar só — inclusive o
parser de front-matter, que já teve bugs e não deve divergir entre modos.

Corte de preâmbulo é SCAN de fronteira (pula até o 1º cabeçalho), não parser de
markdown: encontrar onde o documento começa é achar uma linha divisória, não
entender a árvore. O parser de markdown (mistune) aparece só no facets, que
precisa da ESTRUTURA hierárquica das seções — não aqui.
"""

from __future__ import annotations

import glob
import os
import sys
import uuid
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

# Imports PESADOS adiados. O `classifier` importa este módulo só pelo parser e
# pelo Report — não fala com o Qdrant nem embeda nada. Mantê-los no topo faria o
# pod do classificador carregar o fastembed (e o onnxruntime junto), centenas de
# MB de RAM e segundos de start, para nada. `from __future__ import annotations`
# torna as anotações strings, então o TYPE_CHECKING basta para os tipos.
if TYPE_CHECKING:
    from qdrant_client import AsyncQdrantClient, models

# Namespace uuid5 (URL) — ids de ponto determinísticos e estáveis entre runs.
NS = uuid.UUID("6ba7b811-9dad-11d1-80b4-00c04fd430c8")


@dataclass(frozen=True)
class Config:
    """Config lida do ambiente. Cada entrypoint constrói a sua (collection,
    campos filtráveis) e reusa os defaults comuns (Qdrant, modelo, fonte)."""

    qdrant_url: str
    api_key: str | None
    collection: str
    model: str
    repo_dir: str
    globs: list[str]
    # Onde estão as conclusões (artefato irmão, ADR-0013). Prefixo separado do
    # dos relatórios: o glob dos relatórios NÃO pode varrê-las.
    conclusion_globs: list[str]
    batch: int
    run_id: str
    # Caminho do arquivo de contagem (output do step Argo). Configurável para
    # gestalt e facets no MESMO pod (containerSet) não colidirem no mesmo arquivo.
    count_path: str
    # Campos do payload que ganham índice no Qdrant (habilitam filtro do Tier 0/1).
    payload_indexes: dict[str, models.PayloadSchemaType]

    @staticmethod
    def from_env(
        collection_default: str,
        payload_indexes: dict[str, models.PayloadSchemaType],
    ) -> Config:
        return Config(
            qdrant_url=os.environ["QDRANT_URL"],
            api_key=os.environ.get("QDRANT_API_KEY"),
            collection=os.environ.get("COLLECTION_NAME", collection_default),
            model=os.environ.get("EMBEDDING_MODEL", "intfloat/multilingual-e5-large"),
            repo_dir=os.environ.get("REPO_DIR", "/workspace/repo"),
            globs=os.environ.get("DOC_GLOBS", "triage/**/*.md").split(","),
            conclusion_globs=os.environ.get(
                "CONCLUSION_GLOBS", "conclusions/**/*.md"
            ).split(","),
            batch=int(os.environ.get("EMBED_BATCH", "8")),
            run_id=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
            count_path=os.environ.get("INDEXED_COUNT_PATH", "/workspace/indexed_count"),
            payload_indexes=payload_indexes,
        )


@dataclass(frozen=True)
class Report:
    """Um relatório parseado: front-matter + corpo limpo (sem preâmbulo).

    `path` é o caminho de onde foi lido. O classificador o usa para espelhar
    `triage/…` -> `conclusions/…` sem reconstruir a chave (ADR-0013).
    """

    dedup_key: str
    front_matter: dict
    body: str
    path: str = ""


@dataclass(frozen=True)
class Point:
    """Um ponto a indexar. `suffix` distingue múltiplos pontos do mesmo relatório
    (ex.: facetas) — vazio para um-ponto-por-relatório (gestalt)."""

    suffix: str  # "" para gestalt; "symptom"/"evidence"/... para facets
    text: str  # o que se embeda
    extra_payload: dict  # campos além dos comuns (ex.: {"section": "evidence"})


_ESCAPES = {'"': '"', "\\": "\\", "n": "\n", "t": "\t", "r": "\r"}


def _unescape(s: str) -> str:
    """Desfaz o escape em UMA passada, da esquerda para a direita.

    Substituições em cadeia (`s.replace(r'\\t', '\t').replace(r'\\\\', '\\')`)
    corrompem a barra invertida: em `C:\\\\tmp` (barra escapada + 't'), o
    `\\t` casa antes de a barra ser resolvida e vira TAB. Uma passada só,
    consumindo o par, não tem esse acoplamento entre regras.
    """
    out: list[str] = []
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c == "\\" and i + 1 < n and s[i + 1] in _ESCAPES:
            out.append(_ESCAPES[s[i + 1]])
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _unquote(s: str) -> str:
    """Remove aspas duplas externas e desfaz o escape mínimo do yamlString Go."""
    s = s.strip()
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        return _unescape(s[1:-1])
    return s


def parse_document(text: str) -> tuple[dict, str]:
    """Separa (front_matter, body) de um .md de triagem.

    Front-matter: o YAML restrito que a borda Go emite (garage_document.go) —
    'chave: valor' por linha, aspas duplas, listas '  - "item"'. Parseado sem
    PyYAML (casa exatamente o formato emitido, sem dep na imagem).

    Body: corta o preâmbulo do modelo pulando até o primeiro cabeçalho markdown.
    """
    lines = text.splitlines()
    fm: dict = {}
    body_start = 0

    if lines and lines[0].strip() == "---":
        i = 1
        pending_list_key = None
        while i < len(lines) and lines[i].strip() != "---":
            stripped = lines[i].strip()
            if stripped.startswith("- "):
                if pending_list_key is not None:
                    fm[pending_list_key].append(_unquote(stripped[2:].strip()))
            elif ":" in stripped:
                key, _, val = stripped.partition(":")
                key = key.strip()
                val = val.strip()
                if val == "" or val == "[]":
                    fm[key] = []
                    pending_list_key = key if val == "" else None
                else:
                    fm[key] = _unquote(val)
                    pending_list_key = None
            i += 1
        body_start = i + 1

    body_lines = lines[body_start:]
    header_idx = next(
        (idx for idx, ln in enumerate(body_lines) if ln.lstrip().startswith("#")),
        None,
    )
    body = "\n".join(body_lines[header_idx:] if header_idx is not None else body_lines)
    return fm, body.strip()


def collect_reports_from(repo_dir: str, globs: list[str], tag: str = "indexer") -> list[Report]:
    """Lê e parseia cada .md sob repo_dir. Pula relatórios sem dedup_key (não
    indexáveis de forma idempotente) ou sem corpo, com aviso.

    Separado de `collect_reports(cfg)` para que o classificador — que não tem
    Config de Qdrant — reuse o MESMO parser (ADR-0010: o parser vive num lugar
    só, e já teve bugs; não deve divergir entre consumidores).
    """
    reports: list[Report] = []
    for g in globs:
        for path in glob.glob(os.path.join(repo_dir, g.strip()), recursive=True):
            if not os.path.isfile(path):
                continue
            try:
                txt = open(path, encoding="utf-8").read()
            except OSError:
                continue
            fm, body = parse_document(txt)
            dedup = fm.get("dedup_key") or fm.get("incident_key")
            if not dedup:
                print(f"[{tag}] AVISO: sem dedup_key, pulando {path}", flush=True)
                continue
            if not body:
                print(f"[{tag}] AVISO: corpo vazio, pulando {path}", flush=True)
                continue
            reports.append(Report(dedup_key=dedup, front_matter=fm, body=body, path=path))
    return reports


def collect_reports(cfg: Config) -> list[Report]:
    """Coleta os relatórios da Config do indexer."""
    return collect_reports_from(cfg.repo_dir, cfg.globs)


def load_conclusions(repo_dir: str, globs: list[str], tag: str = "indexer") -> dict[str, dict]:
    """Lê os `.md` de conclusão e devolve {dedup_key: campos_de_payload}.

    O artefato de conclusão é irmão do relatório no Garage (ADR-0013), colado
    pela `dedup_key`. Reusa o MESMO parser de front-matter do relatório — os dois
    têm a mesma anatomia, e o parser vive num lugar só (ADR-0010).

    Conclusão malformada é PULADA COM AVISO, não derruba a indexação: o relatório
    ainda entra, só sem verdict/confidence. E `from_front_matter` valida a união
    discriminada, então um artefato incoerente (diagnosed sem verdict) é barulho,
    não payload torto no Qdrant.
    """
    from triage_indexer.conclusions import from_front_matter, payload_fields

    out: dict[str, dict] = {}
    for g in globs:
        for path in glob.glob(os.path.join(repo_dir, g.strip()), recursive=True):
            if not os.path.isfile(path):
                continue
            try:
                fm, _ = parse_document(open(path, encoding="utf-8").read())
                dedup = fm.get("dedup_key")
                if not dedup:
                    print(f"[{tag}] AVISO: conclusão sem dedup_key, pulando {path}", flush=True)
                    continue
                out[dedup] = payload_fields(from_front_matter(fm))
            except Exception as e:  # noqa: BLE001 — uma conclusão ruim não derruba o corpus
                print(f"[{tag}] AVISO: conclusão inválida em {path}: {e}", flush=True)
    return out


def base_payload(cfg: Config, r: Report, conclusion: dict | None = None) -> dict:
    """Payload comum a todo ponto: os fatos do front-matter + run_id. Os modos
    acrescentam o que é seu (ex.: facets adiciona 'section').

    `conclusion` são os campos derivados do artefato irmão (outcome/verdict/
    confidence). Quando ausente — relatório recém-triado, ainda não classificado —
    os campos simplesmente NÃO entram no payload. O filtro por confiança não o
    alcança até a conclusão existir; nada bloqueia, nada falha.
    """
    fm = r.front_matter
    payload = {
        "document": None,  # preenchido por ponto (o texto embedado)
        "metadata": {
            "dedup_key": r.dedup_key,
            "incident_key": fm.get("incident_key", r.dedup_key),
            "namespace": fm.get("namespace", ""),
            "alertnames": fm.get("alertnames", []),
            "fired_at": fm.get("fired_at", ""),
            "triaged_at": fm.get("triaged_at", ""),
            # Gancho da decisão 4: permite ao Tier 1 despriorizar refutados.
            "confirmation": fm.get("confirmation", "unverified"),
            "run_id": cfg.run_id,
        },
    }
    if conclusion:
        payload["metadata"].update(conclusion)
    return payload


# build_points: dado um Report, produz os pontos daquele relatório. É o ÚNICO
# ponto de variação entre gestalt (1 ponto) e facets (N pontos por seção).
BuildPoints = Callable[[Report], Iterable[Point]]


async def _ensure_collection(client: AsyncQdrantClient, cfg: Config, vname: str, vsize: int) -> bool:
    """Garante a collection com o vetor nomeado e os índices de payload. Recria
    se o schema do vetor for incompatível. Retorna True se (re)criou."""
    from qdrant_client import models

    recreate = True
    if await client.collection_exists(cfg.collection):
        vectors = (await client.get_collection(cfg.collection)).config.params.vectors
        if isinstance(vectors, dict) and vname in vectors:
            recreate = False
        else:
            print(f"[indexer] schema incompatível em {cfg.collection}; recriando", flush=True)
            await client.delete_collection(cfg.collection)
    if recreate:
        await client.create_collection(
            collection_name=cfg.collection,
            vectors_config={vname: models.VectorParams(size=vsize, distance=models.Distance.COSINE)},
        )
        print(f"[indexer] collection {cfg.collection} criada (vetor '{vname}', dim {vsize})", flush=True)

    # Índices de payload: sem eles o Qdrant não filtra (ou filtra por scan) nos
    # campos do Tier 0/1. Idempotente — criar um índice que já existe é no-op.
    for field, schema in cfg.payload_indexes.items():
        try:
            await client.create_payload_index(
                collection_name=cfg.collection, field_name=field, field_schema=schema
            )
        except Exception as e:  # noqa: BLE001 — índice já existente não deve abortar
            print(f"[indexer] índice de payload {field}: {e}", flush=True)
    return recreate


def _report_enrichment(reports: list[Report], conclusions: dict[str, dict], tag: str = "indexer") -> None:
    """Diz em voz alta quantos relatórios ganharam conclusão.

    Sem isto, um enriquecimento que não aconteceu (prefixo `conclusions/` não
    baixado, glob errado, classificador não rodou) é INDISTINGUÍVEL, no Qdrant, de
    um corpus legitimamente não classificado: em ambos os casos o payload só não
    tem `outcome`. O sintoma aparece dias depois, num filtro que não casa nada.
    """
    if not reports:
        return
    enriched = sum(1 for r in reports if r.dedup_key in conclusions)
    print(
        f"[{tag}] conclusões: {len(conclusions)} carregadas; "
        f"{enriched}/{len(reports)} relatórios enriquecidos",
        flush=True,
    )
    if not conclusions:
        print(
            f"[{tag}] AVISO: 0 conclusões para {len(reports)} relatórios. "
            "O classificador rodou? O prefixo conclusions/ foi baixado? "
            "Os pontos serão indexados SEM outcome/verdict/confidence.",
            flush=True,
        )
    # Órfãs: conclusão sem relatório correspondente. Sinaliza corpus dessincronizado
    # (relatório deletado do bucket, ou dedup_key divergente entre os artefatos).
    orphans = set(conclusions) - {r.dedup_key for r in reports}
    if orphans:
        print(f"[{tag}] AVISO: {len(orphans)} conclusões sem relatório irmão", flush=True)


async def reconcile(cfg: Config, build_points: BuildPoints) -> int:
    """Reconcilia repo_dir -> collection. Comum aos dois modos; o que muda é
    build_points. Retorna o número de pontos upsertados."""
    from mcp_server_qdrant.embeddings.fastembed import FastEmbedProvider
    from qdrant_client import AsyncQdrantClient, models

    provider = FastEmbedProvider(cfg.model)
    vname, vsize = provider.get_vector_name(), provider.get_vector_size()
    client = AsyncQdrantClient(url=cfg.qdrant_url, api_key=cfg.api_key)

    recreate = await _ensure_collection(client, cfg, vname, vsize)

    reports = collect_reports(cfg)
    # As conclusões são artefato irmão no Garage (ADR-0013), coladas pela
    # dedup_key. Relatório sem conclusão é indexado sem verdict/confidence —
    # degradação suave, o filtro por confiança simplesmente não o alcança.
    conclusions = load_conclusions(cfg.repo_dir, cfg.conclusion_globs)
    _report_enrichment(reports, conclusions)

    # Materializa (report, point) para todos os pontos de todos os relatórios.
    pending: list[tuple[Report, Point]] = [
        (r, p) for r in reports for p in build_points(r)
    ]

    if not pending:
        # Guarda anti-silent-failure: 0 pontos com collection já populada é
        # provável falha da fonte (rclone vazio) — abortar protege o GC de
        # limpar tudo. Na primeira run (recém-criada) 0 é legítimo.
        if recreate:
            print("[indexer] 0 pontos na primeira run (corpus vazio); ok", flush=True)
            _write_count(cfg.count_path, 0)
            return 0
        print("[indexer] ERRO: 0 pontos com collection populada — fonte vazia? Abortando", flush=True)
        sys.exit(1)

    total = 0
    for s in range(0, len(pending), cfg.batch):
        batch = pending[s : s + cfg.batch]
        vecs = await provider.embed_documents([p.text for _, p in batch])
        points = []
        for (r, p), v in zip(batch, vecs):
            payload = base_payload(cfg, r, conclusions.get(r.dedup_key))
            payload["document"] = p.text
            payload["metadata"].update(p.extra_payload)
            pid = uuid.uuid5(NS, r.dedup_key if not p.suffix else f"{r.dedup_key}#{p.suffix}")
            points.append(models.PointStruct(id=str(pid), vector={vname: v}, payload=payload))
        await client.upsert(collection_name=cfg.collection, points=points)
        total += len(points)
    print(f"[indexer] upsert de {total} pontos em {cfg.collection} (run {cfg.run_id})", flush=True)

    await client.delete(
        collection_name=cfg.collection,
        points_selector=models.FilterSelector(
            filter=models.Filter(
                must_not=[
                    models.FieldCondition(
                        key="metadata.run_id", match=models.MatchValue(value=cfg.run_id)
                    )
                ]
            )
        ),
    )
    print(f"[indexer] GC ok; run corrente {cfg.run_id}", flush=True)

    _write_count(cfg.count_path, total)
    return total


def _write_count(path: str, n: int) -> None:
    try:
        with open(path, "w") as f:
            f.write(str(n))
    except OSError:
        pass  # fora do Argo (teste local) não há /workspace; não é erro


def run(build: Callable[[], Awaitable[int]]) -> None:
    """Executa um coroutine de reconcile. Usado pelos entrypoints."""
    import asyncio

    asyncio.run(build())
