"""Observabilidade OTel do classificador (Fatia B.1).

Espelha `services/core/shared/observability.py` do repo `the-lab-zone-agents`,
que é a REFERÊNCIA desta arquitetura: a instrumentação vive na APLICAÇÃO, não no
gateway LiteLLM — o gateway vê "uma chamada de LLM aconteceu", não sabe que isto
é a classificação do incidente X.

Por que uma cópia mínima e não um pacote compartilhado: o classificador precisa
de um SUBCONJUNTO (não recebe traceparent de ninguém — é um job, ele enraíza o
próprio trace), vive em outro repo/imagem, e publicar um pacote para dois
consumidores é infra que ainda não se paga. O preço é divergência potencial.

DUAS COISAS NÃO PODEM DIVERGIR do módulo dos agentes, e por isso estão fixadas
aqui e cobertas por teste:
  1. O nome do header de custo do LiteLLM: `x-litellm-response-cost`.
  2. O prefixo da baggage de domínio: `langfuse.*` / `thelabzone.*`.
Se algum mudar lá, muda aqui. A versão do semconv é pinada explicitamente pelo
mesmo motivo (o default muda entre releases do Pydantic AI).

Traces do classificador caem no MESMO projeto Langfuse dos agentes, distinguidos
por `service.name` (OTEL_SERVICE_NAME=triage-classifier). Como a
`langfuse.session.id` é a `dedup_key` — a mesma convenção da borda Go —, a
classificação de um incidente aparece na MESMA sessão da triagem dele: a história
completa por incidente, com o custo somável de ponta a ponta.
"""

from __future__ import annotations

import atexit
import contextlib
import logging
import os
from collections.abc import Iterator
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import httpx

logger = logging.getLogger("triage_indexer.observability")

# Contrato com o LiteLLM. Muda lá, muda aqui (ver docstring).
LITELLM_COST_HEADER = "x-litellm-response-cost"

# Prefixos da baggage de domínio. Mesma convenção da borda Go (pipeline.go).
DOMAIN_BAGGAGE_PREFIXES = ("langfuse.", "thelabzone.")

_configured = False


def _domain_baggage_key(key: str) -> bool:
    """Só a baggage de domínio vira atributo de span — não qualquer baggage que
    porventura apareça no contexto."""
    return key.startswith(DOMAIN_BAGGAGE_PREFIXES)


def otel_enabled() -> bool:
    return os.environ.get("OTEL_ENABLED", "true").lower() not in ("false", "0", "no")


def configure_observability() -> None:
    """TracerProvider global + instrumentação de todos os Agents.

    Idempotente. No-op se OTEL_ENABLED=false — o job roda sem Collector em CI e
    localmente. Endpoint/protocolo vêm das envs PADRÃO do OTel
    (OTEL_EXPORTER_OTLP_ENDPOINT, OTEL_EXPORTER_OTLP_PROTOCOL), lidas pelo SDK:
    não reimplementamos essa lógica. Aponte para o Collector na :4318
    (http/protobuf), não :4317 (gRPC).
    """
    global _configured
    if _configured:
        return
    if not otel_enabled():
        logger.info("otel desabilitado (OTEL_ENABLED=false); sem tracing")
        _configured = True
        return

    # Imports pesados adiados: importar este módulo (teste, lint) não deve exigir
    # o stack OTel nem custar tempo — só quem chama configure() paga.
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.processor.baggage import BaggageSpanProcessor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from pydantic_ai import Agent, InstrumentationSettings

    resource = Resource.create(
        {
            "service.name": os.environ.get("OTEL_SERVICE_NAME", "triage-classifier"),
            "service.namespace": "the-lab-zone",
            "deployment.environment": os.environ.get("OTEL_ENVIRONMENT", "prod"),
        }
    )
    provider = TracerProvider(resource=resource)

    # Copia a baggage de domínio para atributo em CADA span (o de classificação e
    # os de model request que o Pydantic AI cria por baixo). O Langfuse filtra e
    # agrega por observação, não só pela raiz do trace — sem isto, `alertname` só
    # existiria no span raiz e o filtro perderia as generations.
    provider.add_span_processor(BaggageSpanProcessor(_domain_baggage_key))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)

    # O job é one-shot: sairia antes de o BatchSpanProcessor drenar. shutdown()
    # força o flush — sem isto os spans do último relatório se perdem.
    atexit.register(provider.shutdown)

    # version e include_content EXPLÍCITOS: pinar é não depender do default, que
    # muda entre releases do Pydantic AI. include_content=True captura o corpo do
    # relatório e a conclusão — é o que torna o trace auditável quando o
    # classificador erra (o `rationale` some do embedding, mas fica no trace).
    Agent.instrument_all(
        InstrumentationSettings(
            version=int(os.environ.get("OTEL_SEMCONV_VERSION", "5")),  # type: ignore[arg-type]
            include_content=True,
        )
    )

    _configured = True
    logger.info("otel configurado: service=%s", resource.attributes["service.name"])


def get_tracer():  # noqa: ANN201 — o tipo vive no SDK, importado tarde
    """Tracer do classificador. Sem provider configurado devolve o no-op do SDK,
    então chamar isto com OTEL_ENABLED=false é inócuo."""
    from opentelemetry import trace

    return trace.get_tracer("triage_indexer.classifier")


@contextlib.contextmanager
def domain_baggage(**kv: str) -> Iterator[None]:
    """Anexa baggage de domínio ao contexto corrente.

    Os spans criados aqui dentro (inclusive os que o Pydantic AI cria) recebem
    esses pares como atributos, via BaggageSpanProcessor. Chaves fora dos
    prefixos de domínio são ignoradas pelo processor — passá-las é inócuo, mas
    não faça: o predicado é a documentação executável da convenção.

    Seguro sob asyncio.gather: cada task copia o contexto na criação, então o
    attach/detach aqui dentro é por-task.
    """
    if not otel_enabled():
        yield
        return

    from opentelemetry import baggage, context

    ctx = context.get_current()
    for k, v in kv.items():
        if v:  # baggage vazia polui sem informar
            ctx = baggage.set_baggage(k, v, context=ctx)
    token = context.attach(ctx)
    try:
        yield
    finally:
        context.detach(token)


def litellm_cost_http_client() -> httpx.AsyncClient:
    """httpx.AsyncClient que captura o custo EFETIVO do LiteLLM.

    Lê `x-litellm-response-cost` da resposta e o anexa como `gen_ai.usage.cost`
    no span corrente — o custo autoritativo do gateway (inclui fallback/retry),
    não a estimativa que o Langfuse infere de tokens.

    É isto que dá controle de custo centralizado: `--reclassify` sobre o corpus
    inteiro passa a ter preço visível e atribuível por incidente (a sessão é a
    `dedup_key`).

    Notas herdadas do módulo dos agentes:
    - Só `gen_ai.usage.cost` é lido pelo Langfuse como custo FORNECIDO; o
      `langfuse.observation.cost_details` tem bug conhecido na ingestão OTLP.
    - O header vem em respostas NÃO-streaming. O classificador é não-streaming.
    """
    import httpx
    from opentelemetry import trace

    async def _capture_cost(response: httpx.Response) -> None:
        raw = response.headers.get(LITELLM_COST_HEADER)
        if not raw:
            return
        try:
            cost = float(raw)
        except ValueError:
            return
        span = trace.get_current_span()
        if span.is_recording():
            span.set_attribute("gen_ai.usage.cost", cost)

    return httpx.AsyncClient(event_hooks={"response": [_capture_cost]})
