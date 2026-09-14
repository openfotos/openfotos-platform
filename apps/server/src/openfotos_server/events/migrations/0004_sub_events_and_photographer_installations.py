import uuid

import django.core.validators
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.utils import timezone


def migrate_existing_installations_and_batches(apps, schema_editor):
    DesktopSession = apps.get_model("events", "DesktopSession")
    EventInstallation = apps.get_model("events", "EventInstallation")
    Event = apps.get_model("events", "Event")
    SubEvent = apps.get_model("events", "SubEvent")
    ContributionBatch = apps.get_model("events", "ContributionBatch")

    DesktopSession.objects.filter(user__isnull=True).delete()
    for installation in EventInstallation.objects.filter(user__isnull=True, role="lead"):
        session = (
            DesktopSession.objects.filter(
                photographer_id=installation.event.photographer_id,
                installation_id=installation.installation_id,
                user__isnull=False,
            )
            .order_by("created_at")
            .first()
        )
        if session is not None:
            installation.user_id = session.user_id
            installation.save(update_fields=("user",))
    EventInstallation.objects.filter(user__isnull=True).update(
        status="revoked", revoked_at=timezone.now()
    )

    event_ids = ContributionBatch.objects.values_list(
        "installation__event_id", flat=True
    ).distinct()
    for event in Event.objects.filter(id__in=event_ids):
        sub_event = SubEvent.objects.create(
            event=event,
            name="Imported photos",
            position=1,
        )
        ContributionBatch.objects.filter(installation__event=event).update(sub_event=sub_event)


class Migration(migrations.Migration):
    dependencies = [
        ("events", "0003_gallery_derivatives"),
    ]

    operations = [
        migrations.RenameModel(
            old_name="UploaderDevice",
            new_name="EventInstallation",
        ),
        migrations.RemoveIndex(
            model_name="contributionbatch",
            name="batch_device_gen_idx",
        ),
        migrations.RenameField(
            model_name="contributionbatch",
            old_name="device",
            new_name="installation",
        ),
        migrations.RenameField(
            model_name="auditevent",
            old_name="uploader_device",
            new_name="event_installation",
        ),
        migrations.AddField(
            model_name="eventinstallation",
            name="user",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="openfotos_event_installations",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.CreateModel(
            name="SubEvent",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("name", models.CharField(max_length=120)),
                (
                    "position",
                    models.PositiveSmallIntegerField(
                        default=1,
                        validators=[django.core.validators.MinValueValidator(1)],
                    ),
                ),
                ("is_archived", models.BooleanField(default=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "event",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="sub_events",
                        to="events.event",
                    ),
                ),
            ],
            options={
                "ordering": ("position", "name", "id"),
            },
        ),
        migrations.AddConstraint(
            model_name="subevent",
            constraint=models.UniqueConstraint(
                fields=("event", "name"),
                name="unique_event_sub_event_name",
            ),
        ),
        migrations.AddField(
            model_name="contributionbatch",
            name="sub_event",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="contribution_batches",
                to="events.subevent",
            ),
        ),
        migrations.RunPython(
            migrate_existing_installations_and_batches,
            migrations.RunPython.noop,
        ),
        migrations.AlterField(
            model_name="contributionbatch",
            name="sub_event",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="contribution_batches",
                to="events.subevent",
            ),
        ),
        migrations.RemoveConstraint(
            model_name="desktopsession",
            name="desktop_session_has_one_actor",
        ),
        migrations.RemoveField(
            model_name="desktopsession",
            name="device",
        ),
        migrations.AlterField(
            model_name="desktopsession",
            name="user",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="openfotos_desktop_sessions",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.RemoveField(
            model_name="eventinstallation",
            name="invitation",
        ),
        migrations.RemoveField(
            model_name="eventinstallation",
            name="role",
        ),
        migrations.DeleteModel(
            name="UploaderInvitation",
        ),
        migrations.RemoveField(
            model_name="event",
            name="original_download_policy",
        ),
        migrations.AlterField(
            model_name="eventinstallation",
            name="event",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="installations",
                to="events.event",
            ),
        ),
        migrations.AlterField(
            model_name="eventinstallation",
            name="status",
            field=models.CharField(
                choices=[("active", "Active"), ("revoked", "Revoked")],
                default="active",
                max_length=16,
            ),
        ),
        migrations.AddConstraint(
            model_name="eventinstallation",
            constraint=models.CheckConstraint(
                condition=models.Q(("user__isnull", False), ("status", "revoked"), _connector="OR"),
                name="active_event_installation_has_user",
            ),
        ),
        migrations.RenameIndex(
            model_name="eventinstallation",
            new_name="install_event_status_idx",
            old_name="device_event_status_idx",
        ),
        migrations.AddIndex(
            model_name="contributionbatch",
            index=models.Index(
                fields=["installation", "intake_generation", "state"],
                name="batch_install_gen_idx",
            ),
        ),
        migrations.AlterField(
            model_name="auditevent",
            name="action",
            field=models.CharField(
                choices=[
                    ("photographer.created", "Photographer created"),
                    ("photographer.changed", "Photographer changed"),
                    ("membership.created", "Membership created"),
                    ("membership.changed", "Membership changed"),
                    ("event.created", "Event created"),
                    ("event.changed", "Event changed"),
                    ("event.state_changed", "Event state changed"),
                    ("event.pin_changed", "Event PIN changed"),
                    ("event.token_changed", "Event token changed"),
                    ("event.access_revoked", "Event access revoked"),
                    ("photographer.login", "Photographer login"),
                    ("photographer.logout", "Photographer logout"),
                    ("event.pin_unlock", "Event PIN unlock"),
                    ("desktop.login", "Desktop login"),
                    ("desktop.token_refresh", "Desktop token refresh"),
                    ("sub_event.created", "Sub-event created"),
                    ("sub_event.changed", "Sub-event changed"),
                    ("sub_event.archived", "Sub-event archived"),
                    ("sub_event.restored", "Sub-event restored"),
                    ("event_installation.registered", "Event installation registered"),
                    ("event_installation.revoked", "Event installation revoked"),
                    ("contribution.reserved", "Contribution reserved"),
                    ("contribution.reassigned", "Contribution reassigned"),
                    ("contribution.cancelled", "Contribution cancelled"),
                    ("asset_upload.verified", "Asset upload verified"),
                    ("asset.excluded", "Asset excluded"),
                    ("event.intake_closed", "Event intake closed"),
                    ("event.intake_reopened", "Event intake reopened"),
                    ("event.ingestion_finalized", "Event ingestion finalized"),
                    ("preview_policy.confirmed", "Preview policy confirmed"),
                    ("derivative_upload.verified", "Derivative upload verified"),
                    ("derivative.failed", "Derivative failed"),
                    ("asset.gallery_excluded", "Asset excluded from gallery"),
                    ("asset.gallery_restored", "Asset restored to gallery"),
                ],
                max_length=64,
            ),
        ),
    ]
