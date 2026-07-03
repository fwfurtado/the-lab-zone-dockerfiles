FROM ghcr.io/astral-sh/uv:0.11.21-python3.12-trixie-slim

WORKDIR /opt/dbt

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

COPY pyproject.toml uv.lock .python-version ./

RUN uv sync --frozen --no-dev --no-install-project

ENV PATH="/opt/dbt/.venv/bin:$PATH"

ENTRYPOINT ["dbt"]
