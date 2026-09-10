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


def _limit_settings() -> tuple[int, int]:
    return settings.AUTH_FAILURE_LIMIT, settings.AUTH_FAILURE_WINDOW_SECONDS


def _bucket_identity(
    *,
    purpose: RateLimitPurpose,
    subject: str,
    request: HttpRequest,
    at: datetime,
) -> dict:
    _, window_seconds = _limit_settings()
    return {
        "purpose": purpose,
        "subject_hash": privacy_hash(subject.casefold(), purpose=f"rate-limit:{purpose}"),
        "client_hash": request_client_hash(request),
        "window_started_at": _window_start(at, window_seconds),
    }


def _retry_after(at: datetime, window_started_at: datetime, window_seconds: int) -> int:
    window_end = window_started_at + timedelta(seconds=window_seconds)
    return max(1, int((window_end - at).total_seconds()) + 1)


def rate_limit_status(
    *,
    purpose: RateLimitPurpose,
    subject: str,
    request: HttpRequest,
    at: datetime | None = None,
) -> RateLimitStatus:
    checked_at = at or timezone.now()
    failure_limit, window_seconds = _limit_settings()
    identity = _bucket_identity(
        purpose=purpose,
        subject=subject,
        request=request,
        at=checked_at,
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
) -> RateLimitStatus:
    checked_at = at or timezone.now()
    failure_limit, window_seconds = _limit_settings()
    identity = _bucket_identity(
        purpose=purpose,
        subject=subject,
        request=request,
        at=checked_at,
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
) -> None:
    identity = _bucket_identity(
        purpose=purpose,
        subject=subject,
        request=request,
        at=timezone.now(),
    )
    RateLimitBucket.objects.filter(
        purpose=purpose,
        subject_hash=identity["subject_hash"],
        client_hash=identity["client_hash"],
    ).delete()
