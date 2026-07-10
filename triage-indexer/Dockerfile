FROM ofwfurtado/mcp-server-qdrant:0.2.2

LABEL org.opencontainers.image.title="triage-indexer" \
      org.opencontainers.image.authors="Mimi <fwfurtado@gmail.com>" \
      org.opencontainers.image.vendor="The Lab Zone" \
      org.opencontainers.image.licenses="MIT"

USER root

WORKDIR /app
COPY pyproject.toml README.md ./
COPY triage_indexer/ ./triage_indexer/

# Instala o pacote e suas deps (mistune, pydantic-ai, otel; qdrant-client e
# mcp-server-qdrant já vêm da imagem base). Expõe os três console-scripts:
#   triage-index-gestalt  -> collection triage_incidents
#   triage-index-facets   -> collection triage_facets
#   triage-classify       -> escreve conclusions/ no workspace (ADR-0013)
RUN pip install --no-cache-dir .

# Remove o `fastmcp`, que vem da imagem base como dependência do mcp-server-qdrant
# para SERVIR MCP. Este job nunca serve MCP: os indexers usam apenas
# `mcp_server_qdrant.embeddings.fastembed` (que importa só o fastembed — o único
# módulo do pacote que toca fastmcp é `mcp_server_qdrant/mcp_server.py`), e o
# classificador não usa tools MCP.
#
# Ele PRECISA sair, não é só peso: o mcp-server-qdrant pina `fastmcp==2.7.0` e
# `pydantic<2.12`, enquanto o pydantic-ai exige `pydantic>=2.12`. O pip sobe o
# pydantic e deixa o fastmcp antigo, que então explode ao definir
# `class ServerSettings(BaseSettings)` com `TypeError: cannot specify both
# default and default_factory`. E como o pydantic_ai protege esse import só
# contra ImportError, o TypeError passa direto e mata `import pydantic_ai`.
#
# Sem o fastmcp instalado, o `except ImportError` do pydantic_ai cobre o caso e o
# import segue. De quebra, a imagem perde toda a árvore do servidor MCP
# (starlette, uvicorn, authlib, keyring...), que o job nunca executa.
RUN pip uninstall -y fastmcp fastmcp-slim || true

# Smoke test de BUILD. Um `pip install .` sobre uma árvore incompleta (arquivo
# novo esquecido no commit, dep faltando no pyproject) instala em silêncio e
# produz uma imagem OCA — o erro só aparece no Argo, horas depois. Aqui o build
# quebra. `import` prova que os módulos e suas deps existem; `--help` prova que
# o console-script foi registrado e que o argparse está são (sai 0 sem tocar
# env nem rede). `command -v` nos indexers: executá-los tentaria falar com o
# Qdrant.
RUN python -c "import triage_indexer.classifier, triage_indexer.conclusions, triage_indexer.observability" \
    && python -c "import mcp_server_qdrant.embeddings.fastembed" \
    && triage-classify --help > /dev/null \
    && command -v triage-index-gestalt > /dev/null \
    && command -v triage-index-facets > /dev/null

# Mesmo cache de modelos da base: FastEmbed (e5-large) roda local, sem rede.
ENV FASTEMBED_CACHE_PATH=/opt/models

# Sem ENTRYPOINT fixo: cada step do Argo escolhe o comando (gestalt|facets).
CMD ["triage-index-gestalt"]
