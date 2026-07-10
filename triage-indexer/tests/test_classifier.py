"""Testes do classificador (ADR-0013): CLI, seleção e agente.

A matriz de seleção é a especificação executável do `--reclassify`. O agente é
exercitado com `FunctionModel` — sem LLM real, mas passando pelo `output_type`
(union discriminado), que é onde mora a garantia de tipo.
"""

import asyncio
import os

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models.function import AgentInfo, FunctionModel

from triage_indexer._common import Report
from triage_indexer.classifier import (
    FILTERS,
    INSTRUCTIONS,
    Config,
    Reclassify,
    build_agent,
    classify_one,
    parse_reclassify,
    select,
    write_conclusion,
)
from triage_indexer.conclusions import Diagnosed, Inconclusive, conclusion_path_for

# --------------------------------------------------------------------------
# parse_reclassify: a mecânica `docker --build-arg K=V`
# --------------------------------------------------------------------------


def test_flag_ausente_e_modo_incremental():
    assert parse_reclassify(None) is None


def test_flag_sem_argumento_reclassifica_tudo():
    rc = parse_reclassify([])
    assert rc == Reclassify()
    assert rc.key is None
    assert rc.describe() == "tudo"


def test_flag_com_filtro_restringe_escopo():
    rc = parse_reclassify(["dedup-key=a102e854"])
    assert rc == Reclassify(key="dedup-key", value="a102e854")
    assert rc.describe() == "dedup-key=a102e854"


def test_dois_filtros_sao_erro():
    # Não definimos semântica de combinação (AND?) — falhar é honesto.
    with pytest.raises(SystemExit) as e:
        parse_reclassify(["namespace=data", "dedup-key=x"])
    assert "apenas um filtro" in str(e.value)


def test_filtro_desconhecido_lista_os_validos():
    with pytest.raises(SystemExit) as e:
        parse_reclassify(["nemspace=data"])
    assert "dedup-key" in str(e.value)  # sugere as chaves válidas


def test_filtro_sem_valor_e_erro():
    with pytest.raises(SystemExit):
        parse_reclassify(["dedup-key"])
    with pytest.raises(SystemExit):
        parse_reclassify(["dedup-key="])


def test_registro_de_filtros_e_ponto_de_extensao():
    # Adicionar `namespace`/`since` deve ser registrar uma entrada, sem mexer
    # no parsing nem na assinatura do CLI.
    assert "dedup-key" in FILTERS
    r = Report(dedup_key="abc", front_matter={}, body="b", path="p")
    assert FILTERS["dedup-key"](r, "abc") is True
    assert FILTERS["dedup-key"](r, "outro") is False


# --------------------------------------------------------------------------
# select: a matriz de comportamento do design
# --------------------------------------------------------------------------


def _report(tmp_path, dedup: str) -> Report:
    p = tmp_path / "triage" / "data" / "Alert" / f"2026__{dedup}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("# corpo", encoding="utf-8")
    return Report(dedup_key=dedup, front_matter={}, body="# corpo", path=str(p))


def _com_conclusao(tmp_path, r: Report) -> None:
    path = conclusion_path_for(r.path, str(tmp_path))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "w").write("---\noutcome: diagnosed\n---\n\n# x")


def test_incremental_pula_quem_ja_tem_conclusao(tmp_path):
    a, b = _report(tmp_path, "aaa"), _report(tmp_path, "bbb")
    _com_conclusao(tmp_path, a)

    got = select([a, b], str(tmp_path), None)
    assert [r.dedup_key for r in got] == ["bbb"]


def test_incremental_repesca_falha_anterior(tmp_path):
    """Falha não escreve conclusão -> a próxima run incremental a repesca.
    As falhas se auto-curam sem estado extra."""
    a = _report(tmp_path, "aaa")  # sem conclusão: classificação falhou antes
    assert select([a], str(tmp_path), None) == [a]


def test_reclassify_tudo_ignora_conclusao_existente(tmp_path):
    a, b = _report(tmp_path, "aaa"), _report(tmp_path, "bbb")
    _com_conclusao(tmp_path, a)
    _com_conclusao(tmp_path, b)

    got = select([a, b], str(tmp_path), Reclassify())
    assert {r.dedup_key for r in got} == {"aaa", "bbb"}


def test_reclassify_com_filtro_pega_so_o_alvo_mesmo_com_conclusao(tmp_path):
    a, b = _report(tmp_path, "aaa"), _report(tmp_path, "bbb")
    _com_conclusao(tmp_path, a)

    got = select([a, b], str(tmp_path), Reclassify("dedup-key", "aaa"))
    assert [r.dedup_key for r in got] == ["aaa"]


def test_reclassify_com_filtro_pega_alvo_sem_conclusao(tmp_path):
    a = _report(tmp_path, "aaa")
    assert select([a], str(tmp_path), Reclassify("dedup-key", "aaa")) == [a]


# --------------------------------------------------------------------------
# O prompt carrega a regra crítica do ADR-0008
# --------------------------------------------------------------------------


def test_prompt_ancora_a_confianca_no_diagnostico_primario():
    assert "PRIMÁRIO" in INSTRUCTIONS
    assert "mínimo global" in INSTRUCTIONS


def test_prompt_distingue_confianca_baixa_de_inconclusive():
    # A confusão que o schema já impede, o prompt também previne.
    assert "confidence: low" in INSTRUCTIONS
    assert "ausência de causa" in INSTRUCTIONS


def test_prompt_mapeia_os_termos_pt_br_do_relatorio():
    assert "alta->high" in INSTRUCTIONS


# --------------------------------------------------------------------------
# O agente: output_type com union discriminado (sem LLM real)
# --------------------------------------------------------------------------


def _cfg(tmp_path) -> Config:
    return Config(
        repo_dir=str(tmp_path),
        triage_globs=["triage/**/*.md"],
        model="stub",
        base_url="http://stub/v1",
        api_key="k",
        retries=1,
        concurrency=1,
    )


def _fixed_output(payload: dict):
    """FunctionModel que responde com a variante pedida em `payload`.

    O Pydantic AI expõe UMA tool de saída por membro do union
    (`final_result_Diagnosed`, `final_result_Inconclusive`): a discriminação
    acontece na ESCOLHA DA TOOL, não num campo que o modelo poderia preencher
    errado. Garantia ainda mais forte que a do discriminador Pydantic.
    """

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        from pydantic_ai.messages import ToolCallPart

        want = "Diagnosed" if payload["outcome"] == "diagnosed" else "Inconclusive"
        tool = next(t for t in info.output_tools if t.name.endswith(want))
        return ModelResponse(parts=[ToolCallPart(tool.name, payload)])

    return FunctionModel(fn)


def test_union_expoe_uma_tool_de_saida_por_variante(tmp_path):
    agent = build_agent(_cfg(tmp_path))
    names: list[str] = []

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        from pydantic_ai.messages import ToolCallPart

        names.extend(t.name for t in info.output_tools)
        tool = next(t for t in info.output_tools if t.name.endswith("Diagnosed"))
        return ModelResponse(
            parts=[ToolCallPart(tool.name, {"outcome": "diagnosed", "verdict": "v",
                                            "confidence": "low", "rationale": "r"})]
        )

    with agent.override(model=FunctionModel(fn)):
        asyncio.run(classify_one(agent, _report(tmp_path, "x")))

    assert any(n.endswith("Diagnosed") for n in names)
    assert any(n.endswith("Inconclusive") for n in names)


def _classify(agent, report, payload):
    with agent.override(model=_fixed_output(payload)):
        return asyncio.run(classify_one(agent, report))


def test_agente_produz_diagnosed(tmp_path):
    out = _classify(
        build_agent(_cfg(tmp_path)),
        _report(tmp_path, "aaa"),
        {
            "outcome": "diagnosed",
            "verdict": "Pod drop-test negado pela CNP",
            "confidence": "high",
            "rationale": "três fontes convergem",
        },
    )
    assert isinstance(out, Diagnosed)
    assert out.confidence == "high"


def test_agente_produz_inconclusive(tmp_path):
    out = _classify(
        build_agent(_cfg(tmp_path)),
        _report(tmp_path, "bbb"),
        {
            "outcome": "inconclusive",
            "reason": "sem acesso às CNPs",
            "rationale": "o relatório declara não há dado suficiente",
        },
    )
    assert isinstance(out, Inconclusive)


def test_write_conclusion_escreve_no_prefixo_espelhado(tmp_path):
    r = _report(tmp_path, "aaa")
    c = Diagnosed(verdict="v", confidence="low", rationale="r")
    path = write_conclusion(r, c, str(tmp_path))

    assert path == conclusion_path_for(r.path, str(tmp_path))
    assert os.path.exists(path)
    assert "outcome: diagnosed" in open(path, encoding="utf-8").read()


def test_write_conclusion_sobrescreve(tmp_path):
    """Conclusão é regenerável: reescrever é o caminho do --reclassify (PUT
    idempotente), nunca um `rm` no bucket das triagens imutáveis."""
    r = _report(tmp_path, "aaa")
    write_conclusion(r, Diagnosed(verdict="v1", confidence="low", rationale="r"), str(tmp_path))
    p = write_conclusion(r, Diagnosed(verdict="v2", confidence="high", rationale="r"), str(tmp_path))

    txt = open(p, encoding="utf-8").read()
    assert "v2" in txt and "v1" not in txt


def test_prompt_proibe_cauda_de_juizo_no_verdict():
    """O modelo acrescentava 'a policy está funcionando como projetado' ao verdict:
    juízo sobre a triagem, não causa — e 41 chars a menos para a causa."""
    assert "SOMENTE a causa" in INSTRUCTIONS
    assert "funcionando como projetado" in INSTRUCTIONS  # o exemplo negativo


def test_prompt_pede_concisao_sem_delegar_contagem_ao_modelo():
    # Estilo no prompt (alvo ~150), teto duro no schema. O modelo não conta chars.
    assert "~150" in INSTRUCTIONS
    assert "200 caracteres" not in INSTRUCTIONS
