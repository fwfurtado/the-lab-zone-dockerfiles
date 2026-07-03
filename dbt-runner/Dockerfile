#  just docker build-and-push --app-name dbt-runner [--tag "<TAG>" | default "latest" ]   # -> pinar em chart/values.yaml
FROM ghcr.io/astral-sh/uv:0.11.21-python3.12-trixie-slim

WORKDIR /opt/dbt
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# só as deps (do lock). Sem COPY . . -> imagem não carrega o projeto.
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project

# `dbt` no PATH. O projeto e o profiles vêm do git artifact em runtime (workingDir aponta lá).
ENV PATH="/opt/dbt/.venv/bin:$PATH"

ENTRYPOINT ["dbt"]
