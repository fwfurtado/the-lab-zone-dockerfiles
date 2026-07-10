"""O artefato de conclusão: schema, serialização e leitura (ADR-0013).

Uma conclusão é uma RELEITURA de um relatório de triagem — o que o agente
afirmou (`verdict`) e quão seguro estava (`confidence`). Vive num `.md` irmão no
Garage, sob o prefixo `conclusions/`, colado ao relatório pela `dedup_key`.

O schema é um UNION DISCRIMINADO: ou o relatório chegou a um diagnóstico
(`Diagnosed`), ou não chegou (`Inconclusive`). A incoerência é inexpressável —
não existe "diagnosed sem verdict" nem "inconclusive com confidence". O campo
`outcome` é o discriminador, e vira o campo derivado do payload no Qdrant sem
que o modelo precise preenchê-lo coerentemente.

`inconclusive` NÃO é um valor de `confidence` (ADR-0013): `verdict` responde
"qual é a causa" e `confidence` responde "quão seguro"; sem causa, a segunda
pergunta não se aplica. Misturar as duas tornaria `low` (pista fraca, útil)
indistinguível de "não há pista".

O front-matter é emitido no MESMO YAML restrito que a borda Go emite para o
relatório (`_common.parse_document` o lê de volta) — um parser só, sem PyYAML.
O `rationale` é prosa e vai no CORPO do markdown, não no front-matter.
"""

from __future__ import annotations

import os
from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field

SCHEMA_VERSION = 1

# Rede de segurança contra patologia (um "verdict" de dois parágrafos), NÃO
# policiamento de estilo. A concisão é pedida no prompt; o limite só existe para
# o payload do Qdrant não degenerar.
#
# Por que folgado (300 e não 200): o modelo NÃO sabe contar caracteres. Com o
# limite colado no comprimento típico, toda causa um pouco mais longa vira
# `string_too_long` -> retry -> outra chamada ao LLM. Observado em produção: uma
# causa legítima de 220 chars (177 só de causa, 78 gastos em nomes de recurso
# entre crases, como `postgresql-default-deny-ingress`) custou DOIS retries;
# com `retries=2` a classificação ficou a uma tentativa de falhar de vez. E cada
# retry reenvia o histórico e repaga os reasoning_tokens.
#
# O verdict vai ao PAYLOAD do Qdrant, não ao embedding: 300 chars não custam
# recuperação. Um retry custa ~6s e ~$0.003.
VERDICT_MAX_CHARS = 300

# Prefixos irmãos no bucket. O relatório é imutável; a conclusão é regenerável.
TRIAGE_PREFIX = "triage"
CONCLUSIONS_PREFIX = "conclusions"

_RATIONALE_HEADING = "## Rationale"


class Diagnosed(BaseModel):
    """O relatório apontou uma causa provável."""

    outcome: Literal["diagnosed"] = "diagnosed"
    verdict: str = Field(
        max_length=VERDICT_MAX_CHARS,
        description="A causa primária apontada pelo relatório, em uma frase curta.",
    )
    confidence: Literal["high", "medium", "low"] = Field(
        description="Confiança do DIAGNÓSTICO PRIMÁRIO (não o mínimo entre afirmações auxiliares).",
    )
    rationale: str = Field(
        description="Por que esta leitura: o que no relatório sustenta o verdict e a confiança.",
    )


class Inconclusive(BaseModel):
    """O relatório NÃO chegou a uma causa (ex.: 'não há dado suficiente')."""

    outcome: Literal["inconclusive"] = "inconclusive"
    reason: str = Field(
        max_length=VERDICT_MAX_CHARS,
        description="Por que não houve diagnóstico, em uma frase curta.",
    )
    rationale: str = Field(
        description="Por que esta leitura: o que no relatório indica a ausência de conclusão.",
    )


# Union discriminado por `outcome`. É o `output_type` do classificador.
Conclusion = Annotated[Union[Diagnosed, Inconclusive], Field(discriminator="outcome")]


def _yaml_quote(s: str) -> str:
    """Escapa uma string para o YAML restrito lido por `_common._unquote`.

    Espelho exato do `_unquote`: barra invertida primeiro, depois aspas e os
    controles. Emitir fora desse contrato quebra a leitura de volta.
    """
    s = s.replace("\\", "\\\\").replace('"', '\\"')
    s = s.replace("\n", "\\n").replace("\t", "\\t").replace("\r", "\\r")
    return f'"{s}"'


def to_markdown(conclusion: Diagnosed | Inconclusive, dedup_key: str) -> str:
    """Serializa a conclusão no `.md` irmão: front-matter + corpo.

    Campos estruturados no front-matter; o `rationale` (prosa) no corpo. Valores
    de enum (`outcome`, `confidence`) vão sem aspas — são seguros e legíveis.
    """
    lines = [
        "---",
        f"schema: {SCHEMA_VERSION}",
        f"dedup_key: {_yaml_quote(dedup_key)}",
        f"outcome: {conclusion.outcome}",
    ]
    if isinstance(conclusion, Diagnosed):
        lines.append(f"verdict: {_yaml_quote(conclusion.verdict)}")
        lines.append(f"confidence: {conclusion.confidence}")
    else:
        lines.append(f"reason: {_yaml_quote(conclusion.reason)}")
    lines += ["---", "", _RATIONALE_HEADING, "", conclusion.rationale.strip(), ""]
    return "\n".join(lines)


def from_front_matter(fm: dict, body: str = "") -> Diagnosed | Inconclusive:
    """Reconstrói a conclusão a partir do front-matter já parseado.

    O indexer usa só o front-matter (não precisa do rationale); o `body` é
    opcional e, quando vem, tem o cabeçalho `## Rationale` removido.
    """
    rationale = body
    if rationale.lstrip().startswith(_RATIONALE_HEADING):
        rationale = rationale.lstrip()[len(_RATIONALE_HEADING) :]
    rationale = rationale.strip()

    outcome = fm.get("outcome", "")
    if outcome == "diagnosed":
        return Diagnosed(
            verdict=fm.get("verdict", ""),
            confidence=fm.get("confidence", "low"),  # type: ignore[arg-type]
            rationale=rationale,
        )
    if outcome == "inconclusive":
        return Inconclusive(reason=fm.get("reason", ""), rationale=rationale)
    raise ValueError(f"outcome desconhecido no .md de conclusão: {outcome!r}")


def payload_fields(conclusion: Diagnosed | Inconclusive) -> dict:
    """Os campos que a conclusão contribui ao payload do Qdrant.

    `outcome` é DERIVADO da variante (não preenchido pelo modelo), logo não pode
    divergir. `rationale` NÃO entra: é artefato de auditoria/calibração, não
    conteúdo buscável — não polui o espaço vetorial (ADR-0013). `reason` também
    fica de fora por ora: registro no `.md`, não filtro (dado sem leitor não sobe).
    """
    if isinstance(conclusion, Diagnosed):
        return {
            "outcome": "diagnosed",
            "verdict": conclusion.verdict,
            "confidence": conclusion.confidence,
        }
    return {"outcome": "inconclusive"}


def conclusion_path_for(triage_path: str, repo_dir: str) -> str:
    """Espelha `…/triage/<resto>.md` -> `…/conclusions/<resto>.md`.

    Deriva do caminho REAL do relatório em vez de reconstruir a chave (cuja
    lógica de composição vive na borda Go) — assim o espelho acompanha o que a
    borda de fato produziu, sem duplicar a regra.
    """
    rel = os.path.relpath(triage_path, repo_dir)
    parts = rel.split(os.sep)
    if not parts or parts[0] != TRIAGE_PREFIX:
        raise ValueError(f"relatório fora do prefixo {TRIAGE_PREFIX}/: {rel}")
    return os.path.join(repo_dir, CONCLUSIONS_PREFIX, *parts[1:])
