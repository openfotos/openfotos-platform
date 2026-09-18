"""URL configuration for the OpenFotos server."""

from django.contrib import admin
from django.urls import include, path

from openfotos_server.health import liveness, readiness

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/v1/", include("openfotos_server.events.api_urls")),
    path("health/live/", liveness, name="health-live"),
    path("health/ready/", readiness, name="health-ready"),
    path("", include("openfotos_server.events.urls")),
]
