from django.apps import AppConfig
from django.db.backends.signals import connection_created


class EventsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "openfotos_server.events"
    verbose_name = "OneNodeAI Studio events"

    def ready(self) -> None:
        from openfotos_server.database import configure_deployed_search_path

        connection_created.connect(
            configure_deployed_search_path,
            dispatch_uid="openfotos.configure_deployed_search_path",
        )
