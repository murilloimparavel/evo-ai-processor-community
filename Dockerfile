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
# Report base-image copies before cleanup: COPY overlays files but does not
# delete old dist-info directories from the runtime's /usr/local prefix.
RUN python - <<'PY'
from importlib.metadata import distributions

for item in distributions():
    if (item.metadata.get('Name') or '').lower() == 'packaging':
        print(f'Runtime base: {item.metadata.get("Name")}=={item.version} at {item.locate_file("")}')
PY
# Clear packages bundled in the base image before copying pinned builder copies.
RUN python -m pip uninstall --yes setuptools wheel packaging

COPY --from=builder /usr/local/ /usr/local/
COPY --from=builder /root/.local/bin/uv /usr/local/bin/uv
COPY --from=builder /app/requirements.txt ./requirements.txt
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
for extension in root.rglob('*'):
    if not extension.is_file() or not re.search(r'\.so(?:\.\d+)*$', extension.name):
        continue
    runtime_env = os.environ.copy()
    relative_path = extension.relative_to(root)
    if relative_path.parts[0] in ('shapely', 'shapely.libs'):
        private_library_dirs = [str(shapely_libs)]
        if runtime_env.get('LD_LIBRARY_PATH'):
            private_library_dirs.append(runtime_env['LD_LIBRARY_PATH'])
        runtime_env['LD_LIBRARY_PATH'] = ':'.join(private_library_dirs)
    result = subprocess.run(['ldd', str(extension)], capture_output=True, text=True, env=runtime_env)
    output = result.stdout + result.stderr
    if result.returncode != 0 or 'not found' in output or 'not a dynamic executable' in output:
        raise SystemExit(f'Unresolved runtime libraries for {extension}:\n{output}')
PY
RUN python scripts/verify_candidate_runtime.py \
    && python -c "import shapely, pyarrow; assert shapely.geos_version_string.startswith('3.13.1')"

# The application import requires a Fernet key. Generate a disposable value in
# memory and pass it only to the smoke-test subprocess; it is never an ARG/ENV.
RUN python - <<'PY'
import base64
import os
import secrets
import subprocess
import sys

env = os.environ.copy()
env['ENCRYPTION_KEY'] = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode('ascii')
subprocess.run(
    [sys.executable, 'scripts/smoke_candidate_import.py'],
    env=env,
    check=True,
)
PY

ENV PORT=8000 \
    HOST=0.0.0.0 \
    DEBUG=false

EXPOSE 8000

CMD ["sh", "-c", "alembic upgrade head && python -m scripts.run_seeders && uvicorn src.main:app --host $HOST --port $PORT"]
