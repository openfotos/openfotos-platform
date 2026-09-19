import django.core.validators
from django.db import migrations, models
from django.utils.text import slugify

EVENT_SLUG_MAX_LENGTH = 200


def backfill_published_event_slugs(apps, schema_editor):
    Event = apps.get_model("events", "Event")
    photographer_ids = (
        Event.objects.filter(state="published").values_list("photographer_id", flat=True).distinct()
    )
    for photographer_id in photographer_ids:
        used = set(
            Event.objects.filter(photographer_id=photographer_id)
            .exclude(slug__isnull=True)
            .values_list("slug", flat=True)
        )
        events = Event.objects.filter(
            photographer_id=photographer_id,
            state="published",
            slug__isnull=True,
        ).order_by("created_at", "id")
        for event in events.iterator():
            normalized = slugify(event.name).replace("_", "-")
            base = "-".join(part for part in normalized.split("-") if part)
            base = base[:EVENT_SLUG_MAX_LENGTH].rstrip("-") or "event"
            candidate = base
            suffix_number = 2
            while candidate in used:
                suffix = f"-{suffix_number}"
                candidate = f"{base[: EVENT_SLUG_MAX_LENGTH - len(suffix)].rstrip('-')}{suffix}"
                suffix_number += 1
            Event.objects.filter(pk=event.pk).update(slug=candidate)
            used.add(candidate)


class Migration(migrations.Migration):
    dependencies = [("events", "0011_event_erasure_tracking")]

    operations = [
        migrations.AddField(
            model_name="event",
            name="slug",
            field=models.SlugField(
                blank=True,
                editable=False,
                max_length=200,
                null=True,
                validators=[
                    django.core.validators.RegexValidator(
                        message="Use lowercase letters, digits, and single hyphens.",
                        regex="^[a-z0-9]+(?:-[a-z0-9]+)*$",
                    )
                ],
            ),
        ),
        migrations.RunPython(backfill_published_event_slugs, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="event",
            constraint=models.UniqueConstraint(
                fields=("photographer", "slug"),
                name="unique_photographer_event_slug",
            ),
        ),
    ]
