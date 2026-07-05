"""Testes do parser de documento do triage-indexer.

Foca na lógica com regra (parse de front-matter, corte de preâmbulo), que é a
divergência real do qa-indexer. FastEmbed/Qdrant não são exercitados aqui — são
I/O, cobertos por teste de integração no cluster.

Rode com: QDRANT_URL=x python -m pytest test_index.py  (ou o stub abaixo).
"""

import os
import sys
import types

# Stub dos imports pesados e da env obrigatória para importar o módulo isolado.
os.environ.setdefault("QDRANT_URL", "http://stub")
for name in ("mcp_server_qdrant", "mcp_server_qdrant.embeddings", "mcp_server_qdrant.embeddings.fastembed"):
    sys.modules.setdefault(name, types.ModuleType(name))
sys.modules["mcp_server_qdrant.embeddings.fastembed"].FastEmbedProvider = object
_qc = types.ModuleType("qdrant_client")
_qc.AsyncQdrantClient = object
_qc.models = object
sys.modules.setdefault("qdrant_client", _qc)

import index  # noqa: E402

REAL_REPORT = '''---
schema: 1
incident_key: "548d681d8de73cea7ef52d9f5763644f"
dedup_key: "548d681d8de73cea7ef52d9f5763644f"
group_key: "{}/{}/{triage=\\"true\\"}:{}"
alertnames:
  - "CiliumPolicyDrop"
namespace: ""
fired_at: "2026-07-05T21:19:00Z"
confirmation: unverified
---

Tenho evidência suficiente para fechar o diagnóstico. Vou consolidar.

---

## Triagem — CiliumPolicyDrop (ai → data)

### Sintoma
Alerta de policy drop.
'''


def test_front_matter_campos():
    fm, _ = index.parse_document(REAL_REPORT)
    assert fm["dedup_key"] == "548d681d8de73cea7ef52d9f5763644f"
    assert fm["alertnames"] == ["CiliumPolicyDrop"]
    assert fm["namespace"] == ""
    assert fm["confirmation"] == "unverified"
    # Aspas internas do group_key desescapadas.
    assert fm["group_key"] == '{}/{}/{triage="true"}:{}'


def test_preambulo_cortado():
    _, body = index.parse_document(REAL_REPORT)
    assert "Vou consolidar" not in body
    assert body.startswith("## Triagem")


def test_alertnames_multiplos():
    doc = '---\nalertnames:\n  - "A"\n  - "B"\ndedup_key: "k"\n---\n\n## X\ncorpo'
    fm, _ = index.parse_document(doc)
    assert fm["alertnames"] == ["A", "B"]


def test_alertnames_vazio_inline():
    doc = '---\nalertnames: []\ndedup_key: "k"\n---\n\n## X\ncorpo'
    fm, _ = index.parse_document(doc)
    assert fm["alertnames"] == []


def test_sem_cabecalho_usa_corpo_todo():
    # Sem cabeçalho markdown, não corta nada além do front-matter.
    doc = '---\ndedup_key: "k"\n---\n\nTexto sem cabeçalho.'
    _, body = index.parse_document(doc)
    assert body == "Texto sem cabeçalho."


def test_sem_front_matter():
    doc = "## Só corpo\nsem front-matter."
    fm, body = index.parse_document(doc)
    assert fm == {}
    assert body.startswith("## Só corpo")
