"""Session boundaries implemented by the network and pipeline work in later sessions."""

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Protocol
from uuid import UUID

from openfotos_contracts import WatermarkLogoKind, WatermarkTemplate

from .ingestion import EventCache


class OnlineServicesUnavailable(RuntimeError):
    pass


class PhotographerSessionGateway(Protocol):
    def sign_in_photographer(
        self, server_url: str, username: str, password: str, device_label: str
    ) -> Sequence[EventCache]: ...

    def resume(self, server_url: str) -> Sequence[EventCache]: ...

    def close_intake(self, event_id: UUID) -> EventCache: ...

    def reopen_intake(self, event_id: UUID) -> EventCache: ...

    def finalize(self, event_id: UUID) -> dict: ...

    def confirm_preview_policy(
        self,
        event_id: UUID,
        *,
        enabled: bool,
        template: WatermarkTemplate,
        text: str,
        logo_kind: WatermarkLogoKind,
        mark_png: bytes,
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
        is_cancelled: Callable[[], bool] | None = None,
        on_stage: Callable[[str, int, int], None] | None = None,
    ) -> None: ...


class DesktopGateway(PhotographerSessionGateway, BatchUploadService, Protocol):
    pass


class Session3Gateway:
    """Make the incomplete network boundary explicit in normal application mode."""

    _MESSAGE = (
        "Online photographer authentication is delivered in Session 4. "
        "Launch with --demo to exercise Session 3 local inventory."
    )

    def sign_in_photographer(
        self,
        server_url: str,
        username: str,
        password: str,
        device_label: str,
    ) -> Sequence[EventCache]:
        del server_url, username, password, device_label
        raise OnlineServicesUnavailable(self._MESSAGE)

    def resume(self, server_url: str) -> Sequence[EventCache]:
        del server_url
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

    def confirm_preview_policy(
        self,
        event_id: UUID,
        *,
        enabled: bool,
        template: WatermarkTemplate,
        text: str,
        logo_kind: WatermarkLogoKind,
        mark_png: bytes,
    ) -> EventCache:
        del event_id, enabled, template, text, logo_kind, mark_png
        raise OnlineServicesUnavailable(self._MESSAGE)

    def upload(
        self,
        batch_id: UUID,
        *,
        transfer_limit: int,
        on_progress: Callable[[int, int], None],
        is_cancelled: Callable[[], bool] | None = None,
        on_stage: Callable[[str, int, int], None] | None = None,
    ) -> None:
        del batch_id, transfer_limit, on_progress, is_cancelled, on_stage
        raise OnlineServicesUnavailable(self._MESSAGE)


class DiagnosticExporter(Protocol):
    def export(self, batch_id: UUID, destination: Path) -> None: ...
