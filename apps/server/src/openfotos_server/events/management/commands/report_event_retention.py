"""Report due and upcoming event purges without deleting data."""

import json
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from openfotos_server.events.models import Event


class Command(BaseCommand):
    help = "Emit a privacy-safe JSON report of due and upcoming manual event purges."

    def add_arguments(self, parser):
        parser.add_argument("--days-ahead", type=int, default=7)

    def handle(self, *args, **options):
        del args
        days_ahead = options["days_ahead"]
        if not 0 <= days_ahead <= 30:
            raise CommandError("--days-ahead must be between 0 and 30.")
        now = timezone.now()
        rows = (
            Event.objects.filter(
                purge_after__isnull=False,
                purge_after__lte=now + timedelta(days=days_ahead),
                media_purged_at__isnull=True,
                privacy_erased_at__isnull=True,
            )
            .select_related("photographer")
            .order_by("purge_after", "id")
        )
        due = []
        upcoming = []
        for event in rows:
            item = {
                "event_id": str(event.id),
                "photographer_slug": event.photographer.slug,
                "purge_after": event.purge_after.isoformat(),
            }
            (due if event.purge_after <= now else upcoming).append(item)
        self.stdout.write(
            json.dumps(
                {
                    "generated_at": now.isoformat(),
                    "due": due,
                    "upcoming": upcoming,
                },
                separators=(",", ":"),
                sort_keys=True,
            )
        )
