"""Testes do núcleo comum (_common): parse de front-matter e corte de preâmbulo.

Validados contra um relatório real (CiliumPolicyDrop). A lógica de I/O
(reconcile/upsert/GC) é integração, não coberta aqui.
"""

from triage_indexer import _common

REAL_REPORT = '''---
schema: 1
incident_key: "548d681d8de73cea7ef52d9f5763644f"
dedup_key: "548d681d8de73cea7ef52d9f5763644f"
group_key: "{}/{}/{triage=\\"true\\"}:{}"
alertnames:
  - "CiliumPolicyDrop"
namespace: "data"
fired_at: "2026-07-05T23:35:20Z"
confirmation: unverified
---

Tenho evidência suficiente para fechar o diagnóstico. Vou consolidar.

---

## Triagem — CiliumPolicyDrop (ai → data)

## Sintoma
Alerta de policy drop.
'''


def test_front_matter_campos():
    fm, _ = _common.parse_document(REAL_REPORT)
    assert fm["dedup_key"] == "548d681d8de73cea7ef52d9f5763644f"
    assert fm["alertnames"] == ["CiliumPolicyDrop"]
    assert fm["namespace"] == "data"
    assert fm["confirmation"] == "unverified"
    assert fm["group_key"] == '{}/{}/{triage="true"}:{}'


def test_preambulo_cortado():
    _, body = _common.parse_document(REAL_REPORT)
    assert "Vou consolidar" not in body
    assert body.startswith("## Triagem")


def test_alertnames_multiplos():
    doc = '---\nalertnames:\n  - "A"\n  - "B"\ndedup_key: "k"\n---\n\n## X\ncorpo'
    fm, _ = _common.parse_document(doc)
    assert fm["alertnames"] == ["A", "B"]


def test_alertnames_vazio_inline():
    doc = '---\nalertnames: []\ndedup_key: "k"\n---\n\n## X\ncorpo'
    fm, _ = _common.parse_document(doc)
    assert fm["alertnames"] == []


def test_sem_front_matter():
    fm, body = _common.parse_document("## Só corpo\nsem front-matter.")
    assert fm == {}
    assert body.startswith("## Só corpo")


def test_base_payload_campos_comuns():
    fm, body = _common.parse_document(REAL_REPORT)
    r = _common.Report(dedup_key="k", front_matter=fm, body=body)
    cfg = _common.Config.from_env("c", {})
    p = _common.base_payload(cfg, r)
    md = p["metadata"]
    assert md["namespace"] == "data"
    assert md["confirmation"] == "unverified"
    assert md["run_id"] == cfg.run_id
