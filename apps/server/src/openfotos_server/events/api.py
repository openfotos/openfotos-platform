"""Versioned JSON API used by authenticated desktop installations."""

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import IntegrityError, transaction
from django.http import Http404, HttpRequest, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from openfotos_contracts import (
    DERIVATIVE_VARIANTS,
    AssetDerivativesInput,
    AssetVariant,
    ContractError,
    ContributionInput,
    EventSnapshot,
    EventState,
    InstallationStatus,
    IntakeState,
    PreviewPolicyInput,
    SubEventSnapshot,
)
from openfotos_vision import (
    ACCEPTED_FACE_MODEL_CONTRACT,
    ACCEPTED_SFACE_DETECTOR_FLOOR,
    FaceAnalysisDocument,
    FaceAnalysisDocumentError,
)

from .derivative_services import (
    confirm_preview_policy,
    issue_derivative_leases,
    issue_owned_original_url,
    issue_policy_mark_url,
    preview_policy_snapshot,
    register_asset_derivatives,
    report_derivative_failure,
    verify_derivative,
)
from .desktop_auth import (
    DesktopAuthError,
    authenticate_access_token,
    authenticate_photographer,
    refresh_session,
    revoke_session,
)
from .face_services import report_face_analysis_failure, submit_face_analysis
from .ingestion_services import (
    IngestionError,
    cancel_batch,
    event_for_session,
    exclude_asset,
    issue_upload_leases,
    reserve_contribution,
    revoke_installation,
    verify_uploaded_object,
    visible_events,
)
from .models import (
    AssetObject,
    ContributionBatch,
    EventInstallation,
    FaceAnalysis,
    IdempotencyRecord,
    PreviewPolicy,
    RateLimitPurpose,
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
    elif exc.code in {"photographer_required", "installation_revoked"}:
        status = 403
    else:
        status = 409
    return _error(exc.code, str(exc), status=status, retryable=getattr(exc, "retryable", False))


@dataclass(frozen=True)
class _MutationClaim:
    record_id: int
    actor_key: str
    key: UUID
    operation: str
    request_sha256: str


_IDEMPOTENCY_PENDING_STATUS = 0
_IDEMPOTENCY_PENDING_TTL_SECONDS = 15 * 60


def _claim_mutation(
    request: HttpRequest, *, session, operation: str
) -> _MutationClaim | JsonResponse:
    raw_key = request.headers.get("Idempotency-Key", "")
    try:
        key = UUID(raw_key)
    except (ValueError, AttributeError):
        return _error(
            "idempotency_key_required",
            "Send a UUID Idempotency-Key for this mutation.",
            status=400,
        )
    actor_key = str(session.id)
    request_sha256 = hashlib.sha256(request.body).hexdigest()
    now = timezone.now()
    with transaction.atomic():
        existing = (
            IdempotencyRecord.objects.select_for_update()
            .filter(actor_key=actor_key, key=key)
            .first()
        )
        if existing is not None and existing.expires_at <= now:
            existing.delete()
            existing = None
        if existing is not None:
            return _replay_or_reject(
                existing,
                operation=operation,
                request_sha256=request_sha256,
            )
        try:
            with transaction.atomic():
                record = IdempotencyRecord.objects.create(
                    actor_key=actor_key,
                    key=key,
                    operation=operation,
                    request_sha256=request_sha256,
                    response_status=_IDEMPOTENCY_PENDING_STATUS,
                    response_body={},
                    expires_at=now
                    + timedelta(
                        seconds=min(
                            settings.IDEMPOTENCY_TTL_SECONDS,
                            _IDEMPOTENCY_PENDING_TTL_SECONDS,
                        )
                    ),
                )
        except IntegrityError:
            existing = IdempotencyRecord.objects.select_for_update().get(
                actor_key=actor_key,
                key=key,
            )
            return _replay_or_reject(
                existing,
                operation=operation,
                request_sha256=request_sha256,
            )
    return _MutationClaim(
        record_id=record.id,
        actor_key=actor_key,
        key=key,
        operation=operation,
        request_sha256=request_sha256,
    )


def _replay_or_reject(
    record: IdempotencyRecord,
    *,
    operation: str,
    request_sha256: str,
) -> JsonResponse:
    if record.operation != operation or record.request_sha256 != request_sha256:
        return _error(
            "idempotency_conflict",
            "The idempotency key was already used for a different request.",
            status=409,
        )
    if record.response_status == _IDEMPOTENCY_PENDING_STATUS:
        return _error(
            "idempotency_in_progress",
            "The matching request is still in progress; retry it shortly.",
            status=409,
            retryable=True,
        )
    return JsonResponse(record.response_body, status=record.response_status)


def _complete_mutation(claim: _MutationClaim, data: dict, *, status: int) -> JsonResponse:
    with transaction.atomic():
        record = IdempotencyRecord.objects.select_for_update().get(pk=claim.record_id)
        if (
            record.actor_key != claim.actor_key
            or record.key != claim.key
            or record.operation != claim.operation
            or record.request_sha256 != claim.request_sha256
            or record.response_status != _IDEMPOTENCY_PENDING_STATUS
        ):
            raise RuntimeError("The idempotency claim changed before completion.")
        record.response_status = status
        record.response_body = data
        record.expires_at = timezone.now() + timedelta(seconds=settings.IDEMPOTENCY_TTL_SECONDS)
        record.save(update_fields=("response_status", "response_body", "expires_at"))
    return JsonResponse(data, status=status)


def _release_mutation(claim: _MutationClaim) -> None:
    IdempotencyRecord.objects.filter(
        pk=claim.record_id,
        actor_key=claim.actor_key,
        key=claim.key,
        operation=claim.operation,
        request_sha256=claim.request_sha256,
        response_status=_IDEMPOTENCY_PENDING_STATUS,
    ).delete()


def _execute_mutation(
    request: HttpRequest,
    *,
    session,
    operation: str,
    command: Callable[[], tuple[dict, int]],
) -> JsonResponse:
    claim = _claim_mutation(request, session=session, operation=operation)
    if isinstance(claim, JsonResponse):
        return claim
    try:
        data, status = command()
    except Exception:
        _release_mutation(claim)
        raise
    return _complete_mutation(claim, data, status=status)


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
    active_devices = EventInstallation.objects.filter(
        event=event,
        status=InstallationStatus.ACTIVE.value,
    ).count()
    installation = EventInstallation.objects.filter(
        event=event,
        user=session.user,
        installation_id=session.installation_id,
        status=InstallationStatus.ACTIVE.value,
    ).first()
    policy = PreviewPolicy.objects.filter(event=event).first()
    active_sub_events = sorted(
        event.sub_events.filter(is_archived=False),
        key=lambda item: (item.position, item.name, str(item.id)),
    )
    return EventSnapshot(
        id=event.id,
        name=event.name,
        state=EventState(event.state),
        storage_limit_bytes=event.storage_limit_bytes,
        reserved_original_bytes=event.reserved_original_bytes,
        verified_original_bytes=event.verified_original_bytes,
        remaining_original_bytes=event.storage_limit_bytes - event.reserved_original_bytes,
        intake_state=IntakeState(event.intake_state),
        intake_generation=event.intake_generation,
        processing_profile_id=event.processing_profile_id,
        face_model_id=event.face_model_id,
        face_index_ready=(event.face_index_ready_generation == event.intake_generation),
        max_contribution_devices=event.max_contribution_devices,
        active_contribution_devices=active_devices,
        device_label=installation.label if installation else "",
        sub_events=tuple(
            SubEventSnapshot(id=item.id, name=item.name, position=item.position)
            for item in active_sub_events
        ),
        preview_policy=preview_policy_snapshot(policy),
    ).as_dict()


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
        tokens = authenticate_photographer(
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
def logout(request: HttpRequest) -> JsonResponse:
    photographer = _tenant(request)
    try:
        body = _json_body(request, fields={"refresh_token"})
        revoke_session(
            str(body["refresh_token"]),
            photographer=photographer,
            request=request,
        )
    except (ContractError, DesktopAuthError) as exc:
        return _domain_error(exc)
    return JsonResponse({"revoked": True})


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
def reserve_batch(request: HttpRequest, event_id: UUID) -> JsonResponse:
    try:
        session = _bearer_session(request)

        def command() -> tuple[dict, int]:
            body = _json_body(
                request,
                fields={
                    "batch_id",
                    "sub_event_id",
                    "label",
                    "processing_profile_id",
                    "device_label",
                    "assets",
                },
            )
            contribution = ContributionInput.from_dict(body)
            batch = reserve_contribution(
                session=session,
                event_id=event_id,
                contribution=contribution,
                request=request,
            )
            return _batch_data(batch, session=session), 201

        return _execute_mutation(
            request,
            session=session,
            operation="reserve_batch",
            command=command,
        )
    except (ContractError, DesktopAuthError, IngestionError) as exc:
        return _domain_error(exc)


@require_GET
def batch_detail(request: HttpRequest, event_id: UUID, batch_id: UUID) -> JsonResponse:
    try:
        session = _bearer_session(request)
        event = event_for_session(session, event_id)
        query = ContributionBatch.objects.select_related("installation", "sub_event").filter(
            pk=batch_id,
            installation__event=event,
        )
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
def confirm_preview_policy_view(request: HttpRequest, event_id: UUID) -> JsonResponse:
    try:
        session = _bearer_session(request)

        def command() -> tuple[dict, int]:
            body = _json_body(
                request,
                fields={"enabled", "template", "text", "logo_kind", "mark_png_base64"},
            )
            value = PreviewPolicyInput.from_dict(body)
            policy = confirm_preview_policy(
                session=session,
                event_id=event_id,
                value=value,
                object_store=configured_object_store() if value.enabled else None,
                request=request,
            )
            policy_data = preview_policy_snapshot(policy)
            return {"preview_policy": policy_data.as_dict() if policy_data else None}, 201

        return _execute_mutation(
            request,
            session=session,
            operation="confirm_preview_policy",
            command=command,
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


@require_GET
def preview_policy_mark(request: HttpRequest, event_id: UUID) -> JsonResponse:
    try:
        session = _bearer_session(request)
        data = issue_policy_mark_url(
            session=session,
            event_id=event_id,
            object_store=configured_object_store(),
        )
    except ImproperlyConfigured:
        return _error(
            "object_store_unavailable",
            "Object storage is not configured.",
            status=503,
            retryable=True,
        )
    except (DesktopAuthError, IngestionError) as exc:
        return _domain_error(exc)
    return JsonResponse(data)


@csrf_exempt
@require_POST
def register_derivatives(request: HttpRequest, event_id: UUID, asset_id: UUID) -> JsonResponse:
    try:
        session = _bearer_session(request)

        def command() -> tuple[dict, int]:
            body = _json_body(
                request,
                fields={
                    "asset_id",
                    "source_sha256",
                    "policy_id",
                    "profile_id",
                    "captured_at",
                    "objects",
                },
            )
            value = AssetDerivativesInput.from_dict(body)
            if value.asset_id != asset_id:
                raise ContractError("invalid_request", "The body asset ID must match the route.")
            objects = register_asset_derivatives(
                session=session,
                event_id=event_id,
                value=value,
            )
            return {
                "asset_id": str(asset_id),
                "objects": [
                    {
                        "variant": item.variant,
                        "state": item.state,
                        "failure_code": item.failure_code,
                    }
                    for item in objects
                ],
            }, 201

        return _execute_mutation(
            request,
            session=session,
            operation="register_derivatives",
            command=command,
        )
    except (ContractError, DesktopAuthError, IngestionError) as exc:
        return _domain_error(exc)


@csrf_exempt
@require_POST
def derivative_leases(request: HttpRequest, event_id: UUID, asset_id: UUID) -> JsonResponse:
    try:
        session = _bearer_session(request)
        body = _json_body(request, fields={"variants"})
        if not isinstance(body["variants"], list) or not 1 <= len(body["variants"]) <= 2:
            raise ContractError("invalid_request", "Choose one or two derivative variants.")
        try:
            variants = tuple(AssetVariant(str(value)) for value in body["variants"])
        except ValueError as exc:
            raise ContractError(
                "invalid_derivative_variant", "Unknown derivative variant."
            ) from exc
        if any(variant not in DERIVATIVE_VARIANTS for variant in variants):
            raise ContractError("invalid_derivative_variant", "Unknown derivative variant.")
        leases = issue_derivative_leases(
            session=session,
            event_id=event_id,
            asset_id=asset_id,
            variants=variants,
            object_store=configured_object_store(),
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
def complete_derivative(
    request: HttpRequest, event_id: UUID, asset_id: UUID, variant: str
) -> JsonResponse:
    try:
        session = _bearer_session(request)
        try:
            parsed_variant = AssetVariant(variant)
        except ValueError as exc:
            raise ContractError(
                "invalid_derivative_variant", "Unknown derivative variant."
            ) from exc
        if parsed_variant not in DERIVATIVE_VARIANTS:
            raise ContractError("invalid_derivative_variant", "Unknown derivative variant.")

        def command() -> tuple[dict, int]:
            _json_body(request, fields=set())
            upload = verify_derivative(
                session=session,
                event_id=event_id,
                asset_id=asset_id,
                variant=parsed_variant,
                object_store=configured_object_store(),
                request=request,
            )
            return {
                "asset_id": str(asset_id),
                "variant": upload.variant,
                "state": upload.state,
            }, 200

        return _execute_mutation(
            request,
            session=session,
            operation=f"complete_derivative_{parsed_variant.value}",
            command=command,
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


@csrf_exempt
@require_POST
def owned_original_url(request: HttpRequest, event_id: UUID, asset_id: UUID) -> JsonResponse:
    try:
        session = _bearer_session(request)
        _json_body(request, fields=set())
        data = issue_owned_original_url(
            session=session,
            event_id=event_id,
            asset_id=asset_id,
            object_store=configured_object_store(),
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
    return JsonResponse(data)


@csrf_exempt
@require_POST
def derivative_failure(request: HttpRequest, event_id: UUID, asset_id: UUID) -> JsonResponse:
    try:
        session = _bearer_session(request)

        def command() -> tuple[dict, int]:
            body = _json_body(request, fields={"code"})
            asset = report_derivative_failure(
                session=session,
                event_id=event_id,
                asset_id=asset_id,
                code=str(body["code"]),
                request=request,
            )
            return {
                "asset_id": str(asset.id),
                "failure_code": asset.derivative_failure_code,
                "attempt_count": asset.derivative_attempt_count,
            }, 200

        return _execute_mutation(
            request,
            session=session,
            operation="derivative_failure",
            command=command,
        )
    except (ContractError, DesktopAuthError, IngestionError) as exc:
        return _domain_error(exc)


@csrf_exempt
@require_POST
def face_analysis(
    request: HttpRequest,
    event_id: UUID,
    sub_event_id: UUID,
    asset_id: UUID,
) -> JsonResponse:
    try:
        session = _bearer_session(request)

        def command() -> tuple[dict, int]:
            body = _json_body(
                request,
                fields={
                    "asset_id",
                    "source_sha256",
                    "model_contract",
                    "detected_face_count",
                    "usable_face_count",
                    "status",
                    "faces",
                },
            )
            document = FaceAnalysisDocument.from_dict(
                body,
                accepted_contract=ACCEPTED_FACE_MODEL_CONTRACT,
                accepted_detector_floor=ACCEPTED_SFACE_DETECTOR_FLOOR,
            )
            if document.asset_id != asset_id:
                raise FaceAnalysisDocumentError(
                    "invalid_face_analysis", "The body asset ID must match the route."
                )
            analysis = submit_face_analysis(
                session=session,
                event_id=event_id,
                sub_event_id=sub_event_id,
                document=document,
                request=request,
            )
            return _face_analysis_data(analysis), 200

        return _execute_mutation(
            request,
            session=session,
            operation="submit_face_analysis",
            command=command,
        )
    except FaceAnalysisDocumentError as exc:
        return _error(exc.code, str(exc), status=400)
    except (ContractError, DesktopAuthError, IngestionError) as exc:
        return _domain_error(exc)


@csrf_exempt
@require_POST
def face_analysis_failure(
    request: HttpRequest,
    event_id: UUID,
    sub_event_id: UUID,
    asset_id: UUID,
) -> JsonResponse:
    try:
        session = _bearer_session(request)

        def command() -> tuple[dict, int]:
            body = _json_body(request, fields={"code"})
            analysis = report_face_analysis_failure(
                session=session,
                event_id=event_id,
                sub_event_id=sub_event_id,
                asset_id=asset_id,
                code=str(body["code"]),
                request=request,
            )
            return _face_analysis_data(analysis), 200

        return _execute_mutation(
            request,
            session=session,
            operation="report_face_analysis_failure",
            command=command,
        )
    except (ContractError, DesktopAuthError, IngestionError) as exc:
        return _domain_error(exc)


@csrf_exempt
@require_POST
def complete_asset(request: HttpRequest, event_id: UUID, asset_id: UUID) -> JsonResponse:
    try:
        session = _bearer_session(request)

        def command() -> tuple[dict, int]:
            _json_body(request, fields=set())
            upload = verify_uploaded_object(
                session=session,
                event_id=event_id,
                asset_id=asset_id,
                object_store=configured_object_store(),
                request=request,
            )
            return {"asset_id": str(upload.asset_id), "state": upload.state}, 200

        return _execute_mutation(
            request,
            session=session,
            operation="complete_asset",
            command=command,
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


@csrf_exempt
@require_POST
def exclude_asset_view(request: HttpRequest, event_id: UUID, asset_id: UUID) -> JsonResponse:
    try:
        session = _bearer_session(request)

        def command() -> tuple[dict, int]:
            body = _json_body(request, fields={"reason"})
            upload = exclude_asset(
                session=session,
                event_id=event_id,
                asset_id=asset_id,
                reason=str(body["reason"]),
                object_store=configured_object_store(),
                request=request,
            )
            return {"asset_id": str(upload.asset_id), "state": upload.state}, 200

        return _execute_mutation(
            request,
            session=session,
            operation="exclude_asset",
            command=command,
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


@csrf_exempt
@require_POST
def cancel_batch_view(request: HttpRequest, event_id: UUID, batch_id: UUID) -> JsonResponse:
    try:
        session = _bearer_session(request)

        def command() -> tuple[dict, int]:
            _json_body(request, fields=set())
            batch = cancel_batch(
                session=session,
                event_id=event_id,
                batch_id=batch_id,
                object_store=configured_object_store(),
                request=request,
            )
            return {"batch_id": str(batch.id), "state": batch.state}, 200

        return _execute_mutation(
            request,
            session=session,
            operation="cancel_batch",
            command=command,
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


@csrf_exempt
@require_POST
def revoke_installation_view(
    request: HttpRequest, event_id: UUID, installation_id: UUID
) -> JsonResponse:
    try:
        session = _bearer_session(request)

        def command() -> tuple[dict, int]:
            _json_body(request, fields=set())
            installation = revoke_installation(
                session=session,
                event_id=event_id,
                installation_id=installation_id,
                request=request,
            )
            return {
                "installation_id": str(installation.id),
                "status": installation.status,
            }, 200

        return _execute_mutation(
            request,
            session=session,
            operation="revoke_installation",
            command=command,
        )
    except (ContractError, DesktopAuthError, IngestionError) as exc:
        return _domain_error(exc)


def _batch_data(batch: ContributionBatch, *, session) -> dict:
    objects = AssetObject.objects.filter(asset__batch=batch)
    originals = objects.filter(variant=AssetVariant.ORIGINAL.value)
    data = {
        "id": str(batch.id),
        "sub_event_id": str(batch.sub_event_id),
        "state": batch.state,
        "generation": batch.intake_generation,
        "asset_count": batch.declared_asset_count,
        "original_bytes": batch.declared_original_bytes,
        "verified_asset_count": originals.filter(state="verified").count(),
        "failed_asset_count": originals.filter(state="failed").count(),
        "excluded_asset_count": originals.filter(state="excluded").count(),
        "verified_derivative_count": objects.exclude(variant=AssetVariant.ORIGINAL.value)
        .filter(state="verified")
        .count(),
    }
    analyses = {
        analysis.asset_id: analysis for analysis in FaceAnalysis.objects.filter(asset__batch=batch)
    }
    data["assets"] = list(
        objects.order_by("asset_id").values(
            "asset_id",
            "variant",
            "expected_bytes",
            "state",
            "failure_code",
            "asset__gallery_excluded_at",
            "asset__derivative_attempt_count",
        )
    )
    for item in data["assets"]:
        asset_id = item["asset_id"]
        item["asset_id"] = str(asset_id)
        item["gallery_excluded"] = item.pop("asset__gallery_excluded_at") is not None
        derivative_attempt_count = item.pop("asset__derivative_attempt_count")
        if item["variant"] == AssetVariant.ORIGINAL.value:
            item["face_analysis"] = _face_analysis_data(analyses.get(asset_id))
        else:
            item["attempt_count"] = derivative_attempt_count
    return data


def _face_analysis_data(analysis: FaceAnalysis | None) -> dict:
    if analysis is None:
        return {
            "state": "pending",
            "attempt_count": 0,
            "failure_code": "",
            "detected_face_count": 0,
            "usable_face_count": 0,
        }
    return {
        "state": analysis.state,
        "attempt_count": analysis.attempt_count,
        "failure_code": analysis.failure_code,
        "detected_face_count": analysis.detected_face_count,
        "usable_face_count": analysis.usable_face_count,
    }
