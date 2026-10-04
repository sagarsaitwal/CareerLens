# Single image for both the API and the worker; the entrypoint differs.
# Keeping one image means domain code cannot drift between them.

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app \
    # In development /app is bind-mounted from the host and the container
    # runs as a non-root user. Tool caches are redirected outside the
    # mount so nothing needs write access to it.
    RUFF_CACHE_DIR=/tmp/.ruff_cache \
    MYPY_CACHE_DIR=/tmp/.mypy_cache

WORKDIR /app

# libpq is needed by psycopg; build tooling is not, since psycopg ships
# binary wheels.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
RUN pip install --upgrade pip && pip install -e ".[dev]"

COPY . .

# Run as a non-root user: this process handles untrusted job-posting
# content.
RUN useradd --create-home --uid 10001 careerlens \
    && chown -R careerlens:careerlens /app
USER careerlens

EXPOSE 8000

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers ${UVICORN_WORKERS:-2}"]
