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
from pgvector.django import VectorField

from openfotos_contracts import (
    DERIVATIVE_PROFILE_ID,
    WATERMARK_RENDERER_ID,
    AssetVariant,
    ContributionState,
    EventState,
    IngestionManifestState,
    InstallationStatus,
    IntakeState,
    UploadObjectState,
    WatermarkLogoKind,
    WatermarkTemplate,
)
from openfotos_vision import ACCEPTED_FACE_MODEL_CONTRACT

PILOT_STORAGE_LIMIT_BYTES = 25_000_000_000
RESERVED_PHOTOGRAPHER_SLUGS = frozenset({"admin", "api", "media", "static", "www"})
PIN_PATTERN = re.compile(r"[0-9]{4}\Z")
SHA256_VALIDATOR = RegexValidator(r"^[0-9a-f]{64}$", "Enter a lowercase SHA-256 digest.")
MD5_VALIDATOR = RegexValidator(r"^[A-Za-z0-9+/]{22}==$", "Enter a base64-encoded MD5 digest.")


def generate_event_token() -> str:
    """Retain the callable referenced by historical migration 0001."""
    return secrets.token_urlsafe(32)


def generate_event_pin() -> str:
    """Retain the callable referenced by historical migration 0004."""
    return f"{secrets.randbelow(10_000):04d}"


def generate_share_pin() -> str:
    """Generate a zero-padded four-digit capability PIN."""
    return f"{secrets.randbelow(10_000):04d}"


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
    PHOTOGRAPHER_LOGIN = "photographer.login", "Photographer login"
    PHOTOGRAPHER_LOGOUT = "photographer.logout", "Photographer logout"
    OWNER_CAPABILITY_ISSUED = "owner_capability.issued", "Owner capability issued"
    OWNER_CAPABILITY_REVOKED = "owner_capability.revoked", "Owner capability revoked"
    OWNER_PIN_UNLOCK = "owner_capability.pin_unlock", "Owner PIN unlock"
    GUEST_CAPABILITY_CREATED = "guest_capability.created", "Guest capability created"
    GUEST_CAPABILITY_REVOKED = "guest_capability.revoked", "Guest capability revoked"
    GUEST_PIN_UNLOCK = "guest_capability.pin_unlock", "Guest PIN unlock"
    SHARE_ACCESS_RESET = "share_access.reset", "Share access reset"
    FACE_SEARCH_COMPLETED = "face_search.completed", "Face search completed"
    FACE_SEARCH_REJECTED = "face_search.rejected", "Face search rejected"
    ORIGINAL_DOWNLOAD_ISSUED = "original_download.issued", "Original download issued"
    DESKTOP_LOGIN = "desktop.login", "Desktop login"
    DESKTOP_TOKEN_REFRESH = "desktop.token_refresh", "Desktop token refresh"
    SUB_EVENT_CREATED = "sub_event.created", "Sub-event created"
    SUB_EVENT_CHANGED = "sub_event.changed", "Sub-event changed"
    SUB_EVENT_ARCHIVED = "sub_event.archived", "Sub-event archived"
    SUB_EVENT_RESTORED = "sub_event.restored", "Sub-event restored"
    EVENT_INSTALLATION_REGISTERED = (
        "event_installation.registered",
        "Event installation registered",
    )
    EVENT_INSTALLATION_REVOKED = (
        "event_installation.revoked",
        "Event installation revoked",
    )
    CONTRIBUTION_RESERVED = "contribution.reserved", "Contribution reserved"
    CONTRIBUTION_REASSIGNED = "contribution.reassigned", "Contribution reassigned"
    CONTRIBUTION_CANCELLED = "contribution.cancelled", "Contribution cancelled"
    ASSET_UPLOAD_VERIFIED = "asset_upload.verified", "Asset upload verified"
    ASSET_EXCLUDED = "asset.excluded", "Asset excluded"
    EVENT_INTAKE_CLOSED = "event.intake_closed", "Event intake closed"
    EVENT_INTAKE_REOPENED = "event.intake_reopened", "Event intake reopened"
    EVENT_INGESTION_FINALIZED = "event.ingestion_finalized", "Event ingestion finalized"
    PREVIEW_POLICY_CONFIRMED = "preview_policy.confirmed", "Preview policy confirmed"
    DERIVATIVE_UPLOAD_VERIFIED = "derivative_upload.verified", "Derivative upload verified"
    DERIVATIVE_FAILED = "derivative.failed", "Derivative failed"
    FACE_ANALYSIS_COMPLETED = "face_analysis.completed", "Face analysis completed"
    FACE_ANALYSIS_FAILED = "face_analysis.failed", "Face analysis failed"
    FACE_ANALYSIS_CONFLICT = "face_analysis.conflict", "Face analysis conflicted"
    FACE_ANALYSIS_RESET = "face_analysis.reset", "Face analysis reset"
    ASSET_GALLERY_EXCLUDED = "asset.gallery_excluded", "Asset excluded from gallery"
    ASSET_GALLERY_RESTORED = "asset.gallery_restored", "Asset restored to gallery"


class AuditResult(models.TextChoices):
    SUCCEEDED = "succeeded", "Succeeded"
    DENIED = "denied", "Denied"
    RATE_LIMITED = "rate_limited", "Rate limited"


class RateLimitPurpose(models.TextChoices):
    PHOTOGRAPHER_LOGIN = "photographer_login", "Photographer login"
    OWNER_PIN = "owner_pin", "Owner PIN"
    GUEST_PIN = "guest_pin", "Guest PIN"
    FACE_SEARCH_CLIENT = "face_search_client", "Face search by client"
    FACE_SEARCH_CAPABILITY = "face_search_capability", "Face search by capability"


class FaceAnalysisState(models.TextChoices):
    PENDING = "pending", "Pending"
    FAILED = "failed", "Failed"
    CONFLICT = "conflict", "Conflict"
    INDEXED = "indexed", "Indexed"
    NO_USABLE_FACE = "no_usable_face", "No usable face"


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
    share_access_version = models.UUIDField(default=uuid4, editable=False)
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
    derivatives_ready_generation = models.PositiveIntegerField(
        blank=True, null=True, editable=False
    )
    face_model_id = models.CharField(
        max_length=128,
        default=ACCEPTED_FACE_MODEL_CONTRACT.model.id,
        editable=False,
    )
    face_index_ready_generation = models.PositiveIntegerField(blank=True, null=True, editable=False)
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

    def is_publicly_available(self, *, at=None) -> bool:
        checked_at = at or timezone.now()
        return (
            self.photographer.status == PhotographerStatus.ACTIVE
            and self.state == EventState.PUBLISHED.value
            and self.expires_at is not None
            and self.expires_at > checked_at
        )


class SubEvent(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    event = models.ForeignKey(Event, on_delete=models.PROTECT, related_name="sub_events")
    name = models.CharField(max_length=120)
    position = models.PositiveSmallIntegerField(default=1, validators=[MinValueValidator(1)])
    is_archived = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("event", "name"),
                name="unique_event_sub_event_name",
            )
        ]
        ordering = ("position", "name", "id")

    def __str__(self) -> str:
        return f"{self.event}: {self.name}"


class ShareCapability(models.Model):
    """Security state shared by owner and guest URL capabilities."""

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    secret_digest = models.CharField(max_length=64, unique=True, validators=[SHA256_VALIDATOR])
    pin_hash = models.CharField(max_length=256, editable=False)
    access_version = models.UUIDField(default=uuid4, editable=False)
    expires_at = models.DateTimeField()
    revoked_at = models.DateTimeField(blank=True, null=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True

    def set_pin(self, raw_pin: str) -> None:
        if not PIN_PATTERN.fullmatch(raw_pin):
            raise ValidationError({"pin": "Enter exactly four ASCII digits."})
        self.pin_hash = make_password(self._peppered_pin(raw_pin), hasher="argon2")

    def check_pin(self, raw_pin: str) -> bool:
        return bool(self.pin_hash) and check_password(self._peppered_pin(raw_pin), self.pin_hash)

    @staticmethod
    def _peppered_pin(raw_pin: str) -> str:
        return f"{settings.SHARE_PIN_PEPPER}:{raw_pin}"

    def has_live_credentials(self, *, at=None) -> bool:
        checked_at = at or timezone.now()
        return self.revoked_at is None and self.expires_at > checked_at


class OwnerCapability(ShareCapability):
    event = models.OneToOneField(
        Event,
        on_delete=models.PROTECT,
        related_name="owner_capability",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="issued_openfotos_owner_capabilities",
    )

    def __str__(self) -> str:
        return f"Owner access for {self.event}"


class GuestCapability(ShareCapability):
    owner = models.ForeignKey(
        OwnerCapability,
        on_delete=models.PROTECT,
        related_name="guest_capabilities",
    )
    sub_event = models.ForeignKey(
        SubEvent,
        blank=True,
        null=True,
        on_delete=models.PROTECT,
        related_name="guest_capabilities",
    )
    label = models.CharField(max_length=80, blank=True)

    class Meta:
        ordering = ("-created_at", "id")

    def __str__(self) -> str:
        scope = self.sub_event.name if self.sub_event_id else "All Photos"
        return f"Guest access to {scope}"


class FaceSearchResultSet(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    owner_capability = models.ForeignKey(
        OwnerCapability,
        blank=True,
        null=True,
        on_delete=models.CASCADE,
        related_name="face_search_results",
    )
    guest_capability = models.ForeignKey(
        GuestCapability,
        blank=True,
        null=True,
        on_delete=models.CASCADE,
        related_name="face_search_results",
    )
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="face_search_results")
    sub_event = models.ForeignKey(
        SubEvent,
        blank=True,
        null=True,
        on_delete=models.CASCADE,
        related_name="face_search_results",
    )
    ordered_asset_ids = models.JSONField(default=list)
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(owner_capability__isnull=False, guest_capability__isnull=True)
                    | Q(owner_capability__isnull=True, guest_capability__isnull=False)
                ),
                name="face_search_has_one_capability",
            )
        ]
        indexes = [models.Index(fields=("expires_at",), name="face_search_expiry_idx")]


class EventInstallation(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    event = models.ForeignKey(Event, on_delete=models.PROTECT, related_name="installations")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        blank=True,
        null=True,
        on_delete=models.PROTECT,
        related_name="openfotos_event_installations",
    )
    installation_id = models.UUIDField()
    label = models.CharField(max_length=100)
    status = models.CharField(
        max_length=16,
        choices=tuple((status.value, status.value.title()) for status in InstallationStatus),
        default=InstallationStatus.ACTIVE.value,
    )
    revoked_at = models.DateTimeField(blank=True, null=True, editable=False)
    last_active_at = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("event", "installation_id"),
                name="unique_event_installation",
            ),
            models.CheckConstraint(
                condition=Q(user__isnull=False) | Q(status=InstallationStatus.REVOKED.value),
                name="active_event_installation_has_user",
            ),
        ]
        indexes = [models.Index(fields=("event", "status"), name="install_event_status_idx")]


class DesktopSession(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    photographer = models.ForeignKey(
        Photographer,
        on_delete=models.PROTECT,
        related_name="desktop_sessions",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="openfotos_desktop_sessions",
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


class ContributionBatch(models.Model):
    id = models.UUIDField(primary_key=True, editable=False)
    installation = models.ForeignKey(
        EventInstallation,
        on_delete=models.PROTECT,
        related_name="contribution_batches",
    )
    sub_event = models.ForeignKey(
        SubEvent,
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
                fields=("installation", "intake_generation", "state"),
                name="batch_install_gen_idx",
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


class FaceAnalysis(models.Model):
    asset = models.OneToOneField(
        Asset,
        primary_key=True,
        on_delete=models.PROTECT,
        related_name="face_analysis",
    )
    state = models.CharField(
        max_length=24,
        choices=FaceAnalysisState.choices,
        default=FaceAnalysisState.PENDING,
    )
    source_sha256 = models.CharField(max_length=64, blank=True, validators=[SHA256_VALIDATOR])
    model_id = models.CharField(max_length=128)
    artifact_sha256 = models.JSONField(default=list)
    document_sha256 = models.CharField(
        max_length=64,
        blank=True,
        validators=[SHA256_VALIDATOR],
    )
    detected_face_count = models.PositiveSmallIntegerField(default=0)
    usable_face_count = models.PositiveSmallIntegerField(default=0)
    attempt_count = models.PositiveSmallIntegerField(default=0)
    failure_code = models.CharField(max_length=64, blank=True)
    completed_at = models.DateTimeField(blank=True, null=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(detected_face_count__gte=F("usable_face_count")),
                name="face_detected_count_covers_usable",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        state=FaceAnalysisState.INDEXED,
                        usable_face_count__gt=0,
                        failure_code="",
                    )
                    | Q(
                        state=FaceAnalysisState.NO_USABLE_FACE,
                        usable_face_count=0,
                        failure_code="",
                    )
                    | Q(
                        state__in=(
                            FaceAnalysisState.PENDING,
                            FaceAnalysisState.FAILED,
                            FaceAnalysisState.CONFLICT,
                        ),
                        usable_face_count=0,
                    )
                ),
                name="face_analysis_state_matches_counts",
            ),
        ]


class FaceEmbedding(models.Model):
    analysis = models.ForeignKey(
        FaceAnalysis,
        on_delete=models.CASCADE,
        related_name="embeddings",
    )
    event = models.ForeignKey(
        Event,
        on_delete=models.PROTECT,
        related_name="face_embeddings",
    )
    face_ordinal = models.PositiveSmallIntegerField()
    detector_confidence = models.FloatField()
    bounding_box_width = models.PositiveIntegerField()
    bounding_box_height = models.PositiveIntegerField()
    model_id = models.CharField(max_length=128)
    vector = VectorField(dimensions=128)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("analysis", "face_ordinal"),
                name="unique_analysis_face_ordinal",
            ),
            models.CheckConstraint(
                condition=Q(detector_confidence__gte=0.0) & Q(detector_confidence__lte=1.0),
                name="face_confidence_between_zero_and_one",
            ),
            models.CheckConstraint(
                condition=Q(bounding_box_width__gt=0) & Q(bounding_box_height__gt=0),
                name="face_box_dimensions_positive",
            ),
        ]
        indexes = [
            models.Index(fields=("event", "model_id"), name="face_event_model_idx"),
            models.Index(fields=("analysis",), name="face_analysis_idx"),
        ]


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
    event_installation = models.ForeignKey(
        EventInstallation,
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
