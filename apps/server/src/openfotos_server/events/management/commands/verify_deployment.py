from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from openfotos_vision import ACCEPTED_FACE_MODEL_CONTRACT, FaceEngineError, artifact_identity


class Command(BaseCommand):
    help = "Verify immutable artifacts required by every deployed server process."

    def handle(self, *args, **options):
        _verify_face_model_artifacts()
        self.stdout.write(self.style.SUCCESS("Deployment artifacts verified."))
        if settings.IS_DEPLOYED:
            _verify_database_migrations()
            self.stdout.write(self.style.SUCCESS("Deployment database migrations verified."))


def _verify_face_model_artifacts() -> None:
    paths = (
        Path(settings.FACE_DETECTOR_MODEL_PATH),
        Path(settings.FACE_RECOGNIZER_MODEL_PATH),
    )
    artifacts = ACCEPTED_FACE_MODEL_CONTRACT.model.artifacts
    try:
        verified = tuple(
            artifact_identity(
                path,
                expected_sha256=artifact.sha256,
                license_id=artifact.license_id,
            )
            for path, artifact in zip(paths, artifacts, strict=True)
        )
    except (FaceEngineError, OSError, ValueError) as exc:
        raise CommandError("The deployed face-model artifacts failed verification.") from exc
    if verified != artifacts:
        raise CommandError("The deployed face-model contract is inconsistent.")


def _verify_database_migrations() -> None:
    try:
        executor = MigrationExecutor(connection)
        pending = executor.migration_plan(executor.loader.graph.leaf_nodes())
    except Exception as exc:
        raise CommandError("The deployed database migration state could not be verified.") from exc
    if pending:
        raise CommandError(f"The deployed database has {len(pending)} unapplied migration(s).")
