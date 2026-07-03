# apps/ai/.../qdrant-indexer/Dockerfile  (ou onde você builda tuas imagens)
FROM ofwfurtado/mcp-server-qdrant:0.2.2
USER root
COPY index.py /app/index.py
ENV FASTEMBED_CACHE_PATH=/opt/models
ENTRYPOINT ["python", "/app/index.py"]
