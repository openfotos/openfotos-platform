"""Edge trust, request identity, and host-to-photographer resolution."""

import hmac
import ipaddress
import logging
import time
from uuid import uuid4

from django.conf import settings
from django.http import Http404, HttpRequest, HttpResponse

from .models import RESERVED_PHOTOGRAPHER_SLUGS, Photographer, PhotographerStatus

logger = logging.getLogger("openfotos.requests")


class OriginProtectionMiddleware:
    """Reject deployed traffic that did not pass through the configured Cloudflare rule."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        if settings.REQUIRE_CLOUDFLARE_ORIGIN_SECRET and not self._is_liveness(request.path):
            supplied = request.headers.get("X-OpenFotos-Origin", "")
            if not hmac.compare_digest(supplied, settings.CLOUDFLARE_ORIGIN_SECRET):
                raise Http404
        return self.get_response(request)

    @staticmethod
    def _is_liveness(path: str) -> bool:
        return path == "/health/live/"


class RequestIdentityMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        started_at = time.monotonic()
        request.openfotos_request_id = uuid4()
        request.openfotos_client_address = self._client_address(request)
        response = self.get_response(request)
        response.headers["X-Request-ID"] = str(request.openfotos_request_id)
        logger.info(
            "request_completed",
            extra={
                "request_id": str(request.openfotos_request_id),
                "request_method": request.method,
                "response_status": response.status_code,
                "host_scope": getattr(request, "openfotos_host_scope", "unknown"),
                "duration_ms": round((time.monotonic() - started_at) * 1_000, 1),
            },
        )
        return response

    @staticmethod
    def _client_address(request: HttpRequest) -> str:
        direct_address = request.META.get("REMOTE_ADDR", "unknown")
        if not settings.TRUST_CLOUDFLARE_CLIENT_IP:
            return direct_address
        cloudflare_address = request.headers.get("CF-Connecting-IP", "")
        try:
            return str(ipaddress.ip_address(cloudflare_address))
        except ValueError:
            return direct_address


class PhotographerHostMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        if self._is_liveness(request.path):
            request.photographer = None
            request.openfotos_host_scope = "health"
            return self.get_response(request)

        host = request.get_host().partition(":")[0].rstrip(".").lower()
        base_domain = settings.PUBLIC_BASE_DOMAIN
        request.photographer = None
        request.openfotos_host_scope = "service"

        if self._is_readiness(request.path):
            if not self._is_recognized_host(host, base_domain):
                raise Http404
            request.openfotos_host_scope = "health"
            return self.get_response(request)

        if host == settings.ADMIN_HOST:
            request.openfotos_host_scope = "admin"
        elif host == base_domain:
            request.openfotos_host_scope = "base"
        elif host.endswith(f".{base_domain}"):
            label = host[: -(len(base_domain) + 1)]
            if "." in label or label in RESERVED_PHOTOGRAPHER_SLUGS:
                raise Http404
            request.photographer = self._active_photographer(label)
            request.openfotos_host_scope = "photographer"
        else:
            raise Http404

        if self._is_admin_path(request.path):
            if request.openfotos_host_scope != "admin":
                raise Http404
        elif request.openfotos_host_scope != "photographer":
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

    @staticmethod
    def _is_liveness(path: str) -> bool:
        return path == "/health/live/"

    @staticmethod
    def _is_readiness(path: str) -> bool:
        return path == "/health/ready/"

    @staticmethod
    def _is_recognized_host(host: str, base_domain: str) -> bool:
        if host in {base_domain, settings.ADMIN_HOST}:
            return True
        if not host.endswith(f".{base_domain}"):
            return False
        label = host[: -(len(base_domain) + 1)]
        return "." not in label and label not in RESERVED_PHOTOGRAPHER_SLUGS


class ResponseSecurityHeadersMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        response = self.get_response(request)
        policy = [
            "default-src 'self'",
            "base-uri 'none'",
            "connect-src 'self'",
            "font-src 'self'",
            "form-action 'self'",
            "frame-ancestors 'none'",
            "img-src 'self' data: https:",
            "object-src 'none'",
            "script-src 'self'",
            "style-src 'self'",
        ]
        if settings.IS_DEPLOYED:
            policy.append("upgrade-insecure-requests")
        response.headers.setdefault("Content-Security-Policy", "; ".join(policy))
        response.headers.setdefault(
            "Permissions-Policy", "camera=(), geolocation=(), microphone=()"
        )
        response.headers.setdefault("Cross-Origin-Resource-Policy", "same-site")
        return response
