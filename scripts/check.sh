#!/usr/bin/env bash
set -euo pipefail

uv run ruff format --check .
uv run ruff check .
uv run python apps/server/manage.py makemigrations --check --dry-run
uv run pytest
uv run python apps/server/manage.py check
