from django.apps import AppConfig


class EventsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "openfotos_server.events"
    verbose_name = "OpenFotos events"
