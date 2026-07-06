FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src

RUN pip install --no-cache-dir ".[trading]"

# Persist the SQLite database and download cache across container restarts
VOLUME ["/data"]
ENV DATABASE_PATH=/data/trades.db \
    CACHE_DIR=/data/.cache

CMD ["python", "-m", "src.main"]
