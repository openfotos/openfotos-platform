from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from openfotos_vision import ACCEPTED_FACE_MODEL_CONTRACT, FaceEngineError, artifact_identity


class Command(BaseCommand):
    help = "Verify immutable artifacts required by every deployed server process."

    def handle(self, *args, **options):
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
        self.stdout.write(self.style.SUCCESS("Deployment artifacts verified."))
