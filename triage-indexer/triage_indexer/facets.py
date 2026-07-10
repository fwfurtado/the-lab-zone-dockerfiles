"""Entrypoint facets: um ponto por seção do relatório (Fase D).

Serve a busca por FACETA — "já vi esse sintoma? que linha de investigação
funcionou? o que foi sugerido?". Cada seção vira um vetor nítido, consultável
isolado, com `metadata.section` no payload para filtrar a faceta desejada.

Caso de uso central (hint de raciocínio): recuperar a seção de Evidência de um
incidente passado similar é recuperar o ROTEIRO de investigação que funcionou —
"da última vez, pra esse sintoma, olhei essas métricas/logs e cheguei aqui".

Corte de seção usa mistune (parser de markdown) em vez de regex: precisa da
ESTRUTURA (níveis de heading, aninhamento) e da robustez de não confundir um
'## x' dentro de um code fence com um heading real. Ver a deliberação da Fase D.

Facetas indexadas (as 4 seções que todo relatório de triagem tem):
symptom, evidence, cause, next_step. Confiança fica de fora (2 linhas viram
ruído no top-k).
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

import mistune
from qdrant_client import models

from . import _common

# Campos filtráveis do Tier 0/1. `section` é o eixo próprio das facetas —
# permite "busca só em section=evidence".
PAYLOAD_INDEXES = {
    "metadata.namespace": models.PayloadSchemaType.KEYWORD,
    "metadata.alertnames": models.PayloadSchemaType.KEYWORD,
    "metadata.confirmation": models.PayloadSchemaType.KEYWORD,
    "metadata.section": models.PayloadSchemaType.KEYWORD,
    # Campos da CONCLUSÃO (artefato irmão, ADR-0013). Ausentes num relatório ainda
    # não classificado — o Qdrant indexa o que existe; o filtro só não o alcança.
    # Quando virarem FilterableField no MCP, usar condition POSITIVA (== high,
    # == diagnosed), NUNCA `!=`: a inversão derrotou o agente em produção (ADR-0012).
    "metadata.outcome": models.PayloadSchemaType.KEYWORD,
    "metadata.confidence": models.PayloadSchemaType.KEYWORD,
}

# Mapa nome-de-seção normalizado -> faceta canônica. Normalização remove acento,
# caixa e plural simples, então "Sintoma"/"Sintomas"/"SINTOMA" casam. As 4
# seções que todo relatório tem hoje; Confiança fica fora de propósito.
_FACET_BY_SECTION = {
    "sintoma": "symptom",
    "evidencia": "evidence",
    "causa provavel": "cause",
    "proximo passo": "next_step",
}

_md_ast = mistune.create_markdown(renderer=None)


def _normalize(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c)).lower().strip()

    words = [re.sub(r"s$", "", w) for w in re.split(r"\s+", s) if w]
    return " ".join(words)


def _fenced_line_ranges(body: str) -> list[tuple[int, int]]:
    """Intervalos [início, fim) de linhas cobertas por code fences (```), via
    AST do mistune. É o ÚNICO caso em que uma linha '## x' não é heading e a
    regex de linha erraria — então é só disso que precisamos do parser.

    O mistune 3.x não dá offset de linha nos nós, mas o texto cru de cada
    block_code está em node['raw']; localizamos esse bloco no corpo para saber
    quais linhas ele ocupa.
    """
    ranges: list[tuple[int, int]] = []
    lines = body.splitlines()
    search_from = 0
    for node in _md_ast(body):
        if node.get("type") != "block_code":
            continue
        raw = node.get("raw", "")
        # As linhas do conteúdo do fence (sem os ``` delimitadores).
        raw_lines = raw.splitlines()
        if not raw_lines:
            continue
        first = raw_lines[0]
        # Acha onde esse bloco começa no corpo, a partir da última posição vista.
        for idx in range(search_from, len(lines)):
            if lines[idx].strip() == first.strip():
                start = idx
                end = min(idx + len(raw_lines), len(lines))
                ranges.append((start, end))
                search_from = end
                break
    return ranges


def _in_fence(line_idx: int, fences: list[tuple[int, int]]) -> bool:
    return any(start <= line_idx < end for start, end in fences)


def split_sections(body: str) -> dict[str, str]:
    """Fatia o corpo em {faceta: texto}.

    Percorre o texto linha a linha; uma linha é heading de topo se casa
    '^#{1,2}\\s+' E não está dentro de um code fence (o mistune fornece os
    intervalos de fence). Casa-se o NOME normalizado do heading contra o mapa de
    facetas — sem depender do texto renderizado do AST, que descarta markdown
    inline (código/negrito) do título e desalinhava o casamento.

    Conteúdo de uma seção vai do heading até o próximo heading de topo;
    subseções (###) ficam incluídas (não são fronteira). Headings que não casam
    faceta (título '# Triagem', '## Confiança') abrem uma seção que é descartada.
    """
    fences = _fenced_line_ranges(body)
    lines = body.splitlines()
    sections: dict[str, str] = {}
    cur_facet: str | None = None
    cur_start = 0

    def flush(facet: str | None, start: int, end: int) -> None:
        if facet is None:
            return
        text = "\n".join(lines[start:end]).strip()
        if text:
            sections[facet] = text

    for i, line in enumerate(lines):
        if _in_fence(i, fences):
            continue
        m = re.match(r"^\s{0,3}#{1,2}\s+(.+?)\s*$", line)
        if not m:
            continue
        flush(cur_facet, cur_start, i)
        cur_facet = _FACET_BY_SECTION.get(_normalize(m.group(1)))
        cur_start = i + 1
    flush(cur_facet, cur_start, len(lines))
    return sections


def build_points(r: _common.Report) -> Iterable[_common.Point]:
    """Um ponto por faceta reconhecida. O suffix é a faceta (id =
    uuid5(dedup#facet)), e section vai ao payload para filtro."""
    for facet, text in split_sections(r.body).items():
        yield _common.Point(
            suffix=facet,
            text=text,
            extra_payload={"section": facet},
        )


async def _main() -> int:
    cfg = _common.Config.from_env(
        collection_default="triage_facets",
        payload_indexes=PAYLOAD_INDEXES,
    )
    return await _common.reconcile(cfg, build_points)


def main() -> None:
    _common.run(_main)


if __name__ == "__main__":
    main()
