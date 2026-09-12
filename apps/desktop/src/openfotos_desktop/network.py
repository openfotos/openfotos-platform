"""Authenticated desktop API access and resumable original/derivative sync."""

import base64
import hashlib
import os
import random
import re
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from urllib.parse import parse_qs, urlparse
from uuid import NAMESPACE_URL, UUID, uuid5

import httpx

from openfotos_contracts import (
    DERIVATIVE_PROFILE_ID,
    WATERMARK_RENDERER_ID,
    DerivativeVariant,
    WatermarkLogoKind,
    WatermarkTemplate,
)

from .credentials import RefreshTokenStore
from .derivatives import DerivativeError, DerivativeRenderer, RenderPolicy
from .ingestion import (
    BatchState,
    CheckpointStore,
    EventCache,
    InventoryStatus,
    LocalUploadState,
    PreviewPolicyCache,
)
from .ingestion.validation import file_checksums

_CHUNK_BYTES = 1024 * 1024
_MAX_UPLOAD_ATTEMPTS = 5
_FATAL_DERIVATIVE_CODES = frozenset(
    {
        "asset_not_found",
        "derivative_manifest_conflict",
        "derivative_profile_mismatch",
        "device_revoked",
        "event_not_found",
        "event_not_processing",
        "invalid_access_token",
        "invalid_server_response",
        "original_not_verified",
        "original_readback_checksum_mismatch",
        "preview_policy_mismatch",
        "preview_policy_not_confirmed",
        "session_unavailable",
        "source_checksum_mismatch",
        "watermark_checksum_mismatch",
    }
)


class DesktopApiError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class SourceChangedError(DesktopApiError):
    def __init__(self) -> None:
        super().__init__(
            "source_changed",
            "The source bytes changed after approval; add the changed photograph to a new batch.",
        )


@dataclass
class _Session:
    server_url: str
    access_token: str
    refresh_token: str


class _DigestingBody:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.sha256 = hashlib.sha256()
        self.md5 = hashlib.md5(usedforsecurity=False)

    def __iter__(self):
        with self.path.open("rb") as source:
            while chunk := source.read(_CHUNK_BYTES):
                self.sha256.update(chunk)
                self.md5.update(chunk)
                yield chunk


class DesktopNetworkService:
    """One installation's authenticated control-plane and direct-upload client."""

    def __init__(
        self,
        store: CheckpointStore,
        *,
        token_store: RefreshTokenStore | None = None,
        api_client: httpx.Client | None = None,
        storage_client: httpx.Client | None = None,
        sleeper=time.sleep,
        jitter=random.uniform,
    ) -> None:
        self.store = store
        self.token_store = token_store or RefreshTokenStore()
        timeout = httpx.Timeout(30, connect=10)
        self.api_client = api_client or httpx.Client(timeout=timeout)
        self.storage_client = storage_client or httpx.Client(timeout=httpx.Timeout(600, connect=15))
        self._owns_api_client = api_client is None
        self._owns_storage_client = storage_client is None
        self._session: _Session | None = None
        self._refresh_lock = Lock()
        self._sleep = sleeper
        self._jitter = jitter

    @property
    def persistence_warning(self) -> str | None:
        return self.token_store.persistence_warning

    def close(self) -> None:
        if self._owns_api_client:
            self.api_client.close()
        if self._owns_storage_client:
            self.storage_client.close()

    def sign_in_lead(
        self,
        server_url: str,
        username: str,
        password: str,
        device_label: str,
    ) -> list[EventCache]:
        origin = _server_origin(server_url)
        label = _device_label(device_label)
        response = self._public_post(
            origin,
            "/api/v1/auth/login/",
            {
                "username": username,
                "password": password,
                "installation_id": str(self.store.installation_id),
            },
        )
        self._accept_tokens(origin, response)
        return self._cache_events(origin, response["events"], device_label=label)

    def enroll_uploader(self, server_url: str, invitation: str, device_label: str) -> EventCache:
        origin, token = _invitation_parts(server_url, invitation)
        label = _device_label(device_label)
        response = self._public_post(
            origin,
            "/api/v1/uploader-invitations/redeem/",
            {
                "invitation_token": token,
                "installation_id": str(self.store.installation_id),
                "device_label": label,
            },
        )
        self._accept_tokens(origin, response)
        return self._cache_events(origin, [response["event"]], device_label=label)[0]

    def resume(self, server_url: str) -> list[EventCache]:
        origin = _server_origin(server_url)
        refresh_token = self.token_store.load(origin, self.store.installation_id)
        if not refresh_token:
            raise DesktopApiError("session_unavailable", "Sign in to continue.")
        self._session = _Session(origin, "", refresh_token)
        self._refresh()
        response = self._request("GET", "/api/v1/events/")
        return self._cache_events(origin, response["events"])

    def create_invitation(self, event_id: UUID) -> str:
        response = self._request("POST", f"/api/v1/events/{event_id}/invitations/", json={})
        return response["enrollment_url"]

    def close_intake(self, event_id: UUID) -> EventCache:
        return self._event_action(event_id, "intake/close")

    def reopen_intake(self, event_id: UUID) -> EventCache:
        return self._event_action(event_id, "intake/reopen")

    def finalize(self, event_id: UUID) -> dict:
        return self._request(
            "POST",
            f"/api/v1/events/{event_id}/finalize/",
            json={},
            idempotency_key=_operation_key(event_id, "finalize"),
        )

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

    def upload(
        self,
        batch_id: UUID,
        *,
        transfer_limit: int,
        on_progress,
        is_cancelled=None,
        on_stage=None,
    ) -> None:
        if transfer_limit not in range(1, 5):
            raise ValueError("Use one to four concurrent transfers.")
        batch = self.store.get_batch(batch_id)
        event = self.store.get_event(batch.event_id)
        items = self._manifest_items(batch_id)
        if not items:
            raise DesktopApiError("empty_manifest", "The contribution has no accepted originals.")
        if batch.state is BatchState.APPROVED:
            if not event.device_label:
                raise DesktopApiError(
                    "device_label_required",
                    "Enter a workstation label by signing in or enrolling again.",
                )
            contribution = {
                "batch_id": str(batch.id),
                "label": batch.label,
                "processing_profile_id": event.processing_profile_id,
                "device_label": event.device_label,
                "assets": [
                    {
                        "id": str(item.id),
                        "filename": item.basename,
                        "size_bytes": item.snapshot.size_bytes,
                        "sha256": item.sha256,
                        "content_md5": item.content_md5,
                        "width": item.width,
                        "height": item.height,
                    }
                    for item in sorted(items.values(), key=lambda value: str(value.id))
                ],
            }
            self._request(
                "POST",
                f"/api/v1/events/{event.id}/batches/",
                json=contribution,
                idempotency_key=_operation_key(batch.id, "reserve"),
            )
            self.store.mark_batch_reserved(batch.id)
        elif batch.state not in {BatchState.RESERVED, BatchState.UPLOADING, BatchState.COMPLETE}:
            raise DesktopApiError(
                "batch_not_approved", "Approve the local contribution before uploading."
            )
        self._upload_originals(
            event=event,
            batch_id=batch.id,
            items=items,
            transfer_limit=transfer_limit,
            on_progress=on_progress,
            on_stage=on_stage,
            is_cancelled=is_cancelled,
        )
        if is_cancelled and is_cancelled():
            return
        event = self.store.get_event(event.id)
        if event.preview_policy is None:
            event = self._refresh_cached_event(event.id)
        if event.preview_policy is None:
            raise DesktopApiError(
                "preview_policy_not_confirmed",
                "The event lead must confirm preview settings before gallery processing.",
            )
        self.store.ensure_derivative_checkpoints(batch.id)
        self._sync_batch(event.id, batch.id)
        self._process_derivatives(
            event=event,
            batch_id=batch.id,
            items=items,
            on_stage=on_stage,
            is_cancelled=is_cancelled,
        )

    def _upload_originals(
        self,
        *,
        event: EventCache,
        batch_id: UUID,
        items: dict[UUID, object],
        transfer_limit: int,
        on_progress,
        on_stage,
        is_cancelled,
    ) -> None:
        while True:
            self._sync_batch(event.id, batch_id)
            checkpoints = {
                checkpoint.item_id: checkpoint
                for checkpoint in self.store.list_upload_checkpoints(batch_id)
            }
            completed = sum(
                checkpoint.state is LocalUploadState.VERIFIED for checkpoint in checkpoints.values()
            )
            terminal = sum(
                checkpoint.state in {LocalUploadState.VERIFIED, LocalUploadState.EXCLUDED}
                for checkpoint in checkpoints.values()
            )
            on_progress(completed, len(items))
            if on_stage:
                on_stage("originals", completed, len(items))
            pending_ids = [
                item_id
                for item_id, checkpoint in checkpoints.items()
                if checkpoint.state not in {LocalUploadState.VERIFIED, LocalUploadState.EXCLUDED}
                and checkpoint.attempt_count < _MAX_UPLOAD_ATTEMPTS
            ]
            if not pending_ids:
                if terminal == len(items):
                    return
                raise DesktopApiError(
                    "upload_attempts_exhausted",
                    "One or more originals need an explicit retry or lead exclusion.",
                )
            if is_cancelled and is_cancelled():
                return
            lease_response = self._request(
                "POST",
                f"/api/v1/events/{event.id}/batches/{batch_id}/upload-leases/",
                json={"asset_ids": [str(item_id) for item_id in pending_ids[:transfer_limit]]},
            )
            leases = lease_response["leases"]
            if not leases:
                raise DesktopApiError(
                    "upload_state_unavailable",
                    "The server returned no upload work for pending originals.",
                    retryable=True,
                )
            with ThreadPoolExecutor(max_workers=transfer_limit) as executor:
                futures = {
                    executor.submit(
                        self._upload_one,
                        event.id,
                        items[UUID(lease["asset_id"])],
                        lease,
                    ): lease
                    for lease in leases
                }
                terminal_error = None
                for future in as_completed(futures):
                    try:
                        future.result()
                    except DesktopApiError as exc:
                        if not exc.retryable and terminal_error is None:
                            terminal_error = exc
                if terminal_error is not None:
                    raise terminal_error

    def _upload_one(self, event_id: UUID, item, lease: dict) -> None:
        self.store.mark_upload_started(item.id)
        try:
            current = item.source_path.stat()
            if (
                current.st_size != item.snapshot.size_bytes
                or current.st_mtime_ns != item.snapshot.modified_ns
                or current.st_ctime_ns != item.snapshot.changed_ns
            ):
                raise SourceChangedError
            body = _DigestingBody(item.source_path)
            response = self.storage_client.put(
                lease["url"],
                headers=lease["headers"],
                content=body,
            )
            if response.status_code != 412:
                streamed_sha256 = body.sha256.hexdigest()
                streamed_md5 = base64.b64encode(body.md5.digest()).decode()
                if streamed_sha256 != item.sha256 or streamed_md5 != item.content_md5:
                    raise SourceChangedError
            if response.status_code not in {200, 201, 204, 412}:
                retryable = response.status_code in {408, 429} or response.status_code >= 500
                raise DesktopApiError(
                    "upload_http_error",
                    f"Object storage rejected an upload with HTTP {response.status_code}.",
                    retryable=retryable,
                )
            self._request(
                "POST",
                f"/api/v1/events/{event_id}/assets/{item.id}/complete/",
                json={},
                idempotency_key=_operation_key(item.id, "complete-original"),
            )
        except SourceChangedError:
            self.store.mark_upload_failed(item.id, "source_changed")
            raise
        except (httpx.HTTPError, OSError) as exc:
            self.store.mark_upload_failed(item.id, "upload_interrupted")
            self._backoff(item.id)
            raise DesktopApiError(
                "upload_interrupted", "The upload was interrupted.", retryable=True
            ) from exc
        except DesktopApiError as exc:
            self.store.mark_upload_failed(item.id, exc.code)
            if exc.retryable:
                self._backoff(item.id)
            raise
        else:
            self.store.mark_upload_verified(item.id)

    def _backoff(self, item_id: UUID) -> None:
        checkpoint = self.store.get_upload_checkpoint(item_id)
        maximum = min(16.0, 2.0 ** max(0, checkpoint.attempt_count - 1))
        self._sleep(self._jitter(0.0, maximum))

    def _process_derivatives(
        self,
        *,
        event: EventCache,
        batch_id: UUID,
        items: dict[UUID, object],
        on_stage,
        is_cancelled,
    ) -> None:
        policy = event.preview_policy
        if policy is None:  # guarded by upload; retained as a narrow type boundary
            raise DesktopApiError(
                "preview_policy_not_confirmed", "Preview settings are unavailable."
            )
        mark_png = self._policy_mark(event.id, policy)
        cache_directory = self.store.derivative_cache_directory(batch_id)
        renderer = DerivativeRenderer()
        try:
            for item in sorted(items.values(), key=lambda value: str(value.id)):
                if is_cancelled and is_cancelled():
                    return
                checkpoints = self._item_derivative_checkpoints(item.id)
                if all(
                    checkpoint.state in {LocalUploadState.VERIFIED, LocalUploadState.EXCLUDED}
                    for checkpoint in checkpoints.values()
                ):
                    self._report_derivative_progress(batch_id, on_stage)
                    continue
                while True:
                    pending = [
                        checkpoint
                        for checkpoint in checkpoints.values()
                        if checkpoint.state
                        not in {LocalUploadState.VERIFIED, LocalUploadState.EXCLUDED}
                    ]
                    attempts = max(checkpoint.attempt_count for checkpoint in pending)
                    if attempts >= _MAX_UPLOAD_ATTEMPTS:
                        raise DesktopApiError(
                            "derivative_attempts_exhausted",
                            "A gallery derivative failed five times and now needs lead review.",
                        )
                    try:
                        self._process_asset_derivatives(
                            event=event,
                            item=item,
                            policy=policy,
                            mark_png=mark_png,
                            renderer=renderer,
                            cache_directory=cache_directory,
                            pending=pending,
                        )
                    except (DerivativeError, DesktopApiError) as exc:
                        code = exc.code
                        current = self._item_derivative_checkpoints(item.id)
                        for checkpoint in current.values():
                            if checkpoint.state not in {
                                LocalUploadState.VERIFIED,
                                LocalUploadState.EXCLUDED,
                            }:
                                self.store.mark_derivative_failed(item.id, checkpoint.variant, code)
                        if isinstance(exc, DesktopApiError) and exc.code in _FATAL_DERIVATIVE_CODES:
                            raise
                        self._report_derivative_failure(
                            event.id,
                            item.id,
                            code,
                            attempt=max(value.attempt_count for value in current.values()),
                        )
                        checkpoints = self._item_derivative_checkpoints(item.id)
                        if max(value.attempt_count for value in checkpoints.values()) >= 5:
                            raise DesktopApiError(
                                "derivative_attempts_exhausted",
                                "A gallery derivative failed five times and now needs lead review.",
                            ) from exc
                        if isinstance(exc, DesktopApiError) and exc.retryable:
                            self._derivative_backoff(checkpoints)
                        continue
                    self._sync_batch(event.id, batch_id)
                    checkpoints = self._item_derivative_checkpoints(item.id)
                    self._report_derivative_progress(batch_id, on_stage)
                    if all(
                        checkpoint.state in {LocalUploadState.VERIFIED, LocalUploadState.EXCLUDED}
                        for checkpoint in checkpoints.values()
                    ):
                        break
            if not self.store.derivatives_complete(batch_id):
                raise DesktopApiError(
                    "derivative_state_unavailable",
                    "Gallery processing stopped before every derivative was verified.",
                    retryable=True,
                )
        finally:
            self.store.cleanup_derivative_cache(batch_id)

    def _process_asset_derivatives(
        self,
        *,
        event: EventCache,
        item,
        policy: PreviewPolicyCache,
        mark_png: bytes,
        renderer: DerivativeRenderer,
        cache_directory: Path,
        pending: list,
    ) -> None:
        for checkpoint in pending:
            self.store.mark_derivative_started(item.id, checkpoint.variant)
        source_path, downloaded = self._derivative_source(event.id, item, cache_directory)
        rendered = None
        try:
            rendered = renderer.render(
                source_path,
                expected_source_sha256=item.sha256,
                policy=RenderPolicy(
                    enabled=policy.enabled,
                    template=policy.template,
                    mark_png=mark_png,
                ),
                cache_directory=cache_directory,
                asset_stem=str(item.id),
            )
            response = self._request(
                "POST",
                f"/api/v1/events/{event.id}/assets/{item.id}/derivatives/",
                json={
                    "asset_id": str(item.id),
                    "source_sha256": item.sha256,
                    "policy_id": str(policy.id),
                    "profile_id": policy.derivative_profile_id,
                    "captured_at": (
                        rendered.captured_at.isoformat() if rendered.captured_at else None
                    ),
                    "objects": [rendered.preview.as_contract(), rendered.thumbnail.as_contract()],
                },
                idempotency_key=_operation_key(item.id, "register-derivatives"),
            )
            rendered_by_variant = {
                rendered.preview.variant.value: rendered.preview,
                rendered.thumbnail.variant.value: rendered.thumbnail,
            }
            pending_variants = []
            for value in response["objects"]:
                variant = value["variant"]
                if value["state"] == "verified":
                    self.store.mark_derivative_verified(item.id, variant)
                    rendered_by_variant[variant].path.unlink(missing_ok=True)
                else:
                    pending_variants.append(variant)
            if not pending_variants:
                return
            lease_response = self._request(
                "POST",
                f"/api/v1/events/{event.id}/assets/{item.id}/derivative-leases/",
                json={"variants": pending_variants},
            )
            if len(lease_response["leases"]) != len(pending_variants):
                raise DesktopApiError(
                    "derivative_state_unavailable",
                    "The server returned incomplete derivative upload work.",
                    retryable=True,
                )
            for lease in lease_response["leases"]:
                variant = lease["variant"]
                self._upload_derivative(
                    event_id=event.id,
                    asset_id=item.id,
                    rendered=rendered_by_variant[variant],
                    lease=lease,
                )
                self.store.mark_derivative_verified(item.id, variant)
                rendered_by_variant[variant].path.unlink(missing_ok=True)
        finally:
            if downloaded:
                source_path.unlink(missing_ok=True)
            if rendered is not None:
                rendered.preview.path.unlink(missing_ok=True)
                rendered.thumbnail.path.unlink(missing_ok=True)

    def _derivative_source(self, event_id: UUID, item, cache_directory: Path) -> tuple[Path, bool]:
        try:
            if item.source_path.is_file() and _path_sha256(item.source_path) == item.sha256:
                return item.source_path, False
        except OSError:
            pass
        data = self._request(
            "POST",
            f"/api/v1/events/{event_id}/assets/{item.id}/source-url/",
            json={},
        )
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f"{item.id}-source-", suffix=".jpg", dir=cache_directory
        )
        os.close(descriptor)
        destination = Path(temporary_name)
        if os.name != "nt":
            destination.chmod(0o600)
        digest = hashlib.sha256()
        try:
            with self.storage_client.stream("GET", data["url"]) as response:
                if response.status_code != 200:
                    raise DesktopApiError(
                        "original_readback_failed",
                        f"Private original read-back failed with HTTP {response.status_code}.",
                        retryable=response.status_code in {408, 429} or response.status_code >= 500,
                    )
                with destination.open("wb") as output:
                    for chunk in response.iter_bytes(_CHUNK_BYTES):
                        digest.update(chunk)
                        output.write(chunk)
            if digest.hexdigest() != item.sha256 or data["sha256"] != item.sha256:
                raise DesktopApiError(
                    "original_readback_checksum_mismatch",
                    "The private original read-back failed checksum validation.",
                )
        except Exception:
            destination.unlink(missing_ok=True)
            raise
        return destination, True

    def _upload_derivative(self, *, event_id: UUID, asset_id: UUID, rendered, lease: dict) -> None:
        body = _DigestingBody(rendered.path)
        try:
            response = self.storage_client.put(lease["url"], headers=lease["headers"], content=body)
        except (httpx.HTTPError, OSError) as exc:
            raise DesktopApiError(
                "derivative_upload_interrupted",
                "A gallery derivative upload was interrupted.",
                retryable=True,
            ) from exc
        if response.status_code != 412 and (
            body.sha256.hexdigest() != rendered.sha256
            or base64.b64encode(body.md5.digest()).decode() != rendered.content_md5
        ):
            raise DesktopApiError(
                "derivative_cache_changed", "A cached gallery derivative changed before upload."
            )
        if response.status_code not in {200, 201, 204, 412}:
            raise DesktopApiError(
                "derivative_upload_http_error",
                f"Object storage rejected a derivative with HTTP {response.status_code}.",
                retryable=response.status_code in {408, 429} or response.status_code >= 500,
            )
        self._request(
            "POST",
            f"/api/v1/events/{event_id}/assets/{asset_id}/derivatives/"
            f"{rendered.variant.value}/complete/",
            json={},
            idempotency_key=_operation_key(asset_id, f"complete-{rendered.variant.value}"),
        )

    def _policy_mark(self, event_id: UUID, policy: PreviewPolicyCache) -> bytes:
        if not policy.enabled:
            return b""
        data = self._request("GET", f"/api/v1/events/{event_id}/preview-policy/mark/")
        try:
            response = self.storage_client.get(data["url"])
        except httpx.HTTPError as exc:
            raise DesktopApiError(
                "watermark_download_interrupted",
                "The confirmed watermark could not be downloaded.",
                retryable=True,
            ) from exc
        if response.status_code != 200 or len(response.content) > 4 * 1024 * 1024:
            raise DesktopApiError(
                "watermark_download_failed",
                "The confirmed watermark could not be downloaded.",
                retryable=response.status_code in {408, 429} or response.status_code >= 500,
            )
        if (
            hashlib.sha256(response.content).hexdigest() != policy.mark_sha256
            or data["sha256"] != policy.mark_sha256
        ):
            raise DesktopApiError(
                "watermark_checksum_mismatch",
                "The confirmed watermark failed checksum validation.",
            )
        return response.content

    def _item_derivative_checkpoints(self, item_id: UUID) -> dict[str, object]:
        return {
            variant.value: self.store.get_derivative_checkpoint(item_id, variant.value)
            for variant in DerivativeVariant
        }

    def _report_derivative_failure(
        self, event_id: UUID, item_id: UUID, code: str, *, attempt: int
    ) -> None:
        stable_code = code if re.fullmatch(r"[a-z0-9_]{1,64}", code) else "derivative_failed"
        self._request(
            "POST",
            f"/api/v1/events/{event_id}/assets/{item_id}/derivative-failure/",
            json={"code": stable_code[:64]},
            idempotency_key=_operation_key(item_id, f"derivative-failure-{attempt}"),
        )

    def _derivative_backoff(self, checkpoints: dict[str, object]) -> None:
        attempts = max(checkpoint.attempt_count for checkpoint in checkpoints.values())
        maximum = min(16.0, 2.0 ** max(0, attempts - 1))
        self._sleep(self._jitter(0.0, maximum))

    def _report_derivative_progress(self, batch_id: UUID, on_stage) -> None:
        if not on_stage:
            return
        checkpoints = self.store.list_derivative_checkpoints(batch_id)
        complete = sum(
            item.state in {LocalUploadState.VERIFIED, LocalUploadState.EXCLUDED}
            for item in checkpoints
        )
        on_stage("derivatives", complete, len(checkpoints))

    def _manifest_items(self, batch_id: UUID) -> dict[UUID, object]:
        items = {
            item.id: item
            for item in self.store.list_items(batch_id)
            if item.status is InventoryStatus.ACCEPTED
        }
        for item in items.values():
            if item.content_md5:
                continue
            sha256, content_md5 = file_checksums(item.source_path)
            if sha256 != item.sha256:
                raise SourceChangedError
            self.store.set_content_md5(item.id, content_md5)
            items[item.id] = self.store.get_item(item.id)
        return items

    def _sync_batch(self, event_id: UUID, batch_id: UUID) -> None:
        response = self._request("GET", f"/api/v1/events/{event_id}/batches/{batch_id}/")
        local = {item.item_id: item for item in self.store.list_upload_checkpoints(batch_id)}
        for item in response.get("assets", []):
            item_id = UUID(item["asset_id"])
            variant = item.get("variant", "originals")
            if item_id not in local:
                continue
            if variant == "originals":
                if (
                    item["state"] == "verified"
                    and local[item_id].state is not LocalUploadState.VERIFIED
                ):
                    self.store.mark_upload_verified(item_id)
                elif item["state"] == "failed":
                    self.store.mark_upload_failed(
                        item_id, item["failure_code"] or "server_rejected"
                    )
                elif item["state"] == "excluded":
                    self.store.mark_upload_excluded(item_id)
                    self.store.mark_derivatives_excluded(item_id)
            elif variant in {value.value for value in DerivativeVariant}:
                if item.get("gallery_excluded"):
                    self.store.mark_derivatives_excluded(item_id)
                elif item["state"] == "verified":
                    self.store.mark_derivative_verified(item_id, variant)
                elif item["state"] == "failed":
                    self.store.mark_derivative_failed(
                        item_id, variant, item["failure_code"] or "server_rejected"
                    )

    def _event_action(self, event_id: UUID, action: str) -> EventCache:
        response = self._request(
            "POST",
            f"/api/v1/events/{event_id}/{action}/",
            json={},
            idempotency_key=_operation_key(event_id, action),
        )
        return self._cache_events(self._require_session().server_url, [response])[0]

    def _refresh_cached_event(self, event_id: UUID) -> EventCache:
        response = self._request("GET", "/api/v1/events/")
        matches = [value for value in response["events"] if UUID(value["id"]) == event_id]
        if not matches:
            raise DesktopApiError("event_not_found", "The event is no longer available.")
        return self._cache_events(self._require_session().server_url, matches)[0]

    def _accept_tokens(self, origin: str, response: dict) -> None:
        session = _Session(
            server_url=origin,
            access_token=response["access_token"],
            refresh_token=response["refresh_token"],
        )
        self._session = session
        self.token_store.save(origin, self.store.installation_id, session.refresh_token)

    def _cache_events(
        self,
        origin: str,
        values: list[dict],
        *,
        device_label: str = "",
    ) -> list[EventCache]:
        cached_labels = {event.id: event.device_label for event in self.store.list_events()}
        events = [
            EventCache(
                id=UUID(value["id"]),
                name=value["name"],
                storage_limit_bytes=value["storage_limit_bytes"],
                processing_profile_id=value["processing_profile_id"],
                server_url=origin,
                role=value["role"],
                reserved_original_bytes=value["reserved_original_bytes"],
                verified_original_bytes=value["verified_original_bytes"],
                intake_state=value["intake_state"],
                intake_generation=value["intake_generation"],
                device_label=(
                    value.get("device_label")
                    or device_label
                    or cached_labels.get(UUID(value["id"]), "")
                ),
                preview_policy=_preview_policy(value.get("preview_policy")),
            )
            for value in values
        ]
        for event in events:
            self.store.cache_event(event)
        return events

    def _request(self, method: str, path: str, *, json=None, idempotency_key=None) -> dict:
        session = self._require_session()
        headers = {"Authorization": f"Bearer {session.access_token}"}
        if idempotency_key:
            headers["Idempotency-Key"] = str(idempotency_key)
        try:
            response = self.api_client.request(
                method,
                f"{session.server_url}{path}",
                json=json,
                headers=headers,
            )
        except httpx.HTTPError as exc:
            raise DesktopApiError(
                "server_unavailable",
                "The OpenFotos server could not be reached.",
                retryable=True,
            ) from exc
        if response.status_code == 401:
            stale_access_token = session.access_token
            with self._refresh_lock:
                if self._require_session().access_token == stale_access_token:
                    self._refresh()
            session = self._require_session()
            headers["Authorization"] = f"Bearer {session.access_token}"
            try:
                response = self.api_client.request(
                    method,
                    f"{session.server_url}{path}",
                    json=json,
                    headers=headers,
                )
            except httpx.HTTPError as exc:
                raise DesktopApiError(
                    "server_unavailable",
                    "The OpenFotos server could not be reached.",
                    retryable=True,
                ) from exc
        return _response_json(response)

    def _refresh(self) -> None:
        session = self._require_session()
        try:
            response = self._public_post(
                session.server_url,
                "/api/v1/auth/refresh/",
                {"refresh_token": session.refresh_token},
            )
        except DesktopApiError as exc:
            if exc.code in {"invalid_access_token", "invalid_refresh_token"}:
                self.token_store.delete(session.server_url, self.store.installation_id)
                self._session = None
            raise
        self._accept_tokens(session.server_url, response)

    def _public_post(self, origin: str, path: str, body: dict) -> dict:
        try:
            response = self.api_client.post(f"{origin}{path}", json=body)
        except httpx.HTTPError as exc:
            raise DesktopApiError(
                "server_unavailable", "The OpenFotos server could not be reached.", retryable=True
            ) from exc
        return _response_json(response)

    def _require_session(self) -> _Session:
        if self._session is None:
            raise DesktopApiError("session_unavailable", "Sign in to continue.")
        return self._session


def _response_json(response: httpx.Response) -> dict:
    try:
        body = response.json()
    except ValueError as exc:
        raise DesktopApiError(
            "invalid_server_response", "The OpenFotos server returned an invalid response."
        ) from exc
    if response.is_error:
        error = body.get("error", {}) if isinstance(body, dict) else {}
        raise DesktopApiError(
            str(error.get("code", "server_error")),
            str(error.get("message", "The OpenFotos request failed.")),
            retryable=bool(error.get("retryable")),
        )
    if not isinstance(body, dict):
        raise DesktopApiError(
            "invalid_server_response", "The OpenFotos server returned an invalid response."
        )
    return body


def _preview_policy(value: object) -> PreviewPolicyCache | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise DesktopApiError(
            "invalid_server_response", "The server returned an invalid preview policy."
        )
    try:
        if not isinstance(value["enabled"], bool):
            raise TypeError
        policy = PreviewPolicyCache(
            id=UUID(str(value["id"])),
            enabled=value["enabled"],
            template=WatermarkTemplate(str(value["template"])),
            text=str(value["text"]),
            logo_kind=WatermarkLogoKind(str(value["logo_kind"])),
            renderer_id=str(value["renderer_id"]),
            derivative_profile_id=str(value["derivative_profile_id"]),
            mark_sha256=str(value["mark_sha256"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise DesktopApiError(
            "invalid_server_response", "The server returned an invalid preview policy."
        ) from exc
    if (
        policy.renderer_id != WATERMARK_RENDERER_ID
        or policy.derivative_profile_id != DERIVATIVE_PROFILE_ID
    ):
        raise DesktopApiError(
            "derivative_profile_mismatch",
            "This desktop version cannot process the event's gallery profile.",
        )
    if policy.enabled and (
        not re.fullmatch(r"[0-9a-f]{64}", policy.mark_sha256)
        or (policy.logo_kind is WatermarkLogoKind.NONE and not policy.text)
    ):
        raise DesktopApiError(
            "invalid_server_response", "The server returned an invalid preview policy."
        )
    return policy


def _path_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def _server_origin(value: str) -> str:
    parsed = urlparse(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username:
        raise DesktopApiError("invalid_server_url", "Enter a valid OpenFotos server URL.")
    local_hostname = parsed.hostname == "localhost" or parsed.hostname.endswith(".localhost")
    if (
        parsed.scheme != "https"
        and not local_hostname
        and parsed.hostname not in {"127.0.0.1", "::1"}
    ):
        raise DesktopApiError(
            "invalid_server_url", "OpenFotos requires HTTPS except for a local test server."
        )
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise DesktopApiError("invalid_server_url", "Enter the OpenFotos server origin only.")
    return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")


def _invitation_parts(server_url: str, invitation: str) -> tuple[str, str]:
    parsed = urlparse(invitation.strip())
    if parsed.scheme and parsed.netloc:
        origin = _server_origin(f"{parsed.scheme}://{parsed.netloc}")
        token = parse_qs(parsed.fragment).get("invite", [""])[0]
    else:
        origin = _server_origin(server_url)
        token = invitation.strip()
    if not token.startswith("ofts_invite_"):
        raise DesktopApiError("invalid_invitation", "Paste a complete OpenFotos invitation.")
    return origin, token


def _device_label(value: str) -> str:
    label = value.strip()
    if not label or len(label) > 100 or any(ord(character) < 32 for character in label):
        raise DesktopApiError(
            "invalid_device_label",
            "Use a workstation label containing 1 to 100 visible characters.",
        )
    return label


def _operation_key(identifier: UUID, operation: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"openfotos:{identifier}:{operation}")
