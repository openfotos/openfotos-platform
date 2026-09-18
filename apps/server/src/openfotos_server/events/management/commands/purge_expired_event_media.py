from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.core.management.base import BaseCommand, CommandError

from openfotos_server.events.object_store import configured_object_store
from openfotos_server.events.retention_services import RetentionError, purge_event_media


class Command(BaseCommand):
    help = "Purge one event whose 30-day post-retention grace period has elapsed."

    def add_arguments(self, parser):
        parser.add_argument("--event-id", required=True)
        parser.add_argument(
            "--confirm",
            action="store_true",
            help="Confirm irreversible deletion of private media, face data, and capabilities.",
        )

    def handle(self, *args, **options):
        if not options["confirm"]:
            raise CommandError(
                "Pass --confirm after verifying the exact event ID and backup state."
            )
        try:
            result = purge_event_media(
                event_id=options["event_id"],
                object_store=configured_object_store(),
            )
        except (ImproperlyConfigured, RetentionError, ValidationError, ValueError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(
            self.style.SUCCESS(
                f"Purged event {result.event_id}: {result.deleted_object_count} objects deleted, "
                f"{result.redacted_asset_count} asset records redacted."
            )
        )
