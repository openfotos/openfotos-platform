"""Session boundaries implemented by the network and pipeline work in later sessions."""

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Protocol
from uuid import UUID

from .ingestion import EventCache


class OnlineServicesUnavailable(RuntimeError):
    pass


class PhotographerSessionGateway(Protocol):
    def sign_in_lead(
        self, server_url: str, username: str, password: str
    ) -> Sequence[EventCache]: ...

    def enroll_uploader(
        self, server_url: str, invitation: str, device_label: str
    ) -> EventCache: ...


class BatchProcessingService(Protocol):
    def process(
        self,
        batch_id: UUID,
        *,
        on_progress: Callable[[int, int], None],
    ) -> None: ...


class BatchUploadService(Protocol):
    def upload(
        self,
        batch_id: UUID,
        *,
        transfer_limit: int,
        on_progress: Callable[[int, int], None],
    ) -> None: ...


class Session3Gateway:
    """Make the incomplete network boundary explicit in normal application mode."""

    _MESSAGE = (
        "Online authentication and uploader enrollment are delivered in Session 4. "
        "Launch with --demo to exercise Session 3 local inventory."
    )

    def sign_in_lead(self, server_url: str, username: str, password: str) -> Sequence[EventCache]:
        del server_url, username, password
        raise OnlineServicesUnavailable(self._MESSAGE)

    def enroll_uploader(self, server_url: str, invitation: str, device_label: str) -> EventCache:
        del server_url, invitation, device_label
        raise OnlineServicesUnavailable(self._MESSAGE)


class DiagnosticExporter(Protocol):
    def export(self, batch_id: UUID, destination: Path) -> None: ...
