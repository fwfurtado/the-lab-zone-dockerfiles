FROM ofwfurtado/mcp-server-qdrant:0.2.2

LABEL org.opencontainers.image.title="triage-indexer" \
      org.opencontainers.image.authors="Mimi <fwfurtado@gmail.com>" \
      org.opencontainers.image.vendor="The Lab Zone" \
      org.opencontainers.image.licenses="MIT"

USER root

WORKDIR /app
COPY pyproject.toml README.md ./
COPY triage_indexer/ ./triage_indexer/

# Instala o pacote e suas deps (mistune; qdrant-client e mcp-server-qdrant já
# vêm da imagem base). Expõe os dois console-scripts:
#   triage-index-gestalt  -> collection triage_incidents
#   triage-index-facets   -> collection triage_facets
RUN pip install --no-cache-dir .

# Mesmo cache de modelos da base: FastEmbed (e5-large) roda local, sem rede.
ENV FASTEMBED_CACHE_PATH=/opt/models

# Sem ENTRYPOINT fixo: cada step do Argo escolhe o comando (gestalt|facets).
CMD ["triage-index-gestalt"]
