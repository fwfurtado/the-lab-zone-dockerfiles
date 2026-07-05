FROM ofwfurtado/mcp-server-qdrant:0.2.2

LABEL org.opencontainers.image.title="triage-indexer" \
      org.opencontainers.image.authors="Mimi <fwfurtado@gmail.com>" \
      org.opencontainers.image.vendor="The Lab Zone" \
      org.opencontainers.image.licenses="MIT"

USER root

COPY index.py /app/index.py

# Mesmo cache de modelos do qa-indexer: FastEmbed (e5-large) roda local, sem rede.
ENV FASTEMBED_CACHE_PATH=/opt/models

ENTRYPOINT ["python", "/app/index.py"]
