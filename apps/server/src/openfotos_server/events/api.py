"""Versioned JSON API used by authenticated desktop installations."""

import hashlib
import json
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import IntegrityError
from django.http import Http404, HttpRequest, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from openfotos_contracts import ContractError, ContributionInput, DeviceRole, DeviceStatus

from .desktop_auth import (
    DesktopAuthError,
    authenticate_access_token,
    authenticate_lead,
    create_invitation,
    redeem_invitation,
    refresh_session,
)
from .ingestion_services import (
    IngestionError,
    cancel_batch,
    close_intake,
    event_for_session,
    exclude_asset,
    finalize_ingestion,
    issue_upload_leases,
    reopen_intake,
    reserve_contribution,
    revoke_device,
    revoke_invitation,
    verify_uploaded_object,
    visible_events,
)
from .models import (
    AssetObject,
    ContributionBatch,
    IdempotencyRecord,
    RateLimitPurpose,
    UploaderDevice,
)
from .object_store import configured_object_store
from .rate_limits import clear_failures, rate_limit_status, register_failure


def _tenant(request: HttpRequest):
    photographer = getattr(request, "photographer", None)
    if photographer is None:
        raise Http404
    return photographer


def _error(code: str, message: str, *, status: int, retryable: bool = False) -> JsonResponse:
    return JsonResponse(
        {"error": {"code": code, "message": message, "retryable": retryable}},
        status=status,
    )


def _domain_error(exc: DesktopAuthError | ContractError | IngestionError) -> JsonResponse:
    if isinstance(exc, DesktopAuthError):
        status = 401
    elif isinstance(exc, ContractError):
        status = 400
    elif exc.retryable:
        status = 503 if exc.code == "object_store_unavailable" else 409
    elif exc.code.endswith("_not_found") or exc.code in {
        "event_not_found",
        "batch_not_found",
        "asset_not_found",
    }:
        status = 404
    elif exc.code in {"lead_required", "device_revoked"}:
        status = 403
    else:
        status = 409
    return _error(exc.code, str(exc), status=status, retryable=getattr(exc, "retryable", False))


@dataclass(frozen=True)
class _MutationContext:
    actor_key: str
    key: UUID
    operation: str
    request_sha256: str


def _mutation_context(
    request: HttpRequest, *, session, operation: str
) -> _MutationContext | JsonResponse:
    raw_key = request.headers.get("Idempotency-Key", "")
    try:
        key = UUID(raw_key)
    except (ValueError, AttributeError):
        return _error(
            "idempotency_key_required",
            "Send a UUID Idempotency-Key for this mutation.",
            status=400,
        )
    context = _MutationContext(
        actor_key=str(session.id),
        key=key,
        operation=operation,
        request_sha256=hashlib.sha256(request.body).hexdigest(),
    )
    existing = IdempotencyRecord.objects.filter(
        actor_key=context.actor_key,
        key=context.key,
        expires_at__gt=timezone.now(),
    ).first()
    if existing is None:
        return context
    if existing.operation != context.operation or existing.request_sha256 != context.request_sha256:
        return _error(
            "idempotency_conflict",
            "The idempotency key was already used for a different request.",
            status=409,
        )
    return JsonResponse(existing.response_body, status=existing.response_status)


def _remember(context: _MutationContext, data: dict, *, status: int = 200) -> JsonResponse:
    try:
        IdempotencyRecord.objects.create(
            actor_key=context.actor_key,
            key=context.key,
            operation=context.operation,
            request_sha256=context.request_sha256,
            response_status=status,
            response_body=data,
            expires_at=timezone.now() + timedelta(seconds=settings.IDEMPOTENCY_TTL_SECONDS),
        )
    except IntegrityError:
        existing = IdempotencyRecord.objects.get(
            actor_key=context.actor_key,
            key=context.key,
        )
        if (
            existing.operation != context.operation
            or existing.request_sha256 != context.request_sha256
        ):
            return _error(
                "idempotency_conflict",
                "The idempotency key was already used for a different request.",
                status=409,
            )
        return JsonResponse(existing.response_body, status=existing.response_status)
    return JsonResponse(data, status=status)


def _json_body(request: HttpRequest, *, fields: set[str]) -> dict:
    if request.content_type != "application/json":
        raise ContractError("invalid_request", "Use the application/json content type.")
    if len(request.body) > settings.DESKTOP_API_MAX_BODY_BYTES:
        raise ContractError("request_too_large", "The JSON request exceeds the API limit.")
    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ContractError("invalid_request", "The request body is not valid JSON.") from exc
    if not isinstance(body, dict) or set(body) != fields:
        raise ContractError("invalid_request", "Request fields do not match the API contract.")
    return body


def _uuid(value, *, field: str) -> UUID:
    try:
        return UUID(str(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ContractError("invalid_request", f"{field} must be a UUID.") from exc


def _bearer_session(request: HttpRequest):
    authorization = request.headers.get("Authorization", "")
    scheme, separator, token = authorization.partition(" ")
    if scheme != "Bearer" or separator != " " or not token or " " in token:
        raise DesktopAuthError("invalid_access_token", "A desktop access token is required.")
    session = authenticate_access_token(token)
    if session.photographer_id != _tenant(request).id:
        raise DesktopAuthError("invalid_access_token", "The desktop session is unavailable.")
    return session


def _event_data(event, *, session) -> dict:
    active_devices = UploaderDevice.objects.filter(
        event=event,
        status=DeviceStatus.ACTIVE.value,
    ).count()
    role = DeviceRole.LEAD.value if session.user_id else DeviceRole.UPLOADER.value
    return {
        "id": str(event.id),
        "name": event.name,
        "state": event.state,
        "role": role,
        "storage_limit_bytes": event.storage_limit_bytes,
        "reserved_original_bytes": event.reserved_original_bytes,
        "verified_original_bytes": event.verified_original_bytes,
        "remaining_original_bytes": event.storage_limit_bytes - event.reserved_original_bytes,
        "intake_state": event.intake_state,
        "intake_generation": event.intake_generation,
        "processing_profile_id": event.processing_profile_id,
        "max_contribution_devices": event.max_contribution_devices,
        "active_contribution_devices": active_devices,
        "device_label": session.device.label if session.device_id else "",
    }


def _tokens_data(tokens) -> dict:
    return {
        "access_token": tokens.access_token,
        "access_expires_at": tokens.session.access_expires_at.isoformat(),
        "refresh_token": tokens.refresh_token,
        "refresh_expires_at": tokens.session.refresh_expires_at.isoformat(),
    }


@csrf_exempt
@require_POST
def login(request: HttpRequest) -> JsonResponse:
    photographer = _tenant(request)
    try:
        body = _json_body(
            request,
            fields={"username", "password", "installation_id"},
        )
        installation_id = _uuid(body["installation_id"], field="installation_id")
    except ContractError as exc:
        return _domain_error(exc)
    subject = f"{photographer.id}:{body['username']}"
    current = rate_limit_status(
        purpose=RateLimitPurpose.PHOTOGRAPHER_LOGIN,
        subject=subject,
        request=request,
    )
    if current.limited:
        response = _error(
            "rate_limited",
            "Too many sign-in attempts. Try again later.",
            status=429,
            retryable=True,
        )
        response.headers["Retry-After"] = str(current.retry_after_seconds)
        return response
    try:
        tokens = authenticate_lead(
            photographer=photographer,
            username=str(body["username"]),
            password=str(body["password"]),
            installation_id=installation_id,
            request=request,
        )
    except DesktopAuthError as exc:
        failed = register_failure(
            purpose=RateLimitPurpose.PHOTOGRAPHER_LOGIN,
            subject=subject,
            request=request,
        )
        if failed.limited:
            response = _error(
                "rate_limited",
                "Too many sign-in attempts. Try again later.",
                status=429,
                retryable=True,
            )
            response.headers["Retry-After"] = str(failed.retry_after_seconds)
            return response
        return _domain_error(exc)
    clear_failures(
        purpose=RateLimitPurpose.PHOTOGRAPHER_LOGIN,
        subject=subject,
        request=request,
    )
    return JsonResponse(
        {
            **_tokens_data(tokens),
            "events": [
                _event_data(event, session=tokens.session)
                for event in visible_events(tokens.session)
            ],
        }
    )


@csrf_exempt
@require_POST
def refresh(request: HttpRequest) -> JsonResponse:
    _tenant(request)
    try:
        body = _json_body(request, fields={"refresh_token"})
        tokens = refresh_session(str(body["refresh_token"]), request=request)
        if tokens.session.photographer_id != request.photographer.id:
            raise DesktopAuthError(
                "invalid_refresh_token", "The desktop session must sign in again."
            )
    except (ContractError, DesktopAuthError) as exc:
        return _domain_error(exc)
    return JsonResponse(_tokens_data(tokens))


@csrf_exempt
@require_POST
def redeem(request: HttpRequest) -> JsonResponse:
    photographer = _tenant(request)
    try:
        body = _json_body(
            request,
            fields={"invitation_token", "installation_id", "device_label"},
        )
        event, tokens = redeem_invitation(
            photographer=photographer,
            token=str(body["invitation_token"]),
            installation_id=_uuid(body["installation_id"], field="installation_id"),
            label=str(body["device_label"]),
            request=request,
        )
    except (ContractError, DesktopAuthError) as exc:
        return _domain_error(exc)
    return JsonResponse(
        {**_tokens_data(tokens), "event": _event_data(event, session=tokens.session)}
    )


@require_GET
def events(request: HttpRequest) -> JsonResponse:
    try:
        session = _bearer_session(request)
    except DesktopAuthError as exc:
        return _domain_error(exc)
    return JsonResponse(
        {"events": [_event_data(event, session=session) for event in visible_events(session)]}
    )


@csrf_exempt
@require_POST
def invitations(request: HttpRequest, event_id: UUID) -> JsonResponse:
    try:
        session = _bearer_session(request)
        body = _json_body(request, fields=set())
        del body
        event = event_for_session(session, event_id)
        issued = create_invitation(session=session, event=event, request=request)
    except (ContractError, DesktopAuthError, IngestionError) as exc:
        return _domain_error(exc)
    origin = request.build_absolute_uri("/").rstrip("/")
    enrollment_url = f"{origin}/desktop/#invite={issued.token}"
    return JsonResponse(
        {
            "invitation_id": str(issued.invitation.id),
            "enrollment_url": enrollment_url,
            "expires_at": issued.invitation.expires_at.isoformat(),
            "max_redemptions": issued.invitation.max_redemptions,
        },
        status=201,
    )


@csrf_exempt
@require_POST
def revoke_invitation_view(
    request: HttpRequest, event_id: UUID, invitation_id: UUID
) -> JsonResponse:
    try:
        session = _bearer_session(request)
        context = _mutation_context(request, session=session, operation="revoke_invitation")
        if isinstance(context, JsonResponse):
            return context
        _json_body(request, fields=set())
        invitation = revoke_invitation(
            session=session,
            event_id=event_id,
            invitation_id=invitation_id,
            request=request,
        )
    except (ContractError, DesktopAuthError, IngestionError) as exc:
        return _domain_error(exc)
    return _remember(
        context,
        {"invitation_id": str(invitation.id), "status": "revoked"},
    )


@csrf_exempt
@require_POST
def reserve_batch(request: HttpRequest, event_id: UUID) -> JsonResponse:
    try:
        session = _bearer_session(request)
        context = _mutation_context(request, session=session, operation="reserve_batch")
        if isinstance(context, JsonResponse):
            return context
        body = _json_body(
            request,
            fields={"batch_id", "label", "processing_profile_id", "device_label", "assets"},
        )
        contribution = ContributionInput.from_dict(body)
        batch = reserve_contribution(
            session=session,
            event_id=event_id,
            contribution=contribution,
            request=request,
        )
    except (ContractError, DesktopAuthError, IngestionError) as exc:
        return _domain_error(exc)
    return _remember(context, _batch_data(batch, session=session), status=201)


@require_GET
def batch_detail(request: HttpRequest, event_id: UUID, batch_id: UUID) -> JsonResponse:
    try:
        session = _bearer_session(request)
        event = event_for_session(session, event_id)
        query = ContributionBatch.objects.select_related("device").filter(
            pk=batch_id,
            device__event=event,
        )
        if session.user_id is None:
            query = query.filter(device=session.device)
        batch = query.get()
    except ContributionBatch.DoesNotExist:
        return _error("batch_not_found", "The contribution is unavailable.", status=404)
    except (DesktopAuthError, IngestionError) as exc:
        return _domain_error(exc)
    return JsonResponse(_batch_data(batch, session=session))


@csrf_exempt
@require_POST
def upload_leases(request: HttpRequest, event_id: UUID, batch_id: UUID) -> JsonResponse:
    try:
        session = _bearer_session(request)
        body = _json_body(request, fields={"asset_ids"})
        if (
            not isinstance(body["asset_ids"], list)
            or not body["asset_ids"]
            or len(body["asset_ids"]) > settings.UPLOAD_LEASE_PAGE_SIZE
        ):
            raise ContractError(
                "invalid_request",
                f"Request 1 to {settings.UPLOAD_LEASE_PAGE_SIZE} asset upload leases.",
            )
        asset_ids = tuple(_uuid(value, field="asset_ids") for value in body["asset_ids"])
        if len(set(asset_ids)) != len(asset_ids):
            raise ContractError("invalid_request", "Asset IDs must be unique.")
        leases = issue_upload_leases(
            session=session,
            event_id=event_id,
            batch_id=batch_id,
            object_store=configured_object_store(),
            asset_ids=asset_ids,
        )
    except ImproperlyConfigured:
        return _error(
            "object_store_unavailable",
            "Object storage is not configured.",
            status=503,
            retryable=True,
        )
    except (ContractError, DesktopAuthError, IngestionError) as exc:
        return _domain_error(exc)
    return JsonResponse({"leases": leases})


@csrf_exempt
@require_POST
def complete_asset(request: HttpRequest, event_id: UUID, asset_id: UUID) -> JsonResponse:
    try:
        session = _bearer_session(request)
        context = _mutation_context(request, session=session, operation="complete_asset")
        if isinstance(context, JsonResponse):
            return context
        _json_body(request, fields=set())
        upload = verify_uploaded_object(
            session=session,
            event_id=event_id,
            asset_id=asset_id,
            object_store=configured_object_store(),
            request=request,
        )
    except ImproperlyConfigured:
        return _error(
            "object_store_unavailable",
            "Object storage is not configured.",
            status=503,
            retryable=True,
        )
    except (ContractError, DesktopAuthError, IngestionError) as exc:
        return _domain_error(exc)
    return _remember(context, {"asset_id": str(upload.asset_id), "state": upload.state})


@csrf_exempt
@require_POST
def intake_action(request: HttpRequest, event_id: UUID, action: str) -> JsonResponse:
    try:
        session = _bearer_session(request)
        context = _mutation_context(request, session=session, operation=f"{action}_intake")
        if isinstance(context, JsonResponse):
            return context
        _json_body(request, fields=set())
        if action == "close":
            event = close_intake(session=session, event_id=event_id, request=request)
        elif action == "reopen":
            event = reopen_intake(session=session, event_id=event_id, request=request)
        else:
            raise Http404
    except (ContractError, DesktopAuthError, IngestionError) as exc:
        return _domain_error(exc)
    return _remember(context, _event_data(event, session=session))


@csrf_exempt
@require_POST
def finalize(request: HttpRequest, event_id: UUID) -> JsonResponse:
    try:
        session = _bearer_session(request)
        context = _mutation_context(request, session=session, operation="finalize_ingestion")
        if isinstance(context, JsonResponse):
            return context
        _json_body(request, fields=set())
        manifest = finalize_ingestion(
            session=session,
            event_id=event_id,
            object_store=configured_object_store(),
            request=request,
        )
    except ImproperlyConfigured:
        return _error(
            "object_store_unavailable",
            "Object storage is not configured.",
            status=503,
            retryable=True,
        )
    except (ContractError, DesktopAuthError, IngestionError) as exc:
        return _domain_error(exc)
    return _remember(
        context,
        {
            "manifest_id": str(manifest.id),
            "generation": manifest.generation,
            "state": manifest.state,
            "asset_count": manifest.asset_count,
            "original_bytes": manifest.original_bytes,
            "excluded_asset_count": manifest.excluded_asset_count,
        },
    )


@csrf_exempt
@require_POST
def exclude_asset_view(request: HttpRequest, event_id: UUID, asset_id: UUID) -> JsonResponse:
    try:
        session = _bearer_session(request)
        context = _mutation_context(request, session=session, operation="exclude_asset")
        if isinstance(context, JsonResponse):
            return context
        body = _json_body(request, fields={"reason"})
        upload = exclude_asset(
            session=session,
            event_id=event_id,
            asset_id=asset_id,
            reason=str(body["reason"]),
            object_store=configured_object_store(),
            request=request,
        )
    except ImproperlyConfigured:
        return _error(
            "object_store_unavailable",
            "Object storage is not configured.",
            status=503,
            retryable=True,
        )
    except (ContractError, DesktopAuthError, IngestionError) as exc:
        return _domain_error(exc)
    return _remember(context, {"asset_id": str(upload.asset_id), "state": upload.state})


@csrf_exempt
@require_POST
def cancel_batch_view(request: HttpRequest, event_id: UUID, batch_id: UUID) -> JsonResponse:
    try:
        session = _bearer_session(request)
        context = _mutation_context(request, session=session, operation="cancel_batch")
        if isinstance(context, JsonResponse):
            return context
        _json_body(request, fields=set())
        batch = cancel_batch(
            session=session,
            event_id=event_id,
            batch_id=batch_id,
            object_store=configured_object_store(),
            request=request,
        )
    except ImproperlyConfigured:
        return _error(
            "object_store_unavailable",
            "Object storage is not configured.",
            status=503,
            retryable=True,
        )
    except (ContractError, DesktopAuthError, IngestionError) as exc:
        return _domain_error(exc)
    return _remember(context, {"batch_id": str(batch.id), "state": batch.state})


@csrf_exempt
@require_POST
def revoke_device_view(request: HttpRequest, event_id: UUID, device_id: UUID) -> JsonResponse:
    try:
        session = _bearer_session(request)
        context = _mutation_context(request, session=session, operation="revoke_device")
        if isinstance(context, JsonResponse):
            return context
        _json_body(request, fields=set())
        device = revoke_device(
            session=session,
            event_id=event_id,
            device_id=device_id,
            request=request,
        )
    except (ContractError, DesktopAuthError, IngestionError) as exc:
        return _domain_error(exc)
    return _remember(context, {"device_id": str(device.id), "status": device.status})


def _batch_data(batch: ContributionBatch, *, session) -> dict:
    objects = AssetObject.objects.filter(asset__batch=batch)
    data = {
        "id": str(batch.id),
        "state": batch.state,
        "generation": batch.intake_generation,
        "asset_count": batch.declared_asset_count,
        "original_bytes": batch.declared_original_bytes,
        "verified_asset_count": objects.filter(state="verified").count(),
        "failed_asset_count": objects.filter(state="failed").count(),
        "excluded_asset_count": objects.filter(state="excluded").count(),
    }
    if session.user_id is not None or batch.device_id == session.device_id:
        data["assets"] = list(
            objects.order_by("asset_id").values(
                "asset_id",
                "variant",
                "expected_bytes",
                "state",
                "failure_code",
            )
        )
        for item in data["assets"]:
            item["asset_id"] = str(item["asset_id"])
    return data
