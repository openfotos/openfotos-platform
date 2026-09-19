from uuid import uuid4

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from ...audit import record_audit
from ...models import (
    AuditAction,
    AuditResult,
    Event,
    FaceSearchResultSet,
    PortalCapability,
)

CONFIRMATION = "RESET-ALL-SHARE-ACCESS"


class Command(BaseCommand):
    help = "Emergency-only revocation before replacing a suspected-compromised share PIN pepper."

    def add_arguments(self, parser):
        parser.add_argument("--confirm", required=True)

    @transaction.atomic
    def handle(self, *args, **options):
        del args
        if options["confirm"] != CONFIRMATION:
            raise CommandError(f"Pass --confirm {CONFIRMATION} to revoke every share link.")
        now = timezone.now()
        events = list(
            Event.objects.select_for_update()
            .filter(portal_capability__isnull=False)
            .select_related("photographer")
        )
        PortalCapability.objects.filter(event__in=events).update(
            revoked_at=now,
            access_version=uuid4(),
        )
        FaceSearchResultSet.objects.all().delete()
        for event in events:
            event.share_access_version = uuid4()
            event.save(update_fields=("share_access_version", "updated_at"))
            record_audit(
                photographer=event.photographer,
                event=event,
                action=AuditAction.SHARE_ACCESS_RESET,
                result=AuditResult.SUCCEEDED,
                metadata={"reason": "share_pin_pepper_emergency_reset"},
            )
        self.stdout.write(f"Revoked event PIN access for {len(events)} event(s).")
