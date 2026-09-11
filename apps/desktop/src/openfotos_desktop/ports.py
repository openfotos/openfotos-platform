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
        self, server_url: str, username: str, password: str, device_label: str
    ) -> Sequence[EventCache]: ...

    def enroll_uploader(
        self, server_url: str, invitation: str, device_label: str
    ) -> EventCache: ...

    def resume(self, server_url: str) -> Sequence[EventCache]: ...

    def create_invitation(self, event_id: UUID) -> str: ...

    def close_intake(self, event_id: UUID) -> EventCache: ...

    def reopen_intake(self, event_id: UUID) -> EventCache: ...

    def finalize(self, event_id: UUID) -> dict: ...


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
        is_cancelled: Callable[[], bool] | None = None,
    ) -> None: ...


class DesktopGateway(PhotographerSessionGateway, BatchUploadService, Protocol):
    pass


class Session3Gateway:
    """Make the incomplete network boundary explicit in normal application mode."""

    _MESSAGE = (
        "Online authentication and uploader enrollment are delivered in Session 4. "
        "Launch with --demo to exercise Session 3 local inventory."
    )

    def sign_in_lead(
        self,
        server_url: str,
        username: str,
        password: str,
        device_label: str,
    ) -> Sequence[EventCache]:
        del server_url, username, password, device_label
        raise OnlineServicesUnavailable(self._MESSAGE)

    def enroll_uploader(self, server_url: str, invitation: str, device_label: str) -> EventCache:
        del server_url, invitation, device_label
        raise OnlineServicesUnavailable(self._MESSAGE)

    def resume(self, server_url: str) -> Sequence[EventCache]:
        del server_url
        raise OnlineServicesUnavailable(self._MESSAGE)

    def create_invitation(self, event_id: UUID) -> str:
        del event_id
        raise OnlineServicesUnavailable(self._MESSAGE)

    def close_intake(self, event_id: UUID) -> EventCache:
        del event_id
        raise OnlineServicesUnavailable(self._MESSAGE)

    def reopen_intake(self, event_id: UUID) -> EventCache:
        del event_id
        raise OnlineServicesUnavailable(self._MESSAGE)

    def finalize(self, event_id: UUID) -> dict:
        del event_id
        raise OnlineServicesUnavailable(self._MESSAGE)

    def upload(
        self,
        batch_id: UUID,
        *,
        transfer_limit: int,
        on_progress: Callable[[int, int], None],
        is_cancelled: Callable[[], bool] | None = None,
    ) -> None:
        del batch_id, transfer_limit, on_progress, is_cancelled
        raise OnlineServicesUnavailable(self._MESSAGE)


class DiagnosticExporter(Protocol):
    def export(self, batch_id: UUID, destination: Path) -> None: ...
