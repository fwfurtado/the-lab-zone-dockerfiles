FROM python:3.12-slim

LABEL org.opencontainers.image.title="mcp-server-qdrant" \
      org.opencontainers.image.authors="Mimi <fwfurtado@gmail.com>" \
      org.opencontainers.image.vendor="The Lab Zone" \
      org.opencontainers.image.licenses="MIT"

RUN pip install --no-cache-dir mcp-server-qdrant

ENV FASTEMBED_CACHE_PATH=/opt/models

# Suprime o access log do uvicorn (INFO: <ip> - "GET / HTTP/1.1" 404 ...), que
# em streamable-http é ~99% ruído de health probe (~72/min por pod). O FastMCP
# (SDK mcp) repassa FASTMCP_LOG_LEVEL direto pro uvicorn.Config(log_level=...);
# WARNING corta o uvicorn.access (emitido em INFO). O único INFO próprio do
# servidor é a linha de startup — perda desprezível; erros (WARNING+) seguem.
ENV FASTMCP_LOG_LEVEL=WARNING

RUN python -c "from fastembed import TextEmbedding; \
    from pprint import pprint; \
    pprint(TextEmbedding.list_supported_models()); \
    assert 'intfloat/multilingual-e5-large' in [m['model'] for m in TextEmbedding.list_supported_models()]; \
    TextEmbedding('intfloat/multilingual-e5-large')"

CMD ["mcp-server-qdrant", "--transport", "sse"]
