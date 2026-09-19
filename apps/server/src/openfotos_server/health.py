"""Process liveness and dependency readiness probes."""

import logging

from django.conf import settings
from django.db import connection
from django.http import JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_safe

from openfotos_server.events.object_store import configured_object_store

logger = logging.getLogger("openfotos.health")
REQUIRED_DATABASE_MIGRATION = ("events", "0011_event_erasure_tracking")


@require_safe
@never_cache
def liveness(_request):
    return JsonResponse({"status": "ok"})


@require_safe
@never_cache
def readiness(_request):
    if not _database_is_ready() or not _object_store_is_ready():
        return JsonResponse({"status": "unavailable"}, status=503)
    return JsonResponse({"status": "ok"})


def _database_is_ready() -> bool:
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT EXISTS (SELECT 1 FROM django_migrations WHERE app = %s AND name = %s)",
                list(REQUIRED_DATABASE_MIGRATION),
            )
            return cursor.fetchone() == (True,)
    except Exception:
        logger.warning("readiness_check_failed", extra={"dependency": "database"})
        return False


def _object_store_is_ready() -> bool:
    try:
        sentinel = configured_object_store().head(settings.READINESS_OBJECT_KEY)
        if sentinel is not None:
            return True
    except Exception:
        pass
    logger.warning("readiness_check_failed", extra={"dependency": "object_store"})
    return False
