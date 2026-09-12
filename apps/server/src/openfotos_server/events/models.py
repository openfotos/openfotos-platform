"""Persistent authorization and event-lifecycle records."""

import re
import secrets
from uuid import uuid4

from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator, RegexValidator
from django.db import models
from django.db.models import F, Q
from django.utils import timezone

from openfotos_contracts import (
    DERIVATIVE_PROFILE_ID,
    WATERMARK_RENDERER_ID,
    ContributionState,
    DeviceRole,
    DeviceStatus,
    EventState,
    IngestionManifestState,
    IntakeState,
    OriginalDownloadPolicy,
    UploadObjectState,
    WatermarkLogoKind,
    WatermarkTemplate,
)
from openfotos_storage import AssetVariant

PILOT_STORAGE_LIMIT_BYTES = 25_000_000_000
RESERVED_PHOTOGRAPHER_SLUGS = frozenset({"admin", "api", "media", "static", "www"})
PIN_PATTERN = re.compile(r"[0-9]{6}\Z")
SHA256_VALIDATOR = RegexValidator(r"^[0-9a-f]{64}$", "Enter a lowercase SHA-256 digest.")
MD5_VALIDATOR = RegexValidator(r"^[A-Za-z0-9+/]{22}==$", "Enter a base64-encoded MD5 digest.")


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
    DESKTOP_LOGIN = "desktop.login", "Desktop login"
    DESKTOP_TOKEN_REFRESH = "desktop.token_refresh", "Desktop token refresh"
    UPLOADER_INVITATION_CREATED = (
        "uploader_invitation.created",
        "Uploader invitation created",
    )
    UPLOADER_INVITATION_REDEEMED = (
        "uploader_invitation.redeemed",
        "Uploader invitation redeemed",
    )
    UPLOADER_INVITATION_REVOKED = (
        "uploader_invitation.revoked",
        "Uploader invitation revoked",
    )
    UPLOADER_DEVICE_REVOKED = "uploader_device.revoked", "Uploader device revoked"
    CONTRIBUTION_RESERVED = "contribution.reserved", "Contribution reserved"
    CONTRIBUTION_CANCELLED = "contribution.cancelled", "Contribution cancelled"
    ASSET_UPLOAD_VERIFIED = "asset_upload.verified", "Asset upload verified"
    ASSET_EXCLUDED = "asset.excluded", "Asset excluded"
    EVENT_INTAKE_CLOSED = "event.intake_closed", "Event intake closed"
    EVENT_INTAKE_REOPENED = "event.intake_reopened", "Event intake reopened"
    EVENT_INGESTION_FINALIZED = "event.ingestion_finalized", "Event ingestion finalized"
    PREVIEW_POLICY_CONFIRMED = "preview_policy.confirmed", "Preview policy confirmed"
    DERIVATIVE_UPLOAD_VERIFIED = "derivative_upload.verified", "Derivative upload verified"
    DERIVATIVE_FAILED = "derivative.failed", "Derivative failed"
    ASSET_GALLERY_EXCLUDED = "asset.gallery_excluded", "Asset excluded from gallery"
    ASSET_GALLERY_RESTORED = "asset.gallery_restored", "Asset restored to gallery"
    DOWNLOAD_POLICY_CHANGED = "event.download_policy_changed", "Download policy changed"


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
    reserved_original_bytes = models.PositiveBigIntegerField(default=0, editable=False)
    verified_original_bytes = models.PositiveBigIntegerField(default=0, editable=False)
    max_contribution_devices = models.PositiveSmallIntegerField(
        default=10,
        validators=[MinValueValidator(1), MaxValueValidator(10)],
    )
    intake_state = models.CharField(
        max_length=16,
        choices=tuple((state.value, state.value.title()) for state in IntakeState),
        default=IntakeState.OPEN.value,
        editable=False,
    )
    intake_generation = models.PositiveIntegerField(default=1, editable=False)
    processing_profile_id = models.CharField(max_length=100, default="pilot-profile-v1")
    original_download_policy = models.CharField(
        max_length=24,
        choices=tuple(
            (policy.value, policy.value.replace("-", " ").title())
            for policy in OriginalDownloadPolicy
        ),
        default=OriginalDownloadPolicy.DISABLED.value,
    )
    derivatives_ready_generation = models.PositiveIntegerField(
        blank=True, null=True, editable=False
    )
    current_ingestion_manifest = models.ForeignKey(
        "IngestionManifest",
        blank=True,
        null=True,
        editable=False,
        on_delete=models.PROTECT,
        related_name="current_for_events",
    )
    expires_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=~Q(state=EventState.PUBLISHED.value) | Q(expires_at__isnull=False),
                name="published_event_has_expiry",
            ),
            models.CheckConstraint(
                condition=Q(max_contribution_devices__gte=1) & Q(max_contribution_devices__lte=10),
                name="event_device_limit_between_1_and_10",
            ),
            models.CheckConstraint(
                condition=Q(verified_original_bytes__lte=F("reserved_original_bytes")),
                name="event_verified_bytes_within_reserved",
            ),
            models.CheckConstraint(
                condition=Q(reserved_original_bytes__lte=F("storage_limit_bytes")),
                name="event_reserved_bytes_within_limit",
            ),
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


class UploaderInvitation(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    event = models.ForeignKey(Event, on_delete=models.PROTECT, related_name="uploader_invitations")
    token_hash = models.CharField(max_length=64, unique=True, editable=False)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_uploader_invitations",
    )
    expires_at = models.DateTimeField()
    max_redemptions = models.PositiveSmallIntegerField(
        default=10,
        validators=[MinValueValidator(1), MaxValueValidator(10)],
    )
    redemption_count = models.PositiveSmallIntegerField(default=0, editable=False)
    revoked_at = models.DateTimeField(blank=True, null=True, editable=False)
    closed_at = models.DateTimeField(blank=True, null=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=("event", "expires_at"), name="invite_event_expiry_idx")]
        constraints = [
            models.CheckConstraint(
                condition=Q(max_redemptions__gte=1) & Q(max_redemptions__lte=10),
                name="invitation_redemptions_between_1_and_10",
            ),
            models.CheckConstraint(
                condition=Q(redemption_count__lte=F("max_redemptions")),
                name="invitation_redemptions_within_limit",
            ),
        ]


class UploaderDevice(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    event = models.ForeignKey(Event, on_delete=models.PROTECT, related_name="uploader_devices")
    installation_id = models.UUIDField()
    label = models.CharField(max_length=100)
    role = models.CharField(
        max_length=16,
        choices=tuple((role.value, role.value.title()) for role in DeviceRole),
    )
    status = models.CharField(
        max_length=16,
        choices=tuple((status.value, status.value.title()) for status in DeviceStatus),
        default=DeviceStatus.ACTIVE.value,
    )
    invitation = models.ForeignKey(
        UploaderInvitation,
        blank=True,
        null=True,
        on_delete=models.PROTECT,
        related_name="devices",
    )
    revoked_at = models.DateTimeField(blank=True, null=True, editable=False)
    last_active_at = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("event", "installation_id"),
                name="unique_event_installation",
            )
        ]
        indexes = [models.Index(fields=("event", "status"), name="device_event_status_idx")]


class DesktopSession(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    photographer = models.ForeignKey(
        Photographer,
        on_delete=models.PROTECT,
        related_name="desktop_sessions",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        blank=True,
        null=True,
        on_delete=models.PROTECT,
        related_name="openfotos_desktop_sessions",
    )
    device = models.ForeignKey(
        UploaderDevice,
        blank=True,
        null=True,
        on_delete=models.PROTECT,
        related_name="sessions",
    )
    installation_id = models.UUIDField()
    access_token_hash = models.CharField(max_length=64, unique=True, editable=False)
    access_expires_at = models.DateTimeField()
    refresh_token_hash = models.CharField(max_length=64, unique=True, editable=False)
    previous_refresh_token_hash = models.CharField(max_length=64, blank=True, db_index=True)
    refresh_expires_at = models.DateTimeField()
    revoked_at = models.DateTimeField(blank=True, null=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(Q(user__isnull=False) & Q(device__isnull=True))
                | (Q(user__isnull=True) & Q(device__isnull=False)),
                name="desktop_session_has_one_actor",
            )
        ]


class ContributionBatch(models.Model):
    id = models.UUIDField(primary_key=True, editable=False)
    device = models.ForeignKey(
        UploaderDevice,
        on_delete=models.PROTECT,
        related_name="contribution_batches",
    )
    intake_generation = models.PositiveIntegerField()
    state = models.CharField(
        max_length=16,
        choices=tuple((state.value, state.value.title()) for state in ContributionState),
        default=ContributionState.RESERVED.value,
    )
    label = models.CharField(max_length=100, blank=True)
    processing_profile_id = models.CharField(max_length=100)
    declared_asset_count = models.PositiveIntegerField()
    declared_original_bytes = models.PositiveBigIntegerField()
    manifest_sha256 = models.CharField(max_length=64, validators=[SHA256_VALIDATOR])
    completed_at = models.DateTimeField(blank=True, null=True, editable=False)
    cancelled_at = models.DateTimeField(blank=True, null=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(
                fields=("device", "intake_generation", "state"), name="batch_device_gen_idx"
            )
        ]


class Asset(models.Model):
    id = models.UUIDField(primary_key=True, editable=False)
    batch = models.ForeignKey(
        ContributionBatch,
        on_delete=models.PROTECT,
        related_name="assets",
    )
    original_filename = models.CharField(max_length=255)
    width = models.PositiveIntegerField()
    height = models.PositiveIntegerField()
    sha256 = models.CharField(max_length=64, validators=[SHA256_VALIDATOR])
    captured_at = models.DateTimeField(blank=True, null=True)
    gallery_position = models.PositiveIntegerField(blank=True, null=True)
    gallery_excluded_at = models.DateTimeField(blank=True, null=True, editable=False)
    gallery_exclusion_reason = models.CharField(max_length=240, blank=True)
    gallery_excluded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        blank=True,
        null=True,
        on_delete=models.PROTECT,
        related_name="gallery_excluded_assets",
    )
    derivative_failure_code = models.CharField(max_length=64, blank=True)
    derivative_failure_at = models.DateTimeField(blank=True, null=True, editable=False)
    derivative_attempt_count = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=("batch",), name="asset_batch_idx")]


class AssetObject(models.Model):
    asset = models.ForeignKey(Asset, on_delete=models.PROTECT, related_name="variant_objects")
    variant = models.CharField(
        max_length=16,
        choices=tuple((variant.value, variant.name.title()) for variant in AssetVariant),
    )
    object_key = models.CharField(max_length=255, unique=True)
    expected_bytes = models.PositiveBigIntegerField()
    sha256 = models.CharField(max_length=64, validators=[SHA256_VALIDATOR])
    content_md5 = models.CharField(max_length=24, validators=[MD5_VALIDATOR])
    width = models.PositiveIntegerField()
    height = models.PositiveIntegerField()
    state = models.CharField(
        max_length=16,
        choices=tuple((state.value, state.value.title()) for state in UploadObjectState),
        default=UploadObjectState.RESERVED.value,
    )
    etag = models.CharField(max_length=128, blank=True)
    lease_expires_at = models.DateTimeField(blank=True, null=True)
    verified_at = models.DateTimeField(blank=True, null=True, editable=False)
    failure_code = models.CharField(max_length=64, blank=True)
    excluded_reason = models.CharField(max_length=240, blank=True)
    excluded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        blank=True,
        null=True,
        on_delete=models.PROTECT,
        related_name="excluded_asset_objects",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("asset", "variant"),
                name="unique_asset_variant",
            )
        ]
        indexes = [models.Index(fields=("state",), name="object_state_idx")]


class PreviewPolicy(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    event = models.OneToOneField(
        Event,
        on_delete=models.PROTECT,
        related_name="preview_policy",
    )
    enabled = models.BooleanField(default=False)
    template = models.CharField(
        max_length=32,
        choices=tuple(
            (template.value, template.value.replace("-", " ").title())
            for template in WatermarkTemplate
        ),
        default=WatermarkTemplate.COMPACT_BOTTOM_RIGHT.value,
    )
    text = models.CharField(max_length=60, blank=True)
    logo_kind = models.CharField(
        max_length=16,
        choices=tuple((kind.value, kind.value.upper()) for kind in WatermarkLogoKind),
        default=WatermarkLogoKind.NONE.value,
    )
    renderer_id = models.CharField(max_length=100, default=WATERMARK_RENDERER_ID, editable=False)
    derivative_profile_id = models.CharField(
        max_length=100,
        default=DERIVATIVE_PROFILE_ID,
        editable=False,
    )
    mark_object_key = models.CharField(max_length=255, blank=True, unique=True, null=True)
    mark_sha256 = models.CharField(max_length=64, blank=True, validators=[SHA256_VALIDATOR])
    mark_bytes = models.PositiveIntegerField(default=0)
    mark_width = models.PositiveIntegerField(default=0)
    mark_height = models.PositiveIntegerField(default=0)
    confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="confirmed_preview_policies",
    )
    confirmed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(enabled=False, mark_object_key__isnull=True, mark_bytes=0)
                    | Q(
                        enabled=True,
                        mark_object_key__isnull=False,
                        mark_bytes__gt=0,
                        mark_width__gt=0,
                        mark_height__gt=0,
                    )
                ),
                name="preview_policy_mark_matches_enabled",
            )
        ]


class IngestionManifest(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    event = models.ForeignKey(Event, on_delete=models.PROTECT, related_name="ingestion_manifests")
    generation = models.PositiveIntegerField()
    state = models.CharField(
        max_length=16,
        choices=tuple((state.value, state.value.title()) for state in IngestionManifestState),
        default=IngestionManifestState.PREPARED.value,
    )
    object_key = models.CharField(max_length=255, unique=True)
    content_sha256 = models.CharField(max_length=64, validators=[SHA256_VALIDATOR])
    document = models.JSONField()
    asset_count = models.PositiveIntegerField()
    original_bytes = models.PositiveBigIntegerField()
    excluded_asset_count = models.PositiveIntegerField(default=0)
    committed_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("event", "generation"),
                name="unique_event_manifest_generation",
            )
        ]


class IdempotencyRecord(models.Model):
    actor_key = models.CharField(max_length=80)
    key = models.UUIDField()
    operation = models.CharField(max_length=80)
    request_sha256 = models.CharField(max_length=64, validators=[SHA256_VALIDATOR])
    response_status = models.PositiveSmallIntegerField()
    response_body = models.JSONField()
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("actor_key", "key"),
                name="unique_actor_idempotency_key",
            )
        ]
        indexes = [models.Index(fields=("expires_at",), name="idempotency_expiry_idx")]


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
    uploader_device = models.ForeignKey(
        UploaderDevice,
        blank=True,
        null=True,
        on_delete=models.SET_NULL,
        related_name="audit_events",
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
