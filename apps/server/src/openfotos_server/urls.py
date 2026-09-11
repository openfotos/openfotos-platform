"""URL configuration for the OpenFotos server."""

from django.contrib import admin
from django.http import JsonResponse
from django.urls import include, path


def health(_request):
    return JsonResponse({"status": "ok", "service": "openfotos"})


urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/v1/", include("openfotos_server.events.api_urls")),
    path("health/", health, name="health"),
    path("", include("openfotos_server.events.urls")),
]
