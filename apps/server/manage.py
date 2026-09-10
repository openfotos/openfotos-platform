#!/usr/bin/env python
"""Django's command-line utility for OpenFotos."""

import os
import sys


def main() -> None:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "openfotos_server.settings")
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "Django is unavailable. Run `uv sync --extra server` before server commands."
        ) from exc
    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
