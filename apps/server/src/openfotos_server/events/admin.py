"""Manual pilot provisioning and audited lifecycle controls."""

from django import forms
from django.conf import settings
from django.contrib import admin, messages
from django.core.exceptions import ValidationError
from django.utils.html import format_html

from openfotos_contracts import EventState

from .audit import record_audit
from .models import (
    AuditAction,
    AuditEvent,
    AuditResult,
    Event,
    Photographer,
    PhotographerMembership,
)
from .services import (
    change_event_pin,
    revoke_event_sessions,
    rotate_event_token,
    transition_event,
)


class EventAdminForm(forms.ModelForm):
    pin = forms.RegexField(
        regex=r"^[0-9]{6}$",
        required=False,
        help_text="Required when creating an event. Enter a new value only to rotate the PIN.",
        widget=forms.PasswordInput(render_value=False),
    )

    class Meta:
        model = Event
        fields = (
            "photographer",
            "name",
            "storage_limit_bytes",
            "expires_at",
            "pin",
        )

    def clean(self):
        cleaned_data = super().clean()
        if self.instance._state.adding and not cleaned_data.get("pin"):
            self.add_error("pin", "Set a six-digit PIN when creating an event.")
        return cleaned_data


@admin.register(Photographer)
class PhotographerAdmin(admin.ModelAdmin):
    list_display = ("display_name", "slug", "status", "created_at")
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
    search_fields = ("name", "photographer__display_name", "public_token")
    autocomplete_fields = ("photographer",)
    readonly_fields = (
        "id",
        "public_token",
        "event_url",
        "state",
        "visitor_access_version",
        "created_at",
        "updated_at",
    )
    actions = (
        "move_to_uploading",
        "move_to_processing",
        "move_to_review",
        "move_to_published",
        "move_to_archived",
        "move_to_failed",
        "move_to_cancelled",
        "revoke_visitor_sessions",
        "rotate_public_token",
    )

    def get_readonly_fields(self, request, obj=None):
        fields = list(super().get_readonly_fields(request, obj))
        if obj is not None:
            fields.append("photographer")
        return fields

    @admin.display(description="Event URL")
    def event_url(self, obj):
        if not obj or not obj.public_token:
            return "Available after saving"
        scheme = "http" if settings.DEBUG else "https"
        url = (
            f"{scheme}://{obj.photographer.slug}.{settings.PUBLIC_BASE_DOMAIN}"
            f"/e/{obj.public_token}/"
        )
        return format_html('<a href="{}">{}</a>', url, url)

    def has_delete_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        raw_pin = form.cleaned_data.get("pin")
        if not change:
            obj.set_pin(raw_pin)
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
        if raw_pin:
            change_event_pin(
                event_id=obj.id,
                raw_pin=raw_pin,
                actor=request.user,
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

    @admin.action(description="Move selected events to Published")
    def move_to_published(self, request, queryset):
        self._transition(request, queryset, EventState.PUBLISHED)

    @admin.action(description="Move selected events to Archived")
    def move_to_archived(self, request, queryset):
        self._transition(request, queryset, EventState.ARCHIVED)

    @admin.action(description="Move selected events to Failed")
    def move_to_failed(self, request, queryset):
        self._transition(request, queryset, EventState.FAILED)

    @admin.action(description="Move selected events to Cancelled")
    def move_to_cancelled(self, request, queryset):
        self._transition(request, queryset, EventState.CANCELLED)

    @admin.action(description="Revoke all current visitor sessions")
    def revoke_visitor_sessions(self, request, queryset):
        changed = 0
        for event in queryset:
            revoke_event_sessions(event_id=event.id, actor=request.user, request=request)
            changed += 1
        self.message_user(request, f"Revoked visitor sessions for {changed} event(s).")

    @admin.action(description="Rotate public token and break existing links")
    def rotate_public_token(self, request, queryset):
        changed = 0
        for event in queryset:
            rotate_event_token(event_id=event.id, actor=request.user, request=request)
            changed += 1
        self.message_user(request, f"Rotated the public token for {changed} event(s).")

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


@admin.register(AuditEvent)
class AuditEventAdmin(admin.ModelAdmin):
    list_display = ("created_at", "action", "result", "photographer", "event", "actor")
    list_filter = ("action", "result", "photographer")
    search_fields = ("request_id", "event__name", "photographer__display_name")
    readonly_fields = (
        "photographer",
        "event",
        "actor",
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
