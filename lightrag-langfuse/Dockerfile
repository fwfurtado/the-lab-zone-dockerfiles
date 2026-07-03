FROM ghcr.io/hkuds/lightrag:v1.5.3

LABEL org.opencontainers.image.title="lightrag-langfuse" \
      org.opencontainers.image.authors="Mimi <fwfurtado@gmail.com>" \
      org.opencontainers.image.vendor="The Lab Zone" \
      org.opencontainers.image.licenses="MIT"

RUN /app/.venv/bin/python -m pip install --no-cache-dir "langfuse>=3,<4"
