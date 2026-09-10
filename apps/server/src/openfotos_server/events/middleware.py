"""Request identity and host-to-photographer resolution."""

from uuid import uuid4

from django.conf import settings
from django.http import Http404, HttpRequest, HttpResponse

from .models import RESERVED_PHOTOGRAPHER_SLUGS, Photographer, PhotographerStatus


class RequestIdentityMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        request.openfotos_request_id = uuid4()
        response = self.get_response(request)
        response.headers["X-Request-ID"] = str(request.openfotos_request_id)
        return response


class PhotographerHostMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        host = request.get_host().partition(":")[0].rstrip(".").lower()
        base_domain = settings.PUBLIC_BASE_DOMAIN
        request.photographer = None
        request.openfotos_host_scope = "service"

        if host == base_domain:
            request.openfotos_host_scope = "base"
        elif host.endswith(f".{base_domain}"):
            label = host[: -(len(base_domain) + 1)]
            if "." in label or label in RESERVED_PHOTOGRAPHER_SLUGS:
                raise Http404
            request.photographer = self._active_photographer(label)
            request.openfotos_host_scope = "photographer"

        if self._is_admin_path(request.path) and request.openfotos_host_scope != "base":
            raise Http404
        return self.get_response(request)

    @staticmethod
    def _active_photographer(slug: str) -> Photographer:
        try:
            return Photographer.objects.get(slug=slug, status=PhotographerStatus.ACTIVE)
        except Photographer.DoesNotExist as exc:
            raise Http404 from exc

    @staticmethod
    def _is_admin_path(path: str) -> bool:
        return path == "/admin" or path.startswith("/admin/")
