FROM ghcr.io/astral-sh/uv:0.11.21-python3.12-trixie-slim

LABEL org.opencontainers.image.title="dbt-runner" \
      org.opencontainers.image.authors="Mimi <fwfurtado@gmail.com>" \
      org.opencontainers.image.vendor="The Lab Zone" \
      org.opencontainers.image.licenses="MIT"

WORKDIR /opt/dbt

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

COPY pyproject.toml uv.lock .python-version ./

RUN uv sync --frozen --no-dev --no-install-project

ENV PATH="/opt/dbt/.venv/bin:$PATH"

ENTRYPOINT ["dbt"]
