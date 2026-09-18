"""Grant the deployed runtime role only the DML privileges Django requires."""

import re

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from psycopg import sql


class Command(BaseCommand):
    help = "Grant least-privilege access on migrated OpenFotos objects to the runtime role."

    def handle(self, *args, **options) -> None:
        if connection.vendor != "postgresql":
            raise CommandError("Runtime database grants require PostgreSQL.")
        role = settings.OPENFOTOS_RUNTIME_DB_ROLE
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", role):
            raise CommandError("OPENFOTOS_RUNTIME_DB_ROLE is invalid.")

        with connection.cursor() as cursor:
            cursor.execute("SELECT current_user")
            migration_role = cursor.fetchone()[0]
            if migration_role == role:
                raise CommandError("The migration and runtime database roles must be different.")
            cursor.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", [role])
            if cursor.fetchone() is None:
                raise CommandError(f"Database role {role!r} does not exist.")
            runtime = sql.Identifier(role)
            schema = sql.Identifier("openfotos")
            statements = (
                sql.SQL("REVOKE CREATE ON SCHEMA {} FROM {}").format(schema, runtime),
                sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(schema, runtime),
                sql.SQL(
                    "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA {} TO {}"
                ).format(schema, runtime),
                sql.SQL("GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA {} TO {}").format(
                    schema, runtime
                ),
                sql.SQL(
                    "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA {} "
                    "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {}"
                ).format(sql.Identifier(migration_role), schema, runtime),
                sql.SQL(
                    "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA {} "
                    "GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO {}"
                ).format(sql.Identifier(migration_role), schema, runtime),
            )
            for statement in statements:
                cursor.execute(statement)
        self.stdout.write(self.style.SUCCESS(f"Runtime grants applied to {role}."))
