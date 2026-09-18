"""Database-backed fixed-window limits for authentication failures."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from django.conf import settings
from django.db import transaction
from django.http import HttpRequest
from django.utils import timezone

from .audit import privacy_hash, request_client_hash
from .models import RateLimitBucket, RateLimitPurpose


@dataclass(frozen=True, slots=True)
class RateLimitStatus:
    limited: bool
    retry_after_seconds: int


def _window_start(at: datetime, window_seconds: int) -> datetime:
    epoch_seconds = int(at.timestamp())
    start_seconds = epoch_seconds - (epoch_seconds % window_seconds)
    return datetime.fromtimestamp(start_seconds, tz=UTC)


def _limit_settings(purpose: RateLimitPurpose) -> tuple[int, int]:
    if purpose is RateLimitPurpose.FACE_SEARCH_CLIENT:
        return settings.FACE_SEARCH_CLIENT_LIMIT, settings.FACE_SEARCH_LIMIT_WINDOW_SECONDS
    if purpose is RateLimitPurpose.FACE_SEARCH_CAPABILITY:
        return settings.FACE_SEARCH_CAPABILITY_LIMIT, settings.FACE_SEARCH_LIMIT_WINDOW_SECONDS
    return settings.AUTH_FAILURE_LIMIT, settings.AUTH_FAILURE_WINDOW_SECONDS


def _bucket_identity(
    *,
    purpose: RateLimitPurpose,
    subject: str,
    request: HttpRequest,
    at: datetime,
    client_scoped: bool,
    client_identifier: str | None = None,
) -> dict:
    _, window_seconds = _limit_settings(purpose)
    return {
        "purpose": purpose,
        "subject_hash": privacy_hash(subject.casefold(), purpose=f"rate-limit:{purpose}"),
        "client_hash": _client_hash(
            purpose=purpose,
            request=request,
            client_scoped=client_scoped,
            client_identifier=client_identifier,
        ),
        "window_started_at": _window_start(at, window_seconds),
    }


def _client_hash(
    *,
    purpose: RateLimitPurpose,
    request: HttpRequest,
    client_scoped: bool,
    client_identifier: str | None,
) -> str:
    if not client_scoped:
        return privacy_hash("all-clients", purpose=f"rate-limit:{purpose}")
    if client_identifier:
        return privacy_hash(client_identifier, purpose=f"rate-limit-client:{purpose}")
    return request_client_hash(request)


def _retry_after(at: datetime, window_started_at: datetime, window_seconds: int) -> int:
    window_end = window_started_at + timedelta(seconds=window_seconds)
    return max(1, int((window_end - at).total_seconds()) + 1)


def rate_limit_status(
    *,
    purpose: RateLimitPurpose,
    subject: str,
    request: HttpRequest,
    at: datetime | None = None,
    client_scoped: bool = True,
) -> RateLimitStatus:
    checked_at = at or timezone.now()
    failure_limit, window_seconds = _limit_settings(purpose)
    identity = _bucket_identity(
        purpose=purpose,
        subject=subject,
        request=request,
        at=checked_at,
        client_scoped=client_scoped,
    )
    bucket = RateLimitBucket.objects.filter(**identity).only("failures").first()
    return RateLimitStatus(
        limited=bucket is not None and bucket.failures >= failure_limit,
        retry_after_seconds=_retry_after(
            checked_at,
            identity["window_started_at"],
            window_seconds,
        ),
    )


@transaction.atomic
def register_failure(
    *,
    purpose: RateLimitPurpose,
    subject: str,
    request: HttpRequest,
    at: datetime | None = None,
    client_scoped: bool = True,
) -> RateLimitStatus:
    checked_at = at or timezone.now()
    failure_limit, window_seconds = _limit_settings(purpose)
    identity = _bucket_identity(
        purpose=purpose,
        subject=subject,
        request=request,
        at=checked_at,
        client_scoped=client_scoped,
    )
    RateLimitBucket.objects.filter(
        purpose=purpose,
        subject_hash=identity["subject_hash"],
        client_hash=identity["client_hash"],
    ).exclude(window_started_at=identity["window_started_at"]).delete()
    bucket, _ = RateLimitBucket.objects.select_for_update().get_or_create(
        **identity,
        defaults={"failures": 0},
    )
    bucket.failures += 1
    bucket.save(update_fields=("failures",))
    return RateLimitStatus(
        limited=bucket.failures >= failure_limit,
        retry_after_seconds=_retry_after(
            checked_at,
            identity["window_started_at"],
            window_seconds,
        ),
    )


def clear_failures(
    *,
    purpose: RateLimitPurpose,
    subject: str,
    request: HttpRequest,
    client_scoped: bool = True,
) -> None:
    identity = _bucket_identity(
        purpose=purpose,
        subject=subject,
        request=request,
        at=timezone.now(),
        client_scoped=client_scoped,
    )
    RateLimitBucket.objects.filter(
        purpose=purpose,
        subject_hash=identity["subject_hash"],
        client_hash=identity["client_hash"],
    ).delete()


@transaction.atomic
def consume_attempt(
    *,
    purpose: RateLimitPurpose,
    subject: str,
    request: HttpRequest,
    at: datetime | None = None,
    client_scoped: bool = True,
    client_identifier: str | None = None,
) -> RateLimitStatus:
    """Consume one allowed request while rejecting only attempts beyond the configured limit."""
    checked_at = at or timezone.now()
    attempt_limit, window_seconds = _limit_settings(purpose)
    identity = _bucket_identity(
        purpose=purpose,
        subject=subject,
        request=request,
        at=checked_at,
        client_scoped=client_scoped,
        client_identifier=client_identifier,
    )
    RateLimitBucket.objects.filter(
        purpose=purpose,
        subject_hash=identity["subject_hash"],
        client_hash=identity["client_hash"],
    ).exclude(window_started_at=identity["window_started_at"]).delete()
    bucket, _ = RateLimitBucket.objects.select_for_update().get_or_create(
        **identity,
        defaults={"failures": 0},
    )
    if bucket.failures >= attempt_limit:
        return RateLimitStatus(
            limited=True,
            retry_after_seconds=_retry_after(
                checked_at,
                identity["window_started_at"],
                window_seconds,
            ),
        )
    bucket.failures += 1
    bucket.save(update_fields=("failures",))
    return RateLimitStatus(
        limited=False,
        retry_after_seconds=_retry_after(
            checked_at,
            identity["window_started_at"],
            window_seconds,
        ),
    )
