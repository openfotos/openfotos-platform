from django.db import migrations
from django.utils import timezone


def complete_terminal_original_batches(apps, schema_editor):
    ContributionBatch = apps.get_model("events", "ContributionBatch")
    AssetObject = apps.get_model("events", "AssetObject")
    terminal_states = ("verified", "excluded")

    for batch in ContributionBatch.objects.filter(state="reserved").iterator():
        originals = AssetObject.objects.filter(
            asset__batch_id=batch.pk,
            variant="originals",
        )
        if not originals.exists() or originals.exclude(state__in=terminal_states).exists():
            continue
        completed_at = timezone.now()
        ContributionBatch.objects.filter(pk=batch.pk, state="reserved").update(
            state="complete",
            completed_at=completed_at,
            updated_at=completed_at,
        )


class Migration(migrations.Migration):
    dependencies = [("events", "0015_rename_ofts_preview_logo_kind")]

    operations = [
        migrations.RunPython(complete_terminal_original_batches, migrations.RunPython.noop),
    ]
