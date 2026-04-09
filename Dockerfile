FROM python:3.12-slim AS base

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Install dependencies first for layer caching
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Copy application code and install project
COPY src/ ./src/
RUN uv sync --frozen --no-dev

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", \
    "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')"]

CMD ["uv", "run", "python", "-m", "slack_agent_router.main"]
