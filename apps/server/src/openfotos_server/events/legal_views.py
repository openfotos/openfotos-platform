"""Tenant-specific public privacy and service disclosures."""

from django.conf import settings
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET


def _context(request: HttpRequest) -> dict:
    photographer = getattr(request, "photographer", None)
    if photographer is None:
        raise Http404
    return {
        "photographer": photographer,
        "privacy_email": settings.STUDIO_PRIVACY_EMAIL,
        "source_code_url": settings.SOURCE_CODE_URL,
    }


def _public_legal_response(request: HttpRequest, template: str) -> HttpResponse:
    response = render(request, template, _context(request))
    response.headers["Cache-Control"] = "public, max-age=300"
    return response


@require_GET
def privacy_notice(request: HttpRequest) -> HttpResponse:
    return _public_legal_response(request, "openfotos_events/privacy.html")


@require_GET
def terms(request: HttpRequest) -> HttpResponse:
    return _public_legal_response(request, "openfotos_events/terms.html")
