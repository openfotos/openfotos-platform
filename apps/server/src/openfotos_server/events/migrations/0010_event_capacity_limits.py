from django.db import migrations, models
from django.db.models import F, Q

OLD_DEFAULT_BYTES = 25_000_000_000
EVENT_ORIGINAL_BYTES_LIMIT = 50_000_000_000
EVENT_ORIGINAL_ASSET_LIMIT = 10_000


def migrate_event_capacity(apps, _schema_editor):
    Event = apps.get_model("events", "Event")
    AssetObject = apps.get_model("events", "AssetObject")

    Event.objects.filter(storage_limit_bytes=OLD_DEFAULT_BYTES).update(
        storage_limit_bytes=EVENT_ORIGINAL_BYTES_LIMIT
    )
    for event_id in Event.objects.values_list("id", flat=True).iterator():
        originals = AssetObject.objects.filter(
            asset__batch__installation__event_id=event_id,
            variant="original",
        )
        Event.objects.filter(pk=event_id).update(
            reserved_original_count=originals.exclude(state="excluded").count(),
            verified_original_count=originals.filter(state="verified").count(),
        )


class Migration(migrations.Migration):
    dependencies = [("events", "0009_alter_audit_and_rate_limit_choices")]

    operations = [
        migrations.AddField(
            model_name="event",
            name="reserved_original_count",
            field=models.PositiveIntegerField(default=0, editable=False),
        ),
        migrations.AddField(
            model_name="event",
            name="verified_original_count",
            field=models.PositiveIntegerField(default=0, editable=False),
        ),
        migrations.AlterField(
            model_name="event",
            name="storage_limit_bytes",
            field=models.PositiveBigIntegerField(
                default=EVENT_ORIGINAL_BYTES_LIMIT, editable=False
            ),
        ),
        migrations.RunPython(migrate_event_capacity, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="event",
            constraint=models.CheckConstraint(
                condition=Q(verified_original_count__lte=F("reserved_original_count")),
                name="event_verified_count_within_reserved",
            ),
        ),
        migrations.AddConstraint(
            model_name="event",
            constraint=models.CheckConstraint(
                condition=Q(reserved_original_count__lte=EVENT_ORIGINAL_ASSET_LIMIT),
                name="event_reserved_count_within_limit",
            ),
        ),
    ]
