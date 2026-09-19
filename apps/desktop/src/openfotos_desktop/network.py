"""Desktop facade for authenticated API access and resumable batch sync."""

import base64
import random
import time
from collections.abc import Callable
from urllib.parse import urlparse
from uuid import UUID

import httpx

from openfotos_contracts import (
    ContractError,
    EventSnapshot,
    WatermarkLogoKind,
    WatermarkTemplate,
)
from openfotos_vision import ACCEPTED_FACE_MODEL_CONTRACT, FaceEngine

from .api_client import AuthenticatedApiClient
from .batch_sync import BatchSyncService
from .batch_sync import operation_key as _operation_key
from .credentials import RefreshTokenStore
from .errors import DesktopApiError
from .errors import SourceChangedError as SourceChangedError
from .face_models import FaceModelStore, create_accepted_face_engine
from .ingestion import CheckpointStore, EventCache, PreviewPolicyCache, SubEventCache
from .object_transfer import ObjectTransferClient


class DesktopNetworkService:
    """Keep the desktop-facing network API stable across narrower components."""

    def __init__(
        self,
        store: CheckpointStore,
        *,
        token_store: RefreshTokenStore | None = None,
        api_client: httpx.Client | None = None,
        storage_client: httpx.Client | None = None,
        sleeper=time.sleep,
        jitter=random.uniform,
        face_engine_factory: Callable[[], FaceEngine] | None = None,
        face_model_store: FaceModelStore | None = None,
    ) -> None:
        self.store = store
        self._api = AuthenticatedApiClient(
            store.installation_id,
            token_store=token_store,
            client=api_client,
        )
        self._objects = ObjectTransferClient(storage_client)
        if face_engine_factory is None and face_model_store is not None:

            def face_engine_factory() -> FaceEngine:
                return create_accepted_face_engine(face_model_store)

        self._batch_sync = BatchSyncService(
            store,
            request=self._request,
            refresh_event=self._refresh_cached_event,
            objects=self._objects,
            sleeper=sleeper,
            jitter=jitter,
            ensure_preview_policy=self._ensure_clean_preview_policy,
            **(
                {"face_engine_factory": face_engine_factory}
                if face_engine_factory is not None
                else {}
            ),
        )
        # Retain these attributes for callers that supplied and inspected custom clients.
        self.token_store = self._api.token_store
        self.api_client = self._api.client
        self.storage_client = self._objects.client

    @property
    def persistence_warning(self) -> str | None:
        return self._api.persistence_warning

    def close(self) -> None:
        self._api.close()
        self._objects.close()

    def sign_in_photographer(
        self,
        server_url: str,
        username: str,
        password: str,
        device_label: str,
    ) -> list[EventCache]:
        origin = _server_origin(server_url)
        label = _device_label(device_label)
        response = self._api.start_session(
            origin,
            "/api/v1/auth/login/",
            {
                "username": username,
                "password": password,
                "installation_id": str(self.store.installation_id),
            },
        )
        return self._cache_events(origin, response["events"], device_label=label)

    def resume(self, server_url: str) -> list[EventCache]:
        origin = _server_origin(server_url)
        response = self._api.resume(origin)
        return self._cache_events(origin, response["events"])

    def saved_session_origin(self) -> str | None:
        """Return the only cached server origin holding a saved refresh token, if any."""
        origins = {event.server_url for event in self.store.list_events() if event.server_url}
        if len(origins) != 1:
            return None
        [origin] = origins
        if self._api.saved_refresh_token(origin) is None:
            return None
        return origin

    def sign_out(self) -> None:
        self._api.end_session()

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
        self._request(
            "POST",
            f"/api/v1/events/{event_id}/preview-policy/confirm/",
            json={
                "enabled": enabled,
                "template": template.value,
                "text": text,
                "logo_kind": logo_kind.value,
                "mark_png_base64": base64.b64encode(mark_png).decode() if enabled else "",
            },
            idempotency_key=_operation_key(event_id, "confirm-preview-policy"),
        )
        return self._refresh_cached_event(event_id)

    def _ensure_clean_preview_policy(self, event_id: UUID) -> EventCache:
        """Record clean previews when the photographer never opened the watermark dialog."""
        return self.confirm_preview_policy(
            event_id,
            enabled=False,
            template=WatermarkTemplate.COMPACT_BOTTOM_RIGHT,
            text="",
            logo_kind=WatermarkLogoKind.NONE,
            mark_png=b"",
        )

    def upload(
        self,
        batch_id: UUID,
        *,
        transfer_limit: int,
        on_progress,
        is_cancelled=None,
        on_stage=None,
    ) -> None:
        self._batch_sync.upload(
            batch_id,
            transfer_limit=transfer_limit,
            on_progress=on_progress,
            is_cancelled=is_cancelled,
            on_stage=on_stage,
        )

    def _refresh_cached_event(self, event_id: UUID) -> EventCache:
        response = self._request("GET", "/api/v1/events/")
        try:
            matches = [value for value in response["events"] if UUID(value["id"]) == event_id]
        except (KeyError, TypeError, ValueError) as exc:
            raise DesktopApiError(
                "invalid_server_response",
                "The OneNodeAI Studio server returned an invalid response.",
            ) from exc
        if not matches:
            raise DesktopApiError("event_not_found", "The event is no longer available.")
        return self._cache_events(self._api.server_url, matches)[0]

    def _cache_events(
        self,
        origin: str,
        values: list[dict],
        *,
        device_label: str = "",
    ) -> list[EventCache]:
        cached_labels = {event.id: event.device_label for event in self.store.list_events()}
        try:
            snapshots = [EventSnapshot.from_dict(value) for value in values]
        except ContractError as exc:
            if exc.code == "profile_mismatch":
                raise DesktopApiError(
                    "derivative_profile_mismatch",
                    "This desktop version cannot process the event's gallery profile.",
                ) from exc
            raise DesktopApiError(
                "invalid_server_response",
                "The OneNodeAI Studio server returned an invalid response.",
            ) from exc
        events = []
        for snapshot in snapshots:
            if snapshot.face_model_id != ACCEPTED_FACE_MODEL_CONTRACT.model.id:
                raise DesktopApiError(
                    "face_model_mismatch",
                    "This desktop version cannot process the event's face model.",
                )
            policy = snapshot.preview_policy
            events.append(
                EventCache(
                    id=snapshot.id,
                    name=snapshot.name,
                    storage_limit_bytes=snapshot.storage_limit_bytes,
                    processing_profile_id=snapshot.processing_profile_id,
                    face_model_id=snapshot.face_model_id,
                    face_index_ready=snapshot.face_index_ready,
                    server_url=origin,
                    reserved_original_bytes=snapshot.reserved_original_bytes,
                    verified_original_bytes=snapshot.verified_original_bytes,
                    intake_state=snapshot.intake_state.value,
                    intake_generation=snapshot.intake_generation,
                    device_label=(
                        snapshot.device_label or device_label or cached_labels.get(snapshot.id, "")
                    ),
                    sub_events=tuple(
                        SubEventCache(
                            id=sub_event.id,
                            name=sub_event.name,
                            position=sub_event.position,
                        )
                        for sub_event in snapshot.sub_events
                    ),
                    preview_policy=(
                        PreviewPolicyCache(
                            id=policy.id,
                            enabled=policy.enabled,
                            template=policy.template,
                            text=policy.text,
                            logo_kind=policy.logo_kind,
                            renderer_id=policy.renderer_id,
                            derivative_profile_id=policy.derivative_profile_id,
                            mark_sha256=policy.mark_sha256,
                        )
                        if policy
                        else None
                    ),
                )
            )
        for event in events:
            self.store.cache_event(event)
        return events

    def _request(self, method: str, path: str, *, json=None, idempotency_key=None) -> dict:
        return self._api.request(
            method,
            path,
            json=json,
            idempotency_key=idempotency_key,
        )


def _server_origin(value: str) -> str:
    parsed = urlparse(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username:
        raise DesktopApiError("invalid_server_url", "Enter a valid OneNodeAI Studio server URL.")
    local_hostname = parsed.hostname == "localhost" or parsed.hostname.endswith(".localhost")
    if (
        parsed.scheme != "https"
        and not local_hostname
        and parsed.hostname not in {"127.0.0.1", "::1"}
    ):
        raise DesktopApiError(
            "invalid_server_url", "OneNodeAI Studio requires HTTPS except for a local test server."
        )
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise DesktopApiError(
            "invalid_server_url", "Enter the OneNodeAI Studio server origin only."
        )
    return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")


def _device_label(value: str) -> str:
    label = value.strip()
    if not label or len(label) > 100 or any(ord(character) < 32 for character in label):
        raise DesktopApiError(
            "invalid_device_label",
            "Use a workstation label containing 1 to 100 visible characters.",
        )
    return label
