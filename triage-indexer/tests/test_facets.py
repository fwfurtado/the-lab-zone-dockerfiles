"""Testes do corte de seções (facets.split_sections).

Cobrem: as 4 facetas, descarte de Confiança, aninhamento (### dentro de ##),
robustez a code fence (## dentro de ``` não é heading), e variações de nome
(plural, acento, caixa).
"""

from triage_indexer import facets
from triage_indexer._common import Report

REAL_BODY = """## Triagem — CiliumPolicyDrop (ai → data)

## Sintoma
Alerta warning de policy drop entre ai e data.

## Evidência
O pod drop-test gerou o tráfego.

### Métricas (VictoriaMetrics)
hubble_drop_total subiu para 0.8/s.

## Causa provável
Não há allow rule faltando — policy funcionando como projetada.

## Próximo passo
Deletar o pod drop-test.

## Confiança
Alta. Três sinais independentes."""


def test_quatro_facetas():
    secs = facets.split_sections(REAL_BODY)
    assert set(secs) == {"symptom", "evidence", "cause", "next_step"}


def test_confianca_descartada():
    secs = facets.split_sections(REAL_BODY)
    assert all("Três sinais" not in v for v in secs.values())


def test_titulo_nao_vira_faceta():
    # "## Triagem — ..." é o título, não uma faceta conhecida: ignorado.
    secs = facets.split_sections(REAL_BODY)
    assert "Triagem" not in " ".join(secs.keys())


def test_subsecao_aninhada_dentro_da_evidencia():
    secs = facets.split_sections(REAL_BODY)
    # O ### Métricas (nível 3) fica DENTRO de evidence, não vira seção solta.
    assert "Métricas" in secs["evidence"]
    assert "hubble_drop_total" in secs["evidence"]


def test_code_fence_nao_confunde_heading():
    # Um '## x' dentro de um code fence NÃO deve virar fronteira de seção.
    body = """## Sintoma
Veja o comando:

```
## isto está dentro de um fence, não é heading
kubectl get pods
```

## Causa provável
A causa real."""
    secs = facets.split_sections(body)
    # symptom deve conter o fence inteiro (o ## de dentro não quebrou a seção).
    assert "kubectl get pods" in secs["symptom"]
    assert "fence" in secs["symptom"]
    assert set(secs) == {"symptom", "cause"}


def test_variacoes_de_nome():
    # Plural, sem acento, caixa alta — todos devem casar.
    body = "## SINTOMAS\ns\n\n## Evidencia\ne\n\n## Causa Provável\nc\n\n## Próximos Passos\nn"
    secs = facets.split_sections(body)
    assert set(secs) == {"symptom", "evidence", "cause", "next_step"}


def test_build_points_um_por_faceta():
    r = Report(dedup_key="k1", front_matter={}, body=REAL_BODY)
    points = list(facets.build_points(r))
    assert len(points) == 4
    suffixes = {p.suffix for p in points}
    assert suffixes == {"symptom", "evidence", "cause", "next_step"}
    # Cada ponto carrega section no extra_payload para o filtro.
    for p in points:
        assert p.extra_payload["section"] == p.suffix


def test_sem_secoes_conhecidas_vazio():
    # Corpo sem nenhuma das 4 seções -> nenhum ponto (não força faceta).
    secs = facets.split_sections("## Outra coisa\ntexto\n\n## Mais uma\nmais")
    assert secs == {}
