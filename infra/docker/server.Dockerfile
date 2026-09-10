FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:0.11.26 /uv /uvx /bin/
COPY pyproject.toml uv.lock README.md ./
COPY apps ./apps
COPY packages ./packages

RUN uv sync --frozen --no-dev --extra server
RUN .venv/bin/python apps/server/manage.py collectstatic --noinput

ENV PATH="/app/.venv/bin:$PATH"
CMD ["gunicorn", "openfotos_server.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "1", "--threads", "4"]
