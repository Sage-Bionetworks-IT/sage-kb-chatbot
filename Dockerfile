FROM python:3.12-slim AS base

WORKDIR /app

# Install dependencies first for layer caching
COPY pyproject.toml ./
RUN pip install --no-cache-dir .

# Copy application code
COPY src/ ./src/

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')"]

CMD ["python", "-m", "slack_agent_router.main"]
