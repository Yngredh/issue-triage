FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
COPY migrations ./migrations
RUN uv sync --frozen --no-dev

# Base de conhecimento lida pelas ferramentas dos especialistas (D17).
COPY catalog/source ./catalog/source

# O wheel do claude-agent-sdk para Linux já traz o CLI do Claude Code embutido
# (claude_agent_sdk/_bundled/claude): não é preciso instalar Node.js nem o CLI (D17).
ENV PATH="/app/.venv/bin:$PATH" \
    CATALOG_SOURCE_DIR=/app/catalog/source

# Worker sem HTTP: nenhuma porta exposta. SIGTERM encerra de forma limpa (D4).
CMD ["python", "-m", "issue_triage.main"]
