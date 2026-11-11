FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

# Apply current distribution security updates before installing the app runtime.
RUN apt-get update && apt-get upgrade -y && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/archives

# Install UV (cached layer)
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:$PATH"

# Copy only dependency files for better caching
COPY requirements.txt ./requirements.txt

# Install dependencies using UV (much faster than pip)
RUN uv pip install --system --no-cache-dir -r requirements.txt

# Copy source code (this invalidates cache only when code changes)
COPY src/ ./src/
COPY README.md ./README.md
COPY pyproject.toml ./pyproject.toml
COPY scripts/ ./scripts/
COPY migrations/ ./migrations/
COPY alembic.ini ./alembic.ini

# Install the package in editable mode using UV (no deps since they're already installed)
RUN uv pip install --system --no-cache-dir -e . --no-deps

ENV PORT=8000 \
    HOST=0.0.0.0 \
    DEBUG=false

# Expose port
EXPOSE 8000

CMD ["sh", "-c", "alembic upgrade head && python -m scripts.run_seeders && uvicorn src.main:app --host $HOST --port $PORT"] 