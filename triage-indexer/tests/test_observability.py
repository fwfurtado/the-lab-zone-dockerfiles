"""Testes da observabilidade do classificador.

Cobrem os DOIS contratos que não podem divergir do módulo dos agentes
(`services/core/shared/observability.py`): o nome do header de custo do LiteLLM e
os prefixos da baggage de domínio. Um teste que falha aqui significa que a cópia
divergiu da referência.

O resto (exporter, endpoint) é config do SDK e não vale mockar.
"""

import asyncio

import httpx
from opentelemetry import baggage, context
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from triage_indexer import observability as obs
from triage_indexer._common import Report
from triage_indexer.classifier import _trace_attrs

# --------------------------------------------------------------------------
# Contrato 1: o header de custo do LiteLLM
# --------------------------------------------------------------------------


def _hook_of(client: httpx.AsyncClient):
    return client.event_hooks["response"][0]


def _recording_span(exporter: InMemorySpanExporter):
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider.get_tracer("t")


def test_header_de_custo_e_o_do_litellm():
    # Se o LiteLLM renomear o header, este teste é o alarme.
    assert obs.LITELLM_COST_HEADER == "x-litellm-response-cost"


def test_custo_efetivo_vira_atributo_no_span():
    exporter = InMemorySpanExporter()
    tracer = _recording_span(exporter)
    client = obs.litellm_cost_http_client()
    hook = _hook_of(client)

    with tracer.start_as_current_span("generation"):
        resp = httpx.Response(200, headers={obs.LITELLM_COST_HEADER: "0.005534"})
        asyncio.run(hook(resp))

    (span,) = exporter.get_finished_spans()
    assert span.attributes["gen_ai.usage.cost"] == 0.005534


def test_custo_ausente_nao_quebra():
    exporter = InMemorySpanExporter()
    tracer = _recording_span(exporter)
    hook = _hook_of(obs.litellm_cost_http_client())

    with tracer.start_as_current_span("generation"):
        asyncio.run(hook(httpx.Response(200)))

    (span,) = exporter.get_finished_spans()
    assert "gen_ai.usage.cost" not in (span.attributes or {})


def test_custo_malformado_nao_quebra():
    exporter = InMemorySpanExporter()
    tracer = _recording_span(exporter)
    hook = _hook_of(obs.litellm_cost_http_client())

    with tracer.start_as_current_span("generation"):
        asyncio.run(hook(httpx.Response(200, headers={obs.LITELLM_COST_HEADER: "free"})))

    (span,) = exporter.get_finished_spans()
    assert "gen_ai.usage.cost" not in (span.attributes or {})


# --------------------------------------------------------------------------
# Contrato 2: a convenção de baggage de domínio
# --------------------------------------------------------------------------


def test_prefixos_de_baggage_sao_os_da_borda_go():
    assert obs.DOMAIN_BAGGAGE_PREFIXES == ("langfuse.", "thelabzone.")


def test_predicado_aceita_dominio_e_recusa_o_resto():
    assert obs._domain_baggage_key("langfuse.session.id")
    assert obs._domain_baggage_key("thelabzone.alertname")
    assert not obs._domain_baggage_key("user.email")  # baggage alheia não vaza


def test_domain_baggage_anexa_e_restaura_o_contexto():
    assert baggage.get_all(context.get_current()) == {}
    with obs.domain_baggage(**{"langfuse.session.id": "abc"}):
        assert baggage.get_baggage("langfuse.session.id") == "abc"
    assert baggage.get_all(context.get_current()) == {}  # detach devolve o contexto


def test_domain_baggage_ignora_valor_vazio():
    with obs.domain_baggage(**{"thelabzone.namespace": ""}):
        assert baggage.get_baggage("thelabzone.namespace") is None


# --------------------------------------------------------------------------
# Os atributos de domínio do relatório
# --------------------------------------------------------------------------


def _report(**fm) -> Report:
    return Report(dedup_key="a102e854", front_matter=fm, body="#", path="p")


def test_sessao_e_o_dedup_key_para_cair_na_sessao_da_triagem():
    """A classificação de um incidente vai para a MESMA sessão Langfuse da
    triagem dele (a borda Go usa dedup_key como session.id). É o que permite
    somar o custo de ponta a ponta por incidente."""
    attrs = _trace_attrs(_report(alertnames=["CiliumPolicyDrop"], namespace="data"), "run1")
    assert attrs["langfuse.session.id"] == "a102e854"


def test_trace_name_tem_baixa_cardinalidade():
    attrs = _trace_attrs(_report(alertnames=["TargetDown"]), "run1")
    assert attrs["langfuse.trace.name"] == "classify:TargetDown"


def test_trace_name_sem_alertname_nao_quebra():
    assert _trace_attrs(_report(), "run1")["langfuse.trace.name"] == "classify"


def test_run_id_permite_filtrar_a_execucao_do_job():
    # Sem agrupar incidentes distintos no mesmo trace (que é o custo de usar
    # session.id para isso).
    assert _trace_attrs(_report(), "20260708T030000Z")["thelabzone.run_id"] == "20260708T030000Z"


def test_alertnames_multiplos_viram_string_unica():
    attrs = _trace_attrs(_report(alertnames=["A", "B"], namespace="ai"), "r")
    assert attrs["thelabzone.alertname"] == "A,B"
    assert attrs["thelabzone.namespace"] == "ai"


def test_otel_desabilitado_e_no_op(monkeypatch):
    monkeypatch.setenv("OTEL_ENABLED", "false")
    assert not obs.otel_enabled()
    with obs.domain_baggage(**{"langfuse.session.id": "x"}):
        assert baggage.get_baggage("langfuse.session.id") is None


# --------------------------------------------------------------------------
# O contrato completo: a baggage chega em CADA span, inclusive na generation
# --------------------------------------------------------------------------


def test_baggage_vira_atributo_ate_no_span_de_generation():
    """O ponto inteiro do BaggageSpanProcessor.

    O Langfuse filtra e agrega por OBSERVAÇÃO, não só pela raiz do trace. Sem
    isto, `alertname` existiria só no span `classify` e um filtro por alerta
    perderia as generations — justamente onde mora o custo.
    """
    from opentelemetry.processor.baggage import BaggageSpanProcessor
    from pydantic_ai import Agent, InstrumentationSettings
    from pydantic_ai.messages import ModelResponse, ToolCallPart
    from pydantic_ai.models.function import FunctionModel

    from triage_indexer.classifier import Config, build_agent, classify_one

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(BaggageSpanProcessor(obs._domain_baggage_key))
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    def fn(msgs, info):
        tool = next(t for t in info.output_tools if t.name.endswith("Diagnosed"))
        return ModelResponse(
            parts=[ToolCallPart(tool.name, {"outcome": "diagnosed", "verdict": "v",
                                            "confidence": "high", "rationale": "r"})]
        )

    r = _report(alertnames=["CiliumPolicyDrop"], namespace="data")
    cfg = Config("/tmp", ["triage/**/*.md"], "stub", "http://stub/v1", "k", 1, 1)

    # tracer_provider explícito: não toca o provider global (isolamento entre testes).
    Agent.instrument_all(InstrumentationSettings(tracer_provider=provider, version=5))
    try:
        agent = build_agent(cfg)
        with obs.domain_baggage(**_trace_attrs(r, "run1")):
            with provider.get_tracer("t").start_as_current_span("classify"):
                with agent.override(model=FunctionModel(fn)):
                    asyncio.run(classify_one(agent, r))
    finally:
        Agent.instrument_all(False)

    spans = exporter.get_finished_spans()
    assert len(spans) >= 2, "esperado ao menos generation + classify"
    for span in spans:
        attrs = span.attributes or {}
        assert attrs.get("langfuse.session.id") == "a102e854", f"span {span.name} sem sessão"
        assert attrs.get("thelabzone.alertname") == "CiliumPolicyDrop"

    # a generation (span do modelo) está entre eles
    assert any("chat" in s.name or "invoke_agent" in s.name for s in spans)
