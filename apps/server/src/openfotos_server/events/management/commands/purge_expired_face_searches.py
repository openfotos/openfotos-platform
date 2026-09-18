from django.core.management.base import BaseCommand
from django.utils import timezone

from ...models import FaceSearchResultSet


class Command(BaseCommand):
    help = "Delete expired, non-authorizing face-search result sets."

    def handle(self, *args, **options):
        del args, options
        deleted, _ = FaceSearchResultSet.objects.filter(expires_at__lte=timezone.now()).delete()
        self.stdout.write(f"Deleted {deleted} expired face-search record(s).")
