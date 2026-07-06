"""Entrypoint gestalt: um ponto por relatório (Fase D).

Serve a busca por GESTALT do incidente — "esse incidente inteiro já aconteceu?
é regressão?". A query é o incidente novo inteiro e o que casa é a assinatura
difusa do todo, então cada relatório vira UM vetor (o corpo inteiro). Não há
chunking: fatiar por tamanho criava fronteiras ruins e destruía a gestalt.

A busca por faceta (sintoma/causa/...) é servida pelo entrypoint facets, numa
collection separada. Aqui, um relatório = um ponto.
"""

from __future__ import annotations

from collections.abc import Iterable

from qdrant_client import models

from . import _common

# Campos filtráveis do Tier 0/1: o filtro híbrido busca por vetor E restringe por
# esses. keyword para match exato (namespace, alertname, confirmation).
PAYLOAD_INDEXES = {
    "metadata.namespace": models.PayloadSchemaType.KEYWORD,
    "metadata.alertnames": models.PayloadSchemaType.KEYWORD,
    "metadata.confirmation": models.PayloadSchemaType.KEYWORD,
}


def build_points(r: _common.Report) -> Iterable[_common.Point]:
    """Um ponto: o corpo íntegro do relatório, sem suffix (id = uuid5(dedup))."""
    yield _common.Point(suffix="", text=r.body, extra_payload={})


async def _main() -> int:
    cfg = _common.Config.from_env(
        collection_default="triage_incidents",
        payload_indexes=PAYLOAD_INDEXES,
    )
    return await _common.reconcile(cfg, build_points)


def main() -> None:
    _common.run(_main)


if __name__ == "__main__":
    main()
