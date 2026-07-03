FROM python:3.12-slim
RUN pip install --no-cache-dir mcp-server-qdrant
ENV FASTEMBED_CACHE_PATH=/opt/models
RUN python -c "from fastembed import TextEmbedding; \
    assert 'intfloat/multilingual-e5-large' in [m['model'] for m in TextEmbedding.list_supported_models()]; \
    TextEmbedding('intfloat/multilingual-e5-large')"
CMD ["mcp-server-qdrant", "--transport", "sse"]
