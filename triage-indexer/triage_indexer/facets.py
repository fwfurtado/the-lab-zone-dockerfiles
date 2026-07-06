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


def _heading_text(node: dict) -> str:
    return "".join(c.get("raw", "") for c in node.get("children", []) if c.get("type") == "text")


def _real_headings(body: str) -> list[tuple[int, str]]:
    """Sequência de headings REAIS (nível, texto) via AST — na ordem do documento.

    Usa o mistune para não confundir '## x' dentro de code fence com heading.
    Só níveis 1-2 são fronteiras de seção de topo; níveis 3+ ficam DENTRO da
    seção de topo corrente (aninhamento preservado), então não entram aqui.
    """
    out = []
    for node in _md_ast(body):
        if node.get("type") == "heading" and node.get("attrs", {}).get("level", 99) <= 2:
            out.append((node["attrs"]["level"], _heading_text(node).strip()))
    return out


def split_sections(body: str) -> dict[str, str]:
    """Fatia o corpo em {faceta: texto}, casando os headings de topo reais.

    Percorre o texto linha a linha, mas só considera heading uma linha que o AST
    do mistune confirmou como heading de topo (evita code fences). O conteúdo de
    uma seção vai do heading até o próximo heading de topo — subseções (###)
    ficam incluídas.
    """
    real = _real_headings(body)
    if not real:
        return {}
    # Fila dos textos de heading de topo reais, na ordem — consumida ao casar.
    pending = [txt for _lvl, txt in real]

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
        m = re.match(r"^\s{0,3}(#{1,2})\s+(.+?)\s*$", line)
        if not m:
            continue
        text = m.group(2).strip()
        # Só é fronteira se corresponde ao próximo heading real esperado (na
        # ordem) — assim um '## x' dentro de code fence, que o AST não listou,
        # não vira fronteira.
        if pending and text == pending[0]:
            pending.pop(0)
            flush(cur_facet, cur_start, i)
            cur_facet = _FACET_BY_SECTION.get(_normalize(text))
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
