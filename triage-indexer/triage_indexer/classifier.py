"""Classificador de conclusões (Fatia B.1, ADR-0013).

Lê os relatórios de triagem (`triage/**.md`, imutáveis) e escreve, para cada um,
um `.md` de conclusão irmão (`conclusions/**.md`, regenerável) com o veredito e a
confiança extraídos. NUNCA toca o Qdrant — só produz artefato. O indexer, depois,
lê os dois e monta o payload; ele segue o único escritor do índice.

Isso preserva a invariante central: o Qdrant é 100% derivável do Garage e pode
ser destruído e reconstruído SEM reclassificar (o LLM não roda de novo).

Por que um agente Pydantic AI e não uma regex: a saída do agente de triagem é
semântica, não estruturada — a confiança aparece ora em seção dedicada, ora
inline, ora com qualificador parentético, ora estratificada por afirmação
(ADR-0008). O `output_type` com union discriminado dá validação de schema e
retry automático quando o modelo viola o schema; a regex quebra a cada forma nova.

Por que NÃO um framework multi-agente: isto não é um workload de agente. Não há
loop, tools nem autonomia — é uma função tipada implementada por um LLM. A
garantia de tipo é o requisito, não um detalhe.

CLI (mecânica inspirada em `docker --build-arg K=V`):

    classify                                  # incremental: só triagem sem conclusão irmã
    classify --reclassify                     # tudo, sobrescreve
    classify --reclassify dedup-key=a102e854  # só esse, sobrescreve

O filtro é SUBORDINADO ao --reclassify: não existe filtrar sem reprocessar, então
o estado inválido é inexpressável (não há o que validar em runtime). Regenerar é
sobrescrita idempotente (PUT), nunca `rm` no bucket — que também contém as
triagens imutáveis.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from triage_indexer._common import Report, collect_reports_from
from triage_indexer.conclusions import (
    Conclusion,
    Diagnosed,
    Inconclusive,
    conclusion_path_for,
    to_markdown,
)
from triage_indexer.observability import (
    configure_observability,
    domain_baggage,
    get_tracer,
    litellm_cost_http_client,
)

TAG = "classifier"

INSTRUCTIONS = """\
Você lê um RELATÓRIO DE TRIAGEM de incidentes de infraestrutura (em PT-BR) e
extrai duas informações sobre ele. Você não investiga nem opina sobre o
incidente: você apenas lê o que o relatório afirma.

O relatório segue esta estrutura: Sintoma, Evidência, Causa provável, Próximo
passo, Confiança.

Decida entre DOIS resultados possíveis:

1. DIAGNOSED — o relatório apontou uma causa provável.
   - `verdict`: a causa primária, em UMA frase de no máximo 200 caracteres.
     Descreva a causa, não o sintoma.
   - `confidence`: a confiança do DIAGNÓSTICO PRIMÁRIO.

2. INCONCLUSIVE — o relatório NÃO chegou a uma causa (ex.: afirma "não há dado
   suficiente", ou lista apenas hipóteses descartadas sem eleger uma causa).
   - `reason`: por que não houve diagnóstico, em UMA frase de até 200 caracteres.

REGRA CRÍTICA SOBRE A CONFIANÇA: extraia a confiança do diagnóstico PRIMÁRIO — a
causa que o relatório elegeu. NÃO use o mínimo global entre as afirmações do
documento. Ressalvas sobre evidência AUXILIAR ("a métrica não expõe label de
pod", "o PSI não veio na consulta", "não consegui ler as CNPs") NÃO rebaixam a
confiança da conclusão quando o relatório sustenta a causa por outras fontes.
Se o relatório declara explicitamente sua confiança (campo "Confiança: alta"),
essa é a confiança do diagnóstico primário — mapeie alta->high, média->medium,
baixa->low.

Um relatório com causa eleita e confiança baixa é DIAGNOSED com
`confidence: low` — NÃO é inconclusive. Inconclusive é a ausência de causa, não
a pouca certeza sobre uma causa.

Em `rationale`, explique em poucas linhas o que no relatório sustenta a sua
leitura (o trecho que elege a causa, o campo de confiança, a ressalva que você
decidiu não considerar). Escreva em PT-BR.
"""


@dataclass(frozen=True)
class Config:
    repo_dir: str
    triage_globs: list[str]
    model: str
    base_url: str
    api_key: str
    retries: int
    concurrency: int

    @staticmethod
    def from_env() -> Config:
        return Config(
            repo_dir=os.environ.get("REPO_DIR", "/workspace/repo"),
            triage_globs=os.environ.get("TRIAGE_GLOBS", "triage/**/*.md").split(","),
            model=os.environ.get("CLASSIFIER_MODEL", "minimax-m3-paid"),
            base_url=os.environ.get("LITELLM_BASE_URL", "http://litellm.ai.svc.cluster.local:4000/v1"),
            api_key=os.environ.get("LITELLM_API_KEY", ""),
            # Retry do Pydantic AI quando o modelo viola o schema: devolve o erro
            # de validação ao modelo e pede de novo. É o que torna seguro rodar
            # sobre o corpus inteiro sem ninguém olhando.
            retries=int(os.environ.get("CLASSIFIER_RETRIES", "2")),
            concurrency=int(os.environ.get("CLASSIFIER_CONCURRENCY", "4")),
        )


# ---------------------------------------------------------------------------
# Filtros: ponto de extensão. Adicionar `namespace` ou `since` é registrar uma
# entrada aqui — o parsing e a assinatura do CLI não mudam.
# A chave é o nome na CLI (hífen, idiomático); o predicado fala o domínio.
# ---------------------------------------------------------------------------
FILTERS: dict[str, Callable[[Report, str], bool]] = {
    "dedup-key": lambda r, v: r.dedup_key == v,
}


@dataclass(frozen=True)
class Reclassify:
    """Escopo do reprocessamento. `key is None` significa 'tudo'."""

    key: str | None = None
    value: str | None = None

    def matches(self, r: Report) -> bool:
        if self.key is None:
            return True
        return FILTERS[self.key](r, self.value or "")

    def describe(self) -> str:
        return "tudo" if self.key is None else f"{self.key}={self.value}"


def parse_reclassify(raw: list[str] | None) -> Reclassify | None:
    """Traduz o valor de `--reclassify` (nargs='*') no escopo.

    None  -> flag ausente: modo incremental.
    []    -> `--reclassify`: reprocessa tudo.
    [k=v] -> `--reclassify k=v`: reprocessa o subconjunto.
    2+    -> erro: apenas um filtro por vez (não definimos semântica de combinação).
    """
    if raw is None:
        return None
    if not raw:
        return Reclassify()
    if len(raw) > 1:
        keys = ", ".join(a.split("=", 1)[0] for a in raw)
        raise SystemExit(f"[{TAG}] ERRO: apenas um filtro por vez; recebi {len(raw)} ({keys})")

    key, sep, value = raw[0].partition("=")
    if not sep or not value:
        raise SystemExit(f"[{TAG}] ERRO: filtro deve ser chave=valor; recebi {raw[0]!r}")
    if key not in FILTERS:
        valid = ", ".join(sorted(FILTERS))
        raise SystemExit(f"[{TAG}] ERRO: filtro desconhecido {key!r}; válidos: {valid}")
    return Reclassify(key=key, value=value)


def select(reports: list[Report], repo_dir: str, rc: Reclassify | None) -> list[Report]:
    """Quais relatórios processar.

    Incremental (rc is None): os que não têm conclusão irmã. Note que um
    relatório cuja classificação FALHOU não escreveu conclusão — então a próxima
    run o pega de novo. As falhas se auto-curam sem estado extra.

    Com --reclassify: os que casam o escopo, independente de já terem conclusão
    (é justamente o ponto de reprocessar).
    """
    if rc is None:
        return [r for r in reports if not os.path.exists(conclusion_path_for(r.path, repo_dir))]
    return [r for r in reports if rc.matches(r)]


def build_agent(cfg: Config) -> Agent:
    model = OpenAIChatModel(
        cfg.model,
        provider=OpenAIProvider(
            base_url=cfg.base_url,
            api_key=cfg.api_key or "unused",
            # Hook que lê o custo EFETIVO do header do LiteLLM e o anexa como
            # gen_ai.usage.cost no span de generation. Um `--reclassify` sobre o
            # corpus inteiro passa a ter preço visível e atribuível por incidente.
            http_client=litellm_cost_http_client(),
        ),
    )
    return Agent(
        model,
        output_type=Conclusion,
        instructions=INSTRUCTIONS,
        # Temperatura 0 + saída constrangida ao schema mitigam o não-determinismo
        # da 2ª passada (ADR-0008).
        model_settings={"temperature": 0.0},
        retries=cfg.retries,
    )


def _trace_attrs(r: Report, run_id: str) -> dict[str, str]:
    """Baggage de domínio do relatório, na convenção da borda Go (pipeline.go).

    `langfuse.session.id` = dedup_key faz a classificação cair na MESMA sessão da
    triagem do incidente: a história completa (triagem -> conclusão) num lugar só,
    com o custo somável de ponta a ponta.
    """
    fm = r.front_matter
    alertnames = fm.get("alertnames") or []
    alert = ",".join(alertnames) if isinstance(alertnames, list) else str(alertnames)
    return {
        "langfuse.session.id": r.dedup_key,
        # Nome de trace de BAIXA cardinalidade (o alertname é enum de fato).
        "langfuse.trace.name": f"classify:{alert}" if alert else "classify",
        "thelabzone.alertname": alert,
        "thelabzone.namespace": str(fm.get("namespace", "")),
        # Permite filtrar "o que esta run do job classificou" no Langfuse sem
        # agrupar incidentes distintos no mesmo trace.
        "thelabzone.run_id": run_id,
    }


async def classify_one(agent: Agent, r: Report) -> Diagnosed | Inconclusive:
    result = await agent.run(r.body)
    return result.output


def write_conclusion(r: Report, c: Diagnosed | Inconclusive, repo_dir: str) -> str:
    """Escreve o `.md` de conclusão. Sobrescrever é esperado (regenerável)."""
    path = conclusion_path_for(r.path, repo_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(to_markdown(c, r.dedup_key))
    return path


async def run_classification(cfg: Config, rc: Reclassify | None) -> int:
    """Retorna o código de saída. Ver política de falha no fim do módulo."""
    reports = collect_reports_from(cfg.repo_dir, cfg.triage_globs, tag=TAG)
    if not reports:
        print(f"[{TAG}] ERRO: 0 relatórios em {cfg.repo_dir} — fonte vazia?", flush=True)
        return 1

    selected = select(reports, cfg.repo_dir, rc)
    skipped = len(reports) - len(selected)

    scope = rc.describe() if rc else "incremental"
    print(f"[{TAG}] corpus: {len(reports)} relatórios | escopo: {scope}", flush=True)

    # Filtro que não casa nada é quase sempre erro de digitação num hash de 32
    # chars. Falha barulhenta, como a guarda anti-silent-failure do indexer.
    if rc is not None and rc.key is not None and not selected:
        print(f"[{TAG}] ERRO: nenhum relatório casou {rc.describe()}", flush=True)
        return 1

    if not selected:
        print(f"[{TAG}] nada a fazer: {skipped} já têm conclusão (use --reclassify)", flush=True)
        return 0

    agent = build_agent(cfg)
    tracer = get_tracer()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    sem = asyncio.Semaphore(cfg.concurrency)
    failures: list[tuple[str, Exception]] = []
    done = 0

    async def work(r: Report) -> None:
        nonlocal done
        async with sem:
            # Um trace POR RELATÓRIO (o job não recebe traceparent — ele enraíza).
            # A baggage entra antes do span para o BaggageSpanProcessor copiá-la
            # em cada span filho, inclusive o de generation que o Pydantic AI cria.
            with domain_baggage(**_trace_attrs(r, run_id)):
                with tracer.start_as_current_span("classify") as span:
                    try:
                        c = await classify_one(agent, r)
                    except Exception as e:  # noqa: BLE001 — um ruim não derruba o corpus
                        span.record_exception(e)
                        failures.append((r.dedup_key, e))
                        print(f"[{TAG}] FALHA em {r.dedup_key}: {e}", flush=True)
                        return
                    # Resultado no span raiz (não em baggage: não é contexto a
                    # propagar, é o que ESTE span produziu).
                    span.set_attribute("thelabzone.outcome", c.outcome)
                    if isinstance(c, Diagnosed):
                        span.set_attribute("thelabzone.confidence", c.confidence)
                    write_conclusion(r, c, cfg.repo_dir)
                    done += 1
                    print(f"[{TAG}] {r.dedup_key} -> {c.outcome}", flush=True)

    await asyncio.gather(*(work(r) for r in selected))

    print(
        f"[{TAG}] classificados: {done} | pulados: {skipped} | falhas: {len(failures)}",
        flush=True,
    )

    # Política de falha: uma falha isolada não bloqueia o resto (o relatório
    # ficará sem conclusão e a próxima run incremental o repesca). Mas TUDO
    # falhar é sistêmico (LLM fora do ar, credencial errada) e deve pintar o job
    # de vermelho.
    if failures and done == 0:
        print(f"[{TAG}] ERRO: todas as {len(failures)} classificações falharam", flush=True)
        return 1
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="triage-classify",
        description="Extrai verdict/confidence dos relatórios de triagem (ADR-0013).",
    )
    parser.add_argument(
        "--reclassify",
        nargs="*",
        default=None,
        metavar="CHAVE=VALOR",
        help=(
            "Reprocessa relatórios que já têm conclusão, sobrescrevendo. "
            "Sem argumento: tudo. Com CHAVE=VALOR: restringe o escopo "
            f"(chaves válidas: {', '.join(sorted(FILTERS))}). "
            "Sem a flag, o modo é incremental (só o que falta)."
        ),
    )
    args = parser.parse_args()
    rc = parse_reclassify(args.reclassify)
    # ANTES de qualquer run de agente: instrument_all() só afeta Agents criados
    # ou executados depois. No-op se OTEL_ENABLED=false.
    configure_observability()
    sys.exit(asyncio.run(run_classification(Config.from_env(), rc)))
