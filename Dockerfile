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

# Check every installed native extension against the final runtime filesystem,
# then import the app without connecting to its external services.
RUN python - <<'PY'
from pathlib import Path
import os
import re
import subprocess

root = Path('/usr/local/lib/python3.11/site-packages')
shapely_libs = root / 'shapely.libs'
if not list(shapely_libs.glob('libgeos-*.so.3.13.1')):
    raise SystemExit('Shapely wheel GEOS runtime library is missing from final image')
private_library_dirs = [str(path) for path in root.glob('*.libs') if path.is_dir()]
runtime_env = os.environ.copy()
runtime_env['LD_LIBRARY_PATH'] = ':'.join(
    private_library_dirs + ([runtime_env['LD_LIBRARY_PATH']] if runtime_env.get('LD_LIBRARY_PATH') else [])
)
for extension in root.rglob('*'):
    if not extension.is_file() or not re.search(r'\.so(?:\.\d+)*$', extension.name):
        continue
    result = subprocess.run(
        ['ldd', str(extension)], capture_output=True, text=True, env=runtime_env
    )
    output = result.stdout + result.stderr
    if result.returncode != 0 or 'not found' in output or 'not a dynamic executable' in output:
        raise SystemExit(f'Unresolved runtime libraries for {extension}:\n{output}')
PY
RUN python scripts/verify_candidate_runtime.py \
    && python -c "import shapely, pyarrow; assert shapely.geos_version_string.startswith('3.13.1')" \
    && python -c "import src.main; assert hasattr(src.main, 'app')"

ENV PORT=8000 \
    HOST=0.0.0.0 \
    DEBUG=false

EXPOSE 8000

CMD ["sh", "-c", "alembic upgrade head && python -m scripts.run_seeders && uvicorn src.main:app --host $HOST --port $PORT"]
