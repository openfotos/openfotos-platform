"""Deployed PostgreSQL connection invariants."""

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

DEPLOYED_DATABASE_SCHEMA = "openfotos"
DEPLOYED_DATABASE_SEARCH_PATH = "openfotos,extensions"


def configure_deployed_search_path(*, connection, **_kwargs) -> None:
    """Select the private application schema after every deployed connection."""
    if not settings.IS_DEPLOYED or connection.vendor != "postgresql":
        return

    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT set_config('search_path', %s, false)",
            [DEPLOYED_DATABASE_SEARCH_PATH],
        )
        cursor.execute("SELECT current_schema()")
        current_schema = cursor.fetchone()
    if current_schema != (DEPLOYED_DATABASE_SCHEMA,):
        raise ImproperlyConfigured(
            "The deployed database connection cannot select the OpenFotos schema."
        )
