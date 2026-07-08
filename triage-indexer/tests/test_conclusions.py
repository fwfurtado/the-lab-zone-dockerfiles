"""Testes do artefato de conclusão (ADR-0013).

O teste que mais importa é o ROUND-TRIP: o `.md` que o classificador emite tem
de ser lido de volta pelo MESMO parser restrito que lê o relatório de triagem
(`_common.parse_document`, sem PyYAML). Se a serialização divergir do parser, o
indexer lê lixo — e o ADR-0010 diz que o parser vive num lugar só.
"""

import os

import pytest
from pydantic import ValidationError

from triage_indexer import _common
from triage_indexer.conclusions import (
    Diagnosed,
    Inconclusive,
    conclusion_path_for,
    from_front_matter,
    payload_fields,
    to_markdown,
)


def _roundtrip(c, dedup="a102e854"):
    """Serializa e lê de volta com o parser do indexer."""
    md = to_markdown(c, dedup)
    fm, body = _common.parse_document(md)
    return fm, from_front_matter(fm, body)


def test_roundtrip_diagnosed():
    c = Diagnosed(
        verdict="Pod de teste drop-test gerando tráfego negado pela CNP",
        confidence="high",
        rationale="O relatório afirma Confiança: alta e sustenta com três fontes.",
    )
    fm, back = _roundtrip(c)

    assert fm["outcome"] == "diagnosed"
    assert fm["confidence"] == "high"
    assert fm["dedup_key"] == "a102e854"
    assert isinstance(back, Diagnosed)
    assert back.verdict == c.verdict
    assert back.confidence == "high"
    assert back.rationale == c.rationale


def test_roundtrip_inconclusive():
    c = Inconclusive(
        reason="Sem acesso às CNPs e métrica hubble ausente para o alvo",
        rationale="O agente declarou 'não há dado suficiente'.",
    )
    fm, back = _roundtrip(c)

    assert fm["outcome"] == "inconclusive"
    assert "confidence" not in fm  # inconclusive NÃO carrega confiança
    assert isinstance(back, Inconclusive)
    assert back.reason == c.reason


def test_roundtrip_escapa_caracteres_do_yaml_restrito():
    """O verdict real pode ter aspas, dois-pontos, barra e quebra de linha.

    O emissor precisa espelhar exatamente o `_unquote` do parser — barra
    invertida primeiro, depois aspas e controles.
    """
    c = Diagnosed(
        verdict='CNP nega "app: x" no path C:\\tmp\tcom tab',
        confidence="medium",
        rationale="qualquer",
    )
    _, back = _roundtrip(c)
    assert back.verdict == c.verdict


def test_roundtrip_verdict_com_dois_pontos():
    # 'chave: valor' é o separador do front-matter; um verdict com ':' não pode
    # confundir o parser (que faz partition no PRIMEIRO ':').
    c = Diagnosed(verdict="Causa: saturação de IO", confidence="low", rationale="r")
    _, back = _roundtrip(c)
    assert back.verdict == "Causa: saturação de IO"


def test_verdict_limitado_a_200_chars():
    with pytest.raises(ValidationError):
        Diagnosed(verdict="x" * 201, confidence="high", rationale="r")


def test_confidence_so_aceita_o_enum():
    with pytest.raises(ValidationError):
        Diagnosed(verdict="v", confidence="alta", rationale="r")  # PT-BR não entra


def test_inconclusive_nao_tem_campo_confidence():
    """A incoerência é inexpressável: não existe inconclusive com confiança."""
    assert "confidence" not in Inconclusive.model_fields


def test_diagnosed_exige_verdict_e_confidence():
    with pytest.raises(ValidationError):
        Diagnosed(rationale="r")  # type: ignore[call-arg]


def test_payload_fields_diagnosed():
    c = Diagnosed(verdict="v", confidence="high", rationale="prosa longa")
    p = payload_fields(c)
    assert p == {"outcome": "diagnosed", "verdict": "v", "confidence": "high"}
    # rationale NÃO vai ao payload (auditoria, não conteúdo buscável)
    assert "rationale" not in p


def test_payload_fields_inconclusive_nao_expoe_reason():
    # `reason` fica só no .md por ora: registro, não filtro (dado sem leitor).
    c = Inconclusive(reason="sem RBAC", rationale="prosa")
    assert payload_fields(c) == {"outcome": "inconclusive"}


def test_conclusion_path_espelha_o_prefixo():
    repo = "/workspace/repo"
    triage = os.path.join(repo, "triage", "data", "CiliumPolicyDrop", "2026__abc.md")
    assert conclusion_path_for(triage, repo) == os.path.join(
        repo, "conclusions", "data", "CiliumPolicyDrop", "2026__abc.md"
    )


def test_conclusion_path_rejeita_fora_do_prefixo_triage():
    with pytest.raises(ValueError):
        conclusion_path_for("/workspace/repo/outro/x.md", "/workspace/repo")


def test_from_front_matter_rejeita_outcome_desconhecido():
    with pytest.raises(ValueError):
        from_front_matter({"outcome": "maybe"})


def test_body_do_md_e_o_rationale_sem_o_cabecalho():
    c = Diagnosed(verdict="v", confidence="high", rationale="linha 1\n\nlinha 2")
    md = to_markdown(c, "k")
    assert "## Rationale" in md
    _, back = _roundtrip(c)
    assert back.rationale == "linha 1\n\nlinha 2"
