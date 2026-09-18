from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("events", "0010_event_capacity_limits")]

    operations = [
        migrations.AddField(
            model_name="event",
            name="erasure_instruction_reference",
            field=models.CharField(blank=True, editable=False, max_length=100),
        ),
        migrations.AddField(
            model_name="event",
            name="erasure_requested_at",
            field=models.DateTimeField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name="event",
            name="privacy_erased_at",
            field=models.DateTimeField(blank=True, editable=False, null=True),
        ),
    ]
