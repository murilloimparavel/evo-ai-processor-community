# Build Python dependencies and the editable application package in a disposable
# stage. Use the current supported Debian stable variant for both stages so
# Python ABI and linked system libraries match exactly.
FROM python:3.11.18-slim-trixie AS builder

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    ca-certificates \
    curl \
    libpq-dev \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/archives

RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:$PATH"

COPY requirements.txt ./requirements.txt
RUN uv pip install --system --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY README.md ./README.md
COPY pyproject.toml ./pyproject.toml
COPY scripts/ ./scripts/
COPY migrations/ ./migrations/
COPY alembic.ini ./alembic.ini

RUN uv pip install --system --no-cache-dir -e . --no-deps

# The runtime omits compilers, headers, libpq development files, curl and the
# UV installer. Candidate verification retains the small UV CLI only.
FROM python:3.11.18-slim-trixie AS runtime

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PATH="/usr/local/bin:$PATH"

RUN apt-get update && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends ca-certificates \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/archives

COPY --from=builder /usr/local/ /usr/local/
COPY --from=builder /root/.local/bin/uv /usr/local/bin/uv
COPY --from=builder /app/src/ ./src/
COPY --from=builder /app/README.md ./README.md
COPY --from=builder /app/pyproject.toml ./pyproject.toml
COPY --from=builder /app/scripts/ ./scripts/
COPY --from=builder /app/migrations/ ./migrations/
COPY --from=builder /app/alembic.ini ./alembic.ini

ENV PORT=8000 \
    HOST=0.0.0.0 \
    DEBUG=false

EXPOSE 8000

CMD ["sh", "-c", "alembic upgrade head && python -m scripts.run_seeders && uvicorn src.main:app --host $HOST --port $PORT"]
