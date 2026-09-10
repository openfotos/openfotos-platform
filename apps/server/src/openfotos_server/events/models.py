"""Persistent authorization and event-lifecycle records."""

import re
import secrets
from uuid import uuid4

from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone

from openfotos_contracts import EventState

PILOT_STORAGE_LIMIT_BYTES = 25_000_000_000
RESERVED_PHOTOGRAPHER_SLUGS = frozenset({"admin", "api", "media", "static", "www"})
PIN_PATTERN = re.compile(r"[0-9]{6}\Z")


def generate_event_token() -> str:
    """Generate a durable, unguessable public event locator."""
    return secrets.token_urlsafe(32)


class PhotographerStatus(models.TextChoices):
    ACTIVE = "active", "Active"
    INACTIVE = "inactive", "Inactive"


class MembershipRole(models.TextChoices):
    PHOTOGRAPHER = "photographer", "Photographer"


class AuditAction(models.TextChoices):
    PHOTOGRAPHER_CREATED = "photographer.created", "Photographer created"
    PHOTOGRAPHER_CHANGED = "photographer.changed", "Photographer changed"
    MEMBERSHIP_CREATED = "membership.created", "Membership created"
    MEMBERSHIP_CHANGED = "membership.changed", "Membership changed"
    EVENT_CREATED = "event.created", "Event created"
    EVENT_CHANGED = "event.changed", "Event changed"
    EVENT_STATE_CHANGED = "event.state_changed", "Event state changed"
    EVENT_PIN_CHANGED = "event.pin_changed", "Event PIN changed"
    EVENT_TOKEN_CHANGED = "event.token_changed", "Event token changed"
    EVENT_ACCESS_REVOKED = "event.access_revoked", "Event access revoked"
    PHOTOGRAPHER_LOGIN = "photographer.login", "Photographer login"
    PHOTOGRAPHER_LOGOUT = "photographer.logout", "Photographer logout"
    EVENT_PIN_UNLOCK = "event.pin_unlock", "Event PIN unlock"


class AuditResult(models.TextChoices):
    SUCCEEDED = "succeeded", "Succeeded"
    DENIED = "denied", "Denied"
    RATE_LIMITED = "rate_limited", "Rate limited"


class RateLimitPurpose(models.TextChoices):
    PHOTOGRAPHER_LOGIN = "photographer_login", "Photographer login"
    EVENT_PIN = "event_pin", "Event PIN"


class Photographer(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    slug = models.SlugField(
        max_length=63,
        unique=True,
        validators=[
            RegexValidator(
                regex=r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$",
                message="Use a lowercase DNS label containing letters, digits, or hyphens.",
            )
        ],
    )
    display_name = models.CharField(max_length=200)
    status = models.CharField(
        max_length=16,
        choices=PhotographerStatus.choices,
        default=PhotographerStatus.ACTIVE,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("display_name", "slug")

    def __str__(self) -> str:
        return self.display_name

    def clean(self) -> None:
        super().clean()
        self.slug = self.slug.lower()
        if self.slug in RESERVED_PHOTOGRAPHER_SLUGS:
            raise ValidationError({"slug": "This hostname is reserved by OpenFotos."})

    def save(self, *args, **kwargs) -> None:
        self.slug = self.slug.lower()
        super().save(*args, **kwargs)


class PhotographerMembership(models.Model):
    photographer = models.ForeignKey(
        Photographer,
        on_delete=models.PROTECT,
        related_name="memberships",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="photographer_memberships",
    )
    role = models.CharField(
        max_length=24,
        choices=MembershipRole.choices,
        default=MembershipRole.PHOTOGRAPHER,
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("photographer", "user"),
                name="unique_photographer_membership",
            )
        ]
        ordering = ("photographer__display_name", "user__username")

    def __str__(self) -> str:
        return f"{self.user} at {self.photographer}"


class Event(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    photographer = models.ForeignKey(
        Photographer,
        on_delete=models.PROTECT,
        related_name="events",
    )
    name = models.CharField(max_length=200)
    public_token = models.CharField(
        max_length=48,
        unique=True,
        default=generate_event_token,
        editable=False,
    )
    pin_hash = models.CharField(max_length=256, editable=False)
    visitor_access_version = models.UUIDField(default=uuid4, editable=False)
    state = models.CharField(
        max_length=16,
        choices=tuple((state.value, state.value.title()) for state in EventState),
        default=EventState.DRAFT.value,
    )
    storage_limit_bytes = models.PositiveBigIntegerField(default=PILOT_STORAGE_LIMIT_BYTES)
    expires_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=~Q(state=EventState.PUBLISHED.value) | Q(expires_at__isnull=False),
                name="published_event_has_expiry",
            )
        ]
        ordering = ("-created_at",)

    def __str__(self) -> str:
        return self.name

    def set_pin(self, raw_pin: str) -> None:
        if not PIN_PATTERN.fullmatch(raw_pin):
            raise ValidationError({"pin": "Enter exactly six ASCII digits."})
        self.pin_hash = make_password(raw_pin, hasher="argon2")

    def check_pin(self, raw_pin: str) -> bool:
        return bool(self.pin_hash) and check_password(raw_pin, self.pin_hash)

    def is_publicly_available(self, *, at=None) -> bool:
        checked_at = at or timezone.now()
        return (
            self.photographer.status == PhotographerStatus.ACTIVE
            and self.state == EventState.PUBLISHED.value
            and self.expires_at is not None
            and self.expires_at > checked_at
        )


class AuditEvent(models.Model):
    photographer = models.ForeignKey(
        Photographer,
        on_delete=models.PROTECT,
        related_name="audit_events",
    )
    event = models.ForeignKey(
        Event,
        blank=True,
        null=True,
        on_delete=models.PROTECT,
        related_name="audit_events",
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        blank=True,
        null=True,
        on_delete=models.SET_NULL,
        related_name="openfotos_audit_events",
    )
    action = models.CharField(max_length=64, choices=AuditAction.choices)
    result = models.CharField(max_length=16, choices=AuditResult.choices)
    client_hash = models.CharField(max_length=64, blank=True)
    request_id = models.UUIDField(default=uuid4, editable=False)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=("photographer", "-created_at"), name="audit_tenant_time_idx"),
            models.Index(fields=("event", "-created_at"), name="audit_event_time_idx"),
        ]
        ordering = ("-created_at",)

    def __str__(self) -> str:
        return f"{self.action}: {self.result}"


class RateLimitBucket(models.Model):
    purpose = models.CharField(max_length=32, choices=RateLimitPurpose.choices)
    subject_hash = models.CharField(max_length=64)
    client_hash = models.CharField(max_length=64)
    window_started_at = models.DateTimeField()
    failures = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("purpose", "subject_hash", "client_hash", "window_started_at"),
                name="unique_rate_limit_window",
            )
        ]
