import getpass

from django.contrib.auth import get_user_model, password_validation
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from openfotos_server.events.account_services import revoke_user_sessions
from openfotos_server.events.models import PhotographerMembership


class Command(BaseCommand):
    help = "Reset one photographer password and revoke all of that user's active sessions."

    def add_arguments(self, parser):
        parser.add_argument("--username", required=True)
        parser.add_argument(
            "--confirm",
            action="store_true",
            help="Confirm that the photographer's identity was verified through support.",
        )

    def handle(self, *args, **options):
        if not options["confirm"]:
            raise CommandError(
                "Pass --confirm only after completing support identity verification."
            )
        user_model = get_user_model()
        try:
            user = user_model.objects.get(username=options["username"])
        except user_model.DoesNotExist as exc:
            raise CommandError("The photographer account does not exist.") from exc
        if user.is_staff or user.is_superuser or not user.is_active:
            raise CommandError("Only an active non-staff photographer account can be reset.")
        if not PhotographerMembership.objects.filter(user=user, is_active=True).exists():
            raise CommandError("The account has no active photographer membership.")

        first = getpass.getpass("New password: ")
        second = getpass.getpass("Confirm new password: ")
        if first != second:
            raise CommandError("The password confirmation did not match.")
        try:
            password_validation.validate_password(first, user=user)
        except ValidationError as exc:
            raise CommandError(" ".join(exc.messages)) from exc

        with transaction.atomic():
            locked = user_model.objects.select_for_update().get(pk=user.pk)
            locked.set_password(first)
            locked.save(update_fields=("password",))
            result = revoke_user_sessions(user=locked)
        self.stdout.write(
            self.style.SUCCESS(
                f"Reset {locked.username}; revoked {result.desktop_sessions} desktop and "
                f"{result.browser_sessions} browser session(s)."
            )
        )
