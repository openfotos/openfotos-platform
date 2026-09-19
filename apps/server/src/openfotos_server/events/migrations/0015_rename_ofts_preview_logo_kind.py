from django.db import migrations


def rename_ofts_logo_kind(apps, schema_editor):
    """Align preview policies created before the OneNodeAI wordmark rename."""
    PreviewPolicy = apps.get_model("events", "PreviewPolicy")
    PreviewPolicy.objects.filter(logo_kind="ofts").update(logo_kind="onenodeai")


class Migration(migrations.Migration):
    dependencies = [("events", "0014_studio_branding_and_logout")]

    operations = [
        # Deliberately irreversible: renaming every onenodeai row back would corrupt
        # policies created after the rename.
        migrations.RunPython(rename_ofts_logo_kind, migrations.RunPython.noop),
    ]
