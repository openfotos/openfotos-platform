from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.core.management.base import BaseCommand, CommandError

from openfotos_server.events.models import Event
from openfotos_server.events.object_store import configured_object_store
from openfotos_server.events.retention_services import (
    RetentionError,
    erase_event_for_privacy,
)


class Command(BaseCommand):
    help = "Quarantine and irreversibly erase one exact event under a written studio instruction."

    def add_arguments(self, parser):
        parser.add_argument("--event-id", required=True)
        parser.add_argument("--confirm-event-name", required=True)
        parser.add_argument("--instruction-reference", required=True)
        parser.add_argument(
            "--confirm",
            action="store_true",
            help="Confirm irreversible deletion of the event and all cloud media.",
        )

    def handle(self, *args, **options):
        if not options["confirm"]:
            raise CommandError("Pass --confirm after verifying the written studio instruction.")
        event = (
            Event.objects.filter(pk=options["event_id"]).only("name", "privacy_erased_at").first()
        )
        if event is None:
            raise CommandError("The event does not exist.")
        if event.privacy_erased_at is None and event.name != options["confirm_event_name"]:
            raise CommandError("The confirmation name does not match the selected event.")
        try:
            result = erase_event_for_privacy(
                event_id=event.id,
                instruction_reference=options["instruction_reference"],
                object_store=configured_object_store(),
            )
        except (ImproperlyConfigured, RetentionError, ValidationError, ValueError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(
            self.style.SUCCESS(
                f"Erased event {result.event_id}: {result.deleted_object_count} objects deleted, "
                f"{result.redacted_asset_count} asset records redacted."
            )
        )
