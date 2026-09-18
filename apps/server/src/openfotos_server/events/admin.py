"""Manual pilot provisioning and audited lifecycle controls."""

from django import forms
from django.contrib import admin, messages
from django.core.exceptions import ValidationError

from openfotos_contracts import EventState, InstallationStatus

from .audit import record_audit
from .models import (
    Asset,
    AssetObject,
    AuditAction,
    AuditEvent,
    AuditResult,
    ConsentAttestation,
    ContributionBatch,
    Event,
    EventInstallation,
    GuestCapability,
    IngestionManifest,
    OwnerCapability,
    Photographer,
    PhotographerMembership,
    PortalCapability,
    PreviewPolicy,
    SubEvent,
)
from .services import transition_event
from .sharing_services import ShareAccessError, revoke_owner_capability


class EventAdminForm(forms.ModelForm):
    class Meta:
        model = Event
        fields = (
            "photographer",
            "name",
            "storage_limit_bytes",
            "max_contribution_devices",
            "processing_profile_id",
            "expires_at",
        )

    def clean(self):
        cleaned_data = super().clean()
        device_limit = cleaned_data.get("max_contribution_devices")
        if self.instance.pk and device_limit is not None:
            active_devices = self.instance.installations.filter(
                status=InstallationStatus.ACTIVE.value
            ).count()
            if device_limit < active_devices:
                self.add_error(
                    "max_contribution_devices",
                    f"Revoke devices before lowering the limit below {active_devices}.",
                )
        return cleaned_data


@admin.register(Photographer)
class PhotographerAdmin(admin.ModelAdmin):
    list_display = ("display_name", "slug", "contact_phone", "status", "created_at")
    list_filter = ("status",)
    search_fields = ("display_name", "slug")
    readonly_fields = ("id", "created_at", "updated_at")

    def get_readonly_fields(self, request, obj=None):
        fields = list(super().get_readonly_fields(request, obj))
        if obj is not None:
            fields.append("slug")
        return fields

    def has_delete_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        record_audit(
            photographer=obj,
            actor=request.user,
            action=(
                AuditAction.PHOTOGRAPHER_CHANGED if change else AuditAction.PHOTOGRAPHER_CREATED
            ),
            result=AuditResult.SUCCEEDED,
            request=request,
        )


@admin.register(PhotographerMembership)
class PhotographerMembershipAdmin(admin.ModelAdmin):
    list_display = ("user", "photographer", "role", "is_active", "updated_at")
    list_filter = ("role", "is_active", "photographer")
    search_fields = ("user__username", "photographer__display_name", "photographer__slug")
    autocomplete_fields = ("user", "photographer")
    readonly_fields = ("created_at", "updated_at")

    def get_readonly_fields(self, request, obj=None):
        fields = list(super().get_readonly_fields(request, obj))
        if obj is not None:
            fields.extend(("photographer", "user"))
        return fields

    def has_delete_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        record_audit(
            photographer=obj.photographer,
            actor=request.user,
            action=(AuditAction.MEMBERSHIP_CHANGED if change else AuditAction.MEMBERSHIP_CREATED),
            result=AuditResult.SUCCEEDED,
            request=request,
            metadata={"membership_id": obj.pk, "user_id": obj.user_id},
        )


@admin.register(Event)
class EventAdmin(admin.ModelAdmin):
    form = EventAdminForm
    list_display = ("name", "photographer", "state", "expires_at", "updated_at")
    list_filter = ("state", "photographer")
    search_fields = ("name", "photographer__display_name")
    autocomplete_fields = ("photographer",)
    readonly_fields = (
        "id",
        "state",
        "reserved_original_bytes",
        "verified_original_bytes",
        "intake_state",
        "intake_generation",
        "current_ingestion_manifest",
        "derivatives_ready_generation",
        "face_model_id",
        "face_index_ready_generation",
        "share_access_version",
        "cover_object_key",
        "cover_sha256",
        "cover_width",
        "cover_height",
        "first_published_at",
        "purge_after",
        "media_purged_at",
        "created_at",
        "updated_at",
    )
    actions = (
        "move_to_uploading",
        "move_to_processing",
        "move_to_review",
        "move_to_archived",
        "move_to_failed",
        "move_to_cancelled",
        "revoke_owner_access",
    )

    def get_readonly_fields(self, request, obj=None):
        fields = list(super().get_readonly_fields(request, obj))
        if obj is not None:
            fields.append("photographer")
        return fields

    def has_delete_permission(self, request, obj=None):
        return False

    def has_add_permission(self, request):
        return False

    def save_model(self, request, obj, form, change):
        if not change:
            super().save_model(request, obj, form, change)
            record_audit(
                photographer=obj.photographer,
                event=obj,
                actor=request.user,
                action=AuditAction.EVENT_CREATED,
                result=AuditResult.SUCCEEDED,
                request=request,
            )
            return

        super().save_model(request, obj, form, change)
        record_audit(
            photographer=obj.photographer,
            event=obj,
            actor=request.user,
            action=AuditAction.EVENT_CHANGED,
            result=AuditResult.SUCCEEDED,
            request=request,
        )

    @admin.action(description="Move selected events to Uploading")
    def move_to_uploading(self, request, queryset):
        self._transition(request, queryset, EventState.UPLOADING)

    @admin.action(description="Move selected events to Processing")
    def move_to_processing(self, request, queryset):
        self._transition(request, queryset, EventState.PROCESSING)

    @admin.action(description="Move selected events to Review")
    def move_to_review(self, request, queryset):
        self._transition(request, queryset, EventState.REVIEW)

    @admin.action(description="Move selected events to Archived")
    def move_to_archived(self, request, queryset):
        self._transition(request, queryset, EventState.ARCHIVED)

    @admin.action(description="Move selected events to Failed")
    def move_to_failed(self, request, queryset):
        self._transition(request, queryset, EventState.FAILED)

    @admin.action(description="Move selected events to Cancelled")
    def move_to_cancelled(self, request, queryset):
        self._transition(request, queryset, EventState.CANCELLED)

    @admin.action(description="Revoke owner access and all guest links")
    def revoke_owner_access(self, request, queryset):
        changed = 0
        for event in queryset:
            try:
                revoke_owner_capability(event=event, actor=request.user, request=request)
            except ShareAccessError:
                continue
            else:
                changed += 1
        self.message_user(request, f"Revoked owner access for {changed} event(s).")

    def _transition(self, request, queryset, target):
        changed = 0
        for event in queryset:
            try:
                transitioned = transition_event(
                    event_id=event.id,
                    target=target,
                    actor=request.user,
                    request=request,
                )
            except ValidationError as exc:
                self.message_user(
                    request,
                    f"{event}: {' '.join(exc.messages)}",
                    level=messages.ERROR,
                )
                continue
            if transitioned.state == target:
                changed += 1
        if changed:
            self.message_user(request, f"Moved {changed} event(s) to {target.value.title()}.")


@admin.register(ConsentAttestation)
class ConsentAttestationAdmin(admin.ModelAdmin):
    list_display = ("event", "notice_version", "actor", "created_at")
    search_fields = ("event__name", "event__photographer__display_name", "actor__username")
    readonly_fields = ("event", "notice_version", "actor", "created_at")

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(PortalCapability)
class PortalCapabilityAdmin(admin.ModelAdmin):
    list_display = ("event", "expires_at", "revoked_at", "created_at")
    search_fields = ("event__name", "event__photographer__display_name")
    readonly_fields = (
        "id",
        "event",
        "pin_hash",
        "access_version",
        "expires_at",
        "revoked_at",
        "created_at",
        "updated_at",
    )

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(AuditEvent)
class AuditEventAdmin(admin.ModelAdmin):
    list_display = (
        "created_at",
        "action",
        "result",
        "photographer",
        "event",
        "actor",
        "event_installation",
    )
    list_filter = ("action", "result", "photographer")
    search_fields = ("request_id", "event__name", "photographer__display_name")
    readonly_fields = (
        "photographer",
        "event",
        "actor",
        "event_installation",
        "action",
        "result",
        "client_hash",
        "request_id",
        "metadata",
        "created_at",
    )

    def has_add_permission(self, request):
        return False

    def has_view_permission(self, request, obj=None):
        return super().has_view_permission(request, obj)

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class _IngestionRecordAdmin(admin.ModelAdmin):
    """Expose ingestion history without creating an unaudited mutation path."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(OwnerCapability)
class OwnerCapabilityAdmin(_IngestionRecordAdmin):
    list_display = ("event", "expires_at", "revoked_at", "created_at")
    list_filter = ("revoked_at", "event__photographer")
    search_fields = ("id", "event__name")
    exclude = ("secret_digest", "pin_hash", "access_version")


@admin.register(GuestCapability)
class GuestCapabilityAdmin(_IngestionRecordAdmin):
    list_display = ("id", "owner", "sub_event", "label", "expires_at", "revoked_at")
    list_filter = ("revoked_at", "owner__event__photographer")
    search_fields = ("id", "label", "owner__event__name")
    exclude = ("secret_digest", "pin_hash", "access_version")


@admin.register(EventInstallation)
class EventInstallationAdmin(_IngestionRecordAdmin):
    list_display = ("label", "event", "user", "status", "last_active_at", "created_at")
    list_filter = ("status", "event__photographer")
    search_fields = ("label", "event__name")


@admin.register(SubEvent)
class SubEventAdmin(_IngestionRecordAdmin):
    list_display = ("name", "event", "position", "is_archived", "updated_at")
    list_filter = ("is_archived", "event__photographer")
    search_fields = ("name", "event__name")


@admin.register(ContributionBatch)
class ContributionBatchAdmin(_IngestionRecordAdmin):
    list_display = (
        "id",
        "installation",
        "sub_event",
        "intake_generation",
        "state",
        "declared_asset_count",
        "declared_original_bytes",
        "created_at",
    )
    list_filter = ("state", "installation__event__photographer")


@admin.register(Asset)
class AssetAdmin(_IngestionRecordAdmin):
    list_display = ("id", "original_filename", "batch", "width", "height", "created_at")
    list_filter = ("batch__installation__event__photographer",)
    search_fields = ("id", "original_filename")


@admin.register(AssetObject)
class AssetObjectAdmin(_IngestionRecordAdmin):
    list_display = ("asset", "variant", "state", "expected_bytes", "verified_at")
    list_filter = ("state", "variant", "asset__batch__installation__event__photographer")
    search_fields = ("asset__id", "object_key")


@admin.register(PreviewPolicy)
class PreviewPolicyAdmin(_IngestionRecordAdmin):
    list_display = (
        "event",
        "enabled",
        "template",
        "logo_kind",
        "derivative_profile_id",
        "confirmed_at",
    )
    list_filter = ("enabled", "template", "logo_kind", "event__photographer")
    search_fields = ("event__name", "event__photographer__display_name")


@admin.register(IngestionManifest)
class IngestionManifestAdmin(_IngestionRecordAdmin):
    list_display = (
        "event",
        "generation",
        "state",
        "asset_count",
        "original_bytes",
        "committed_at",
    )
    list_filter = ("state", "event__photographer")
