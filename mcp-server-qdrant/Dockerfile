FROM python:3.12-slim

LABEL org.opencontainers.image.title="mcp-server-qdrant" \
      org.opencontainers.image.authors="Mimi <fwfurtado@gmail.com>" \
      org.opencontainers.image.vendor="The Lab Zone" \
      org.opencontainers.image.licenses="MIT"

RUN pip install --no-cache-dir mcp-server-qdrant

ENV FASTEMBED_CACHE_PATH=/opt/models

RUN python -c "from fastembed import TextEmbedding; \
    from pprint import pprint; \
    pprint(TextEmbedding.list_supported_models()); \
    assert 'intfloat/multilingual-e5-large' in [m['model'] for m in TextEmbedding.list_supported_models()]; \
    TextEmbedding('intfloat/multilingual-e5-large')"

CMD ["mcp-server-qdrant", "--transport", "sse"]
