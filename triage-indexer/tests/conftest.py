"""Stubs dos imports pesados (fastembed, qdrant_client) para os testes das
funções puras (parse, preâmbulo, split_sections) rodarem sem o modelo nem o
Qdrant. A lógica de I/O (reconcile) é validada por integração no cluster.
"""

import os
import sys
import types

os.environ.setdefault("QDRANT_URL", "http://stub")

for _n in (
    "mcp_server_qdrant",
    "mcp_server_qdrant.embeddings",
    "mcp_server_qdrant.embeddings.fastembed",
):
    sys.modules.setdefault(_n, types.ModuleType(_n))
sys.modules["mcp_server_qdrant.embeddings.fastembed"].FastEmbedProvider = object

_qc = types.ModuleType("qdrant_client")
_models = types.ModuleType("qdrant_client.models")


class _PayloadSchemaType:
    KEYWORD = "keyword"


_models.PayloadSchemaType = _PayloadSchemaType
_qc.models = _models
_qc.AsyncQdrantClient = object
sys.modules.setdefault("qdrant_client", _qc)
sys.modules.setdefault("qdrant_client.models", _models)
