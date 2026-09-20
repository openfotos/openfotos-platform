"""Resumable original, gallery-derivative, and face-index synchronization."""

import hashlib
import os
import queue
import re
import tempfile
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

import httpx

from openfotos_contracts import DERIVATIVE_VARIANTS, AssetVariant
from openfotos_vision import (
    ACCEPTED_FACE_MODEL_CONTRACT,
    ACCEPTED_SFACE_DETECTOR_FLOOR,
    FaceAnalysisDocumentError,
    FaceEngine,
    FaceEngineError,
    build_face_analysis_document,
)

from .derivatives import DerivativeError, DerivativeRenderer, RenderPolicy
from .errors import DesktopApiError, SourceChangedError
from .face_models import FaceModelSetupError, create_accepted_face_engine
from .ingestion import (
    BatchState,
    CheckpointStore,
    EventCache,
    InventoryStatus,
    LocalFaceState,
    LocalUploadState,
    PreviewPolicyCache,
)
from .ingestion.validation import file_checksums
from .object_transfer import ObjectTransferClient, path_sha256

_MAX_UPLOAD_ATTEMPTS = 5
# Backstop wait for a stage runner when no verified-original signal arrives. Signals are
# always preceded by their checkpoint writes, so a missed signal only costs this delay.
_STAGE_WAIT_SECONDS = 1.0
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
_FATAL_FACE_CODES = frozenset(
    {
        "asset_not_found",
        "device_revoked",
        "event_not_found",
        "event_not_processing",
        "face_analysis_attempts_exhausted",
        "face_analysis_conflict",
        "idempotency_conflict",
        "installation_revoked",
        "invalid_access_token",
        "invalid_server_response",
        "model_contract_mismatch",
        "original_not_verified",
        "session_unavailable",
        "source_checksum_mismatch",
        "sub_event_archived",
    }
)


def _worker_count(value: int | None) -> int:
    if value is None:
        return max(1, min(4, os.cpu_count() or 2))
    if value < 1:
        raise ValueError("Worker counts must be positive.")
    return value


class BatchSyncService:
    def __init__(
        self,
        store: CheckpointStore,
        *,
        request: Callable[..., dict],
        refresh_event: Callable[[UUID], EventCache],
        objects: ObjectTransferClient,
        sleeper: Callable[[float], None],
        jitter: Callable[[float, float], float],
        face_engine_factory: Callable[[], FaceEngine] = create_accepted_face_engine,
        ensure_preview_policy: Callable[[UUID], EventCache] | None = None,
        derivative_workers: int | None = None,
        face_workers: int | None = None,
    ) -> None:
        self.store = store
        self._request = request
        self._refresh_cached_event = refresh_event
        self._objects = objects
        self._sleep = sleeper
        self._jitter = jitter
        self._face_engine_factory = face_engine_factory
        self._ensure_preview_policy = ensure_preview_policy
        self._derivative_workers = _worker_count(derivative_workers)
        self._face_workers = _worker_count(face_workers)

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
                "sub_event_id": str(batch.sub_event_id),
                "label": batch.label,
                "processing_profile_id": event.processing_profile_id,
                "device_label": event.device_label,
                "assets": [
                    {
                        "id": str(item.id),
                        "filename": item.basename,
                        "content_type": item.content_type,
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
                idempotency_key=operation_key(batch.id, "reserve"),
            )
            self.store.mark_batch_reserved(batch.id)
        elif batch.state not in {BatchState.RESERVED, BatchState.UPLOADING, BatchState.COMPLETE}:
            raise DesktopApiError(
                "batch_not_approved", "Approve the local contribution before uploading."
            )
        # Compatibility gates fail closed before any transfer or processing work.
        event = self.store.get_event(event.id)
        if event.preview_policy is None:
            event = self._refresh_cached_event(event.id)
        if event.preview_policy is None and self._ensure_preview_policy is not None:
            event = self._ensure_preview_policy(event.id)
        if event.preview_policy is None:
            raise DesktopApiError(
                "preview_policy_not_confirmed",
                "Confirm preview settings before gallery processing.",
            )
        event = self._refresh_cached_event(event.id)
        if event.face_model_id != ACCEPTED_FACE_MODEL_CONTRACT.model.id:
            raise DesktopApiError(
                "face_model_mismatch",
                "This desktop version cannot process the event's face model.",
            )
        self.store.ensure_derivative_checkpoints(batch.id)
        self.store.ensure_face_analysis_checkpoints(batch.id)
        engines = self._face_engine_pool(len(items))
        cache_directory = self.store.derivative_cache_directory(batch.id)
        try:
            self._run_pipeline(
                event=event,
                batch_id=batch.id,
                items=items,
                transfer_limit=transfer_limit,
                engines=engines,
                cache_directory=cache_directory,
                on_progress=on_progress,
                on_stage=on_stage,
                is_cancelled=is_cancelled,
            )
        finally:
            self.store.cleanup_derivative_cache(batch.id)

    def _face_engine_pool(self, item_count: int) -> queue.Queue:
        """Create one single-threaded engine per face worker; engines are not thread-safe."""
        try:
            engines = [
                self._face_engine_factory() for _ in range(min(self._face_workers, item_count))
            ]
        except FaceModelSetupError as exc:
            raise DesktopApiError(exc.code, str(exc)) from exc
        pool: queue.Queue = queue.Queue()
        for engine in engines:
            pool.put(engine)
        return pool

    def _run_pipeline(
        self,
        *,
        event: EventCache,
        batch_id: UUID,
        items: dict[UUID, object],
        transfer_limit: int,
        engines: queue.Queue,
        cache_directory: Path,
        on_progress,
        on_stage,
        is_cancelled,
    ) -> None:
        """Run originals, derivatives, and face indexing as overlapped stage runners.

        Derivative and face work for one asset starts as soon as its original is verified,
        while later originals still transfer. A failure in any runner halts the others;
        the originals stage has deterministic error priority, matching the serial order.
        """
        abort = threading.Event()
        upload_done = threading.Event()
        derivative_work = threading.Event()
        face_work = threading.Event()
        errors: dict[str, BaseException] = {}
        errors_lock = threading.Lock()

        def halted() -> bool:
            return abort.is_set() or bool(is_cancelled and is_cancelled())

        def record(stage: str, exc: BaseException) -> None:
            with errors_lock:
                errors.setdefault(stage, exc)
            abort.set()

        def run_originals() -> None:
            try:
                self._upload_originals(
                    event=event,
                    batch_id=batch_id,
                    items=items,
                    transfer_limit=transfer_limit,
                    on_progress=on_progress,
                    on_stage=on_stage,
                    is_cancelled=halted,
                    work_signals=(derivative_work, face_work),
                )
            except Exception as exc:
                record("originals", exc)
            finally:
                upload_done.set()
                derivative_work.set()
                face_work.set()

        def run_derivatives() -> None:
            try:
                self._process_derivatives(
                    event=event,
                    batch_id=batch_id,
                    items=items,
                    cache_directory=cache_directory,
                    upload_done=upload_done,
                    work_available=derivative_work,
                    on_stage=on_stage,
                    is_halted=halted,
                )
            except Exception as exc:
                record("derivatives", exc)

        def run_faces() -> None:
            try:
                self._process_face_index(
                    event=event,
                    batch_id=batch_id,
                    items=items,
                    engines=engines,
                    cache_directory=cache_directory,
                    upload_done=upload_done,
                    work_available=face_work,
                    on_stage=on_stage,
                    is_halted=halted,
                )
            except Exception as exc:
                record("faces", exc)

        runners = [
            threading.Thread(target=run_originals, name="openfotos-originals", daemon=True),
            threading.Thread(target=run_derivatives, name="openfotos-derivatives", daemon=True),
            threading.Thread(target=run_faces, name="openfotos-faces", daemon=True),
        ]
        for runner in runners:
            runner.start()
        for runner in runners:
            runner.join()
        for stage in ("originals", "derivatives", "faces"):
            if stage in errors:
                raise errors[stage]
        if is_cancelled and is_cancelled():
            return
        # Report terminal stage counts in the historic stage order once every runner
        # finished, so observers see a deterministic final progress sequence.
        self._report_derivative_progress(batch_id, on_stage)
        self._report_face_progress(batch_id, on_stage)

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
        work_signals: tuple[threading.Event, ...] = (),
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
                on_stage(AssetVariant.ORIGINAL.value, completed, len(items))
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
                    "One or more originals need an explicit retry or photographer exclusion.",
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
                        work_signals,
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
            for signal in work_signals:
                signal.set()

    def _upload_one(
        self,
        event_id: UUID,
        item,
        lease: dict,
        work_signals: tuple[threading.Event, ...] = (),
    ) -> None:
        self.store.mark_upload_started(item.id)
        try:
            current = item.source_path.stat()
            if (
                current.st_size != item.snapshot.size_bytes
                or current.st_mtime_ns != item.snapshot.modified_ns
                or current.st_ctime_ns != item.snapshot.changed_ns
            ):
                raise SourceChangedError
            result = self._objects.put_file(
                url=lease["url"],
                headers=lease["headers"],
                path=item.source_path,
            )
            if result.status_code != 412 and (
                result.sha256 != item.sha256 or result.content_md5 != item.content_md5
            ):
                raise SourceChangedError
            if result.status_code not in {200, 201, 204, 412}:
                retryable = result.status_code in {408, 429} or result.status_code >= 500
                raise DesktopApiError(
                    "upload_http_error",
                    f"Object storage rejected an upload with HTTP {result.status_code}.",
                    retryable=retryable,
                )
            self._request(
                "POST",
                f"/api/v1/events/{event_id}/assets/{item.id}/complete/",
                json={},
                idempotency_key=operation_key(item.id, "complete-original"),
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
            for signal in work_signals:
                signal.set()

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
        cache_directory: Path,
        upload_done: threading.Event,
        work_available: threading.Event,
        on_stage,
        is_halted,
    ) -> None:
        policy = event.preview_policy
        if policy is None:  # guarded by upload; retained as a narrow type boundary
            raise DesktopApiError(
                "preview_policy_not_confirmed", "Preview settings are unavailable."
            )
        mark_png = self._policy_mark(event.id, policy)
        renderer = DerivativeRenderer()
        synced = True
        while True:
            if is_halted():
                return
            if synced:
                self._sync_batch(event.id, batch_id)
            self._report_derivative_progress(batch_id, on_stage)
            originals = {
                checkpoint.item_id: checkpoint
                for checkpoint in self.store.list_upload_checkpoints(batch_id)
            }
            derivatives: dict[UUID, list] = {}
            for checkpoint in self.store.list_derivative_checkpoints(batch_id):
                derivatives.setdefault(checkpoint.item_id, []).append(checkpoint)
            ready = []
            exhausted = False
            for item in sorted(items.values(), key=lambda value: str(value.id)):
                pending = [
                    checkpoint
                    for checkpoint in derivatives.get(item.id, [])
                    if checkpoint.state
                    not in {LocalUploadState.VERIFIED, LocalUploadState.EXCLUDED}
                ]
                if not pending:
                    continue
                original = originals.get(item.id)
                if original is None or original.state is not LocalUploadState.VERIFIED:
                    continue
                if max(checkpoint.attempt_count for checkpoint in pending) >= (
                    _MAX_UPLOAD_ATTEMPTS
                ):
                    exhausted = True
                    continue
                ready.append((item, pending))
            if ready:
                self._derivative_wave(
                    event=event,
                    ready=ready,
                    policy=policy,
                    mark_png=mark_png,
                    renderer=renderer,
                    cache_directory=cache_directory,
                )
                synced = True
                continue
            if self.store.derivatives_complete(batch_id):
                return
            if upload_done.is_set():
                if exhausted:
                    raise DesktopApiError(
                        "derivative_attempts_exhausted",
                        "A gallery derivative failed five times and needs photographer review.",
                    )
                raise DesktopApiError(
                    "derivative_state_unavailable",
                    "Gallery processing stopped before every derivative was verified.",
                    retryable=True,
                )
            synced = work_available.wait(timeout=_STAGE_WAIT_SECONDS)
            work_available.clear()

    def _derivative_wave(
        self,
        *,
        event: EventCache,
        ready: list,
        policy: PreviewPolicyCache,
        mark_png: bytes,
        renderer: DerivativeRenderer,
        cache_directory: Path,
    ) -> None:
        fatal = None
        with ThreadPoolExecutor(max_workers=min(self._derivative_workers, len(ready))) as executor:
            futures = [
                executor.submit(
                    self._derivative_attempt,
                    event=event,
                    item=item,
                    pending=pending,
                    policy=policy,
                    mark_png=mark_png,
                    renderer=renderer,
                    cache_directory=cache_directory,
                )
                for item, pending in ready
            ]
            for future in as_completed(futures):
                try:
                    future.result()
                except DesktopApiError as exc:
                    if exc.code in _FATAL_DERIVATIVE_CODES and fatal is None:
                        fatal = exc
        if fatal is not None:
            raise fatal

    def _derivative_attempt(
        self,
        *,
        event: EventCache,
        item,
        pending: list,
        policy: PreviewPolicyCache,
        mark_png: bytes,
        renderer: DerivativeRenderer,
        cache_directory: Path,
    ) -> None:
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
            if isinstance(exc, DesktopApiError) and exc.retryable:
                self._derivative_backoff(current)

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
                    "objects": [
                        rendered.preview.as_contract(),
                        rendered.thumbnail.as_contract(),
                    ],
                },
                idempotency_key=operation_key(item.id, "register-derivatives"),
            )
            rendered_by_variant = {
                rendered.preview.variant.value: rendered.preview,
                rendered.thumbnail.variant.value: rendered.thumbnail,
            }
            pending_variants = []
            for value in response["objects"]:
                try:
                    variant = AssetVariant(value["variant"])
                except ValueError as exc:
                    raise DesktopApiError(
                        "invalid_server_response",
                        "The server returned an invalid derivative variant.",
                    ) from exc
                if variant not in DERIVATIVE_VARIANTS:
                    raise DesktopApiError(
                        "invalid_server_response",
                        "The server returned an invalid derivative variant.",
                    )
                if value["state"] == "verified":
                    self.store.mark_derivative_verified(item.id, variant)
                    rendered_by_variant[variant.value].path.unlink(missing_ok=True)
                else:
                    pending_variants.append(variant.value)
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
                try:
                    variant = AssetVariant(lease["variant"])
                except ValueError as exc:
                    raise DesktopApiError(
                        "invalid_server_response",
                        "The server returned an invalid derivative variant.",
                    ) from exc
                if variant not in DERIVATIVE_VARIANTS:
                    raise DesktopApiError(
                        "invalid_server_response",
                        "The server returned an invalid derivative variant.",
                    )
                self._upload_derivative(
                    event_id=event.id,
                    asset_id=item.id,
                    rendered=rendered_by_variant[variant.value],
                    lease=lease,
                )
                self.store.mark_derivative_verified(item.id, variant)
                rendered_by_variant[variant.value].path.unlink(missing_ok=True)
        finally:
            if downloaded:
                source_path.unlink(missing_ok=True)
            if rendered is not None:
                rendered.preview.path.unlink(missing_ok=True)
                rendered.thumbnail.path.unlink(missing_ok=True)

    def _derivative_source(self, event_id: UUID, item, cache_directory: Path) -> tuple[Path, bool]:
        try:
            if item.source_path.is_file() and path_sha256(item.source_path) == item.sha256:
                return item.source_path, False
        except OSError:
            pass
        data = self._request(
            "POST",
            f"/api/v1/events/{event_id}/assets/{item.id}/source-url/",
            json={},
        )
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f"{item.id}-source-",
            suffix=Path(item.basename).suffix.lower(),
            dir=cache_directory,
        )
        os.close(descriptor)
        destination = Path(temporary_name)
        if os.name != "nt":
            destination.chmod(0o600)
        try:
            result = self._objects.download_file(url=data["url"], destination=destination)
            if result.status_code != 200:
                raise DesktopApiError(
                    "original_readback_failed",
                    f"Private original read-back failed with HTTP {result.status_code}.",
                    retryable=result.status_code in {408, 429} or result.status_code >= 500,
                )
            if result.sha256 != item.sha256 or data["sha256"] != item.sha256:
                raise DesktopApiError(
                    "original_readback_checksum_mismatch",
                    "The private original read-back failed checksum validation.",
                )
        except Exception:
            destination.unlink(missing_ok=True)
            raise
        return destination, True

    def _upload_derivative(self, *, event_id: UUID, asset_id: UUID, rendered, lease: dict) -> None:
        try:
            result = self._objects.put_file(
                url=lease["url"],
                headers=lease["headers"],
                path=rendered.path,
            )
        except (httpx.HTTPError, OSError) as exc:
            raise DesktopApiError(
                "derivative_upload_interrupted",
                "A gallery derivative upload was interrupted.",
                retryable=True,
            ) from exc
        if result.status_code != 412 and (
            result.sha256 != rendered.sha256 or result.content_md5 != rendered.content_md5
        ):
            raise DesktopApiError(
                "derivative_cache_changed", "A cached gallery derivative changed before upload."
            )
        if result.status_code not in {200, 201, 204, 412}:
            raise DesktopApiError(
                "derivative_upload_http_error",
                f"Object storage rejected a derivative with HTTP {result.status_code}.",
                retryable=result.status_code in {408, 429} or result.status_code >= 500,
            )
        self._request(
            "POST",
            f"/api/v1/events/{event_id}/assets/{asset_id}/derivatives/"
            f"{rendered.variant.value}/complete/",
            json={},
            idempotency_key=operation_key(asset_id, f"complete-{rendered.variant.value}"),
        )

    def _policy_mark(self, event_id: UUID, policy: PreviewPolicyCache) -> bytes:
        if not policy.enabled:
            return b""
        data = self._request("GET", f"/api/v1/events/{event_id}/preview-policy/mark/")
        try:
            response = self._objects.get(data["url"])
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

    def _process_face_index(
        self,
        *,
        event: EventCache,
        batch_id: UUID,
        items: dict[UUID, object],
        engines: queue.Queue,
        cache_directory: Path,
        upload_done: threading.Event,
        work_available: threading.Event,
        on_stage,
        is_halted,
    ) -> None:
        synced = True
        while True:
            if is_halted():
                return
            if synced:
                self._sync_batch(event.id, batch_id)
            self._report_face_progress(batch_id, on_stage)
            originals = {
                checkpoint.item_id: checkpoint
                for checkpoint in self.store.list_upload_checkpoints(batch_id)
            }
            analyses = {
                checkpoint.item_id: checkpoint
                for checkpoint in self.store.list_face_analysis_checkpoints(batch_id)
            }
            ready = []
            exhausted = False
            for item in sorted(items.values(), key=lambda value: str(value.id)):
                checkpoint = analyses.get(item.id)
                if checkpoint is None or checkpoint.state in {
                    LocalFaceState.INDEXED,
                    LocalFaceState.NO_USABLE_FACE,
                    LocalFaceState.EXCLUDED,
                }:
                    continue
                if checkpoint.state is LocalFaceState.CONFLICT:
                    raise DesktopApiError(
                        "face_analysis_conflict",
                        "A photo needs an explicit face-analysis reset in the dashboard.",
                    )
                original = originals.get(item.id)
                if original is None or original.state is not LocalUploadState.VERIFIED:
                    continue
                if checkpoint.attempt_count >= _MAX_UPLOAD_ATTEMPTS:
                    exhausted = True
                    continue
                ready.append(item)
            if ready:
                self._face_wave(
                    event=event,
                    batch_id=batch_id,
                    items=ready,
                    engines=engines,
                    cache_directory=cache_directory,
                )
                synced = True
                continue
            if self.store.face_analysis_complete(batch_id):
                return
            if upload_done.is_set():
                if exhausted:
                    raise DesktopApiError(
                        "face_analysis_attempts_exhausted",
                        "A photo failed face analysis five times and needs review.",
                    )
                raise DesktopApiError(
                    "face_analysis_state_unavailable",
                    "Face indexing stopped before every photo reached a terminal state.",
                    retryable=True,
                )
            synced = work_available.wait(timeout=_STAGE_WAIT_SECONDS)
            work_available.clear()

    def _face_wave(
        self,
        *,
        event: EventCache,
        batch_id: UUID,
        items: list,
        engines: queue.Queue,
        cache_directory: Path,
    ) -> None:
        fatal = None
        with ThreadPoolExecutor(max_workers=min(self._face_workers, len(items))) as executor:
            futures = [
                executor.submit(
                    self._face_attempt,
                    event=event,
                    batch_id=batch_id,
                    item=item,
                    engines=engines,
                    cache_directory=cache_directory,
                )
                for item in items
            ]
            for future in as_completed(futures):
                try:
                    future.result()
                except DesktopApiError as exc:
                    if exc.code in _FATAL_FACE_CODES and fatal is None:
                        fatal = exc
        if fatal is not None:
            raise fatal

    def _face_attempt(
        self,
        *,
        event: EventCache,
        batch_id: UUID,
        item,
        engines: queue.Queue,
        cache_directory: Path,
    ) -> None:
        engine = engines.get()
        try:
            self._process_asset_faces(
                event=event,
                batch_id=batch_id,
                item=item,
                engine=engine,
                cache_directory=cache_directory,
            )
        except (
            FaceAnalysisDocumentError,
            FaceEngineError,
            DesktopApiError,
            OSError,
        ) as exc:
            code = getattr(exc, "code", "face_engine_failed")
            conflict = code == "face_analysis_conflict"
            self.store.mark_face_analysis_failed(
                item.id,
                code,
                conflict=conflict,
            )
            if isinstance(exc, DesktopApiError) and code in _FATAL_FACE_CODES:
                raise
            checkpoint = self.store.get_face_analysis_checkpoint(item.id)
            self._report_face_failure(
                event.id,
                self.store.get_batch(batch_id).sub_event_id,
                item.id,
                code,
                attempt=checkpoint.attempt_count,
            )
            if isinstance(exc, DesktopApiError) and exc.retryable:
                self._face_backoff(checkpoint.attempt_count)
        finally:
            engines.put(engine)

    def _process_asset_faces(
        self,
        *,
        event: EventCache,
        batch_id: UUID,
        item,
        engine: FaceEngine,
        cache_directory: Path,
    ) -> None:
        self.store.mark_face_analysis_started(item.id)
        source_path, downloaded = self._derivative_source(event.id, item, cache_directory)
        try:
            detected = tuple(engine.detect_and_embed(source_path.read_bytes()))
            document = build_face_analysis_document(
                asset_id=item.id,
                source_sha256=item.sha256,
                detected_faces=detected,
                contract=ACCEPTED_FACE_MODEL_CONTRACT,
                detector_floor=ACCEPTED_SFACE_DETECTOR_FLOOR,
            )
            sub_event_id = self.store.get_batch(batch_id).sub_event_id
            response = self._request(
                "POST",
                f"/api/v1/events/{event.id}/sub-events/{sub_event_id}/assets/"
                f"{item.id}/face-analysis/",
                json=document.as_dict(),
                idempotency_key=operation_key(item.id, f"face-analysis-{document.document_sha256}"),
            )
            try:
                terminal = LocalFaceState(response["state"])
                detected_count = int(response["detected_face_count"])
                usable_count = int(response["usable_face_count"])
            except (KeyError, TypeError, ValueError) as exc:
                raise DesktopApiError(
                    "invalid_server_response",
                    "The server returned an invalid face-analysis state.",
                ) from exc
            if terminal not in {LocalFaceState.INDEXED, LocalFaceState.NO_USABLE_FACE}:
                raise DesktopApiError(
                    "invalid_server_response",
                    "The server returned a nonterminal face-analysis state.",
                )
            self.store.mark_face_analysis_complete(
                item.id,
                state=terminal,
                detected_face_count=detected_count,
                usable_face_count=usable_count,
            )
        finally:
            if downloaded:
                source_path.unlink(missing_ok=True)

    def _report_face_failure(
        self,
        event_id: UUID,
        sub_event_id: UUID,
        item_id: UUID,
        code: str,
        *,
        attempt: int,
    ) -> None:
        stable_code = code if re.fullmatch(r"[a-z0-9_]{1,64}", code) else "face_engine_failed"
        self._request(
            "POST",
            f"/api/v1/events/{event_id}/sub-events/{sub_event_id}/assets/"
            f"{item_id}/face-analysis-failure/",
            json={"code": stable_code},
            idempotency_key=operation_key(item_id, f"face-analysis-failure-{attempt}"),
        )

    def _face_backoff(self, attempts: int) -> None:
        maximum = min(16.0, 2.0 ** max(0, attempts - 1))
        self._sleep(self._jitter(0.0, maximum))

    def _report_face_progress(self, batch_id: UUID, on_stage) -> None:
        if not on_stage:
            return
        checkpoints = self.store.list_face_analysis_checkpoints(batch_id)
        complete = sum(
            checkpoint.state
            in {
                LocalFaceState.INDEXED,
                LocalFaceState.NO_USABLE_FACE,
                LocalFaceState.EXCLUDED,
            }
            for checkpoint in checkpoints
        )
        on_stage("face-index", complete, len(checkpoints))

    def _item_derivative_checkpoints(self, item_id: UUID) -> dict[str, object]:
        return {
            variant.value: self.store.get_derivative_checkpoint(item_id, variant)
            for variant in DERIVATIVE_VARIANTS
        }

    def _report_derivative_failure(
        self, event_id: UUID, item_id: UUID, code: str, *, attempt: int
    ) -> None:
        stable_code = code if re.fullmatch(r"[a-z0-9_]{1,64}", code) else "derivative_failed"
        self._request(
            "POST",
            f"/api/v1/events/{event_id}/assets/{item_id}/derivative-failure/",
            json={"code": stable_code[:64]},
            idempotency_key=operation_key(item_id, f"derivative-failure-{attempt}"),
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
        if response.get("sub_event_id"):
            self.store.update_batch_sub_event(batch_id, UUID(response["sub_event_id"]))
        local = {item.item_id: item for item in self.store.list_upload_checkpoints(batch_id)}
        for item in response.get("assets", []):
            item_id = UUID(item["asset_id"])
            variant = item.get("variant", AssetVariant.ORIGINAL.value)
            if item_id not in local:
                continue
            if variant == AssetVariant.ORIGINAL.value:
                face_analysis = item.get("face_analysis")
                if face_analysis is not None:
                    self.store.sync_face_analysis(
                        item_id,
                        state=LocalFaceState(face_analysis["state"]),
                        attempt_count=int(face_analysis["attempt_count"]),
                        error_code=str(face_analysis["failure_code"]),
                        detected_face_count=int(face_analysis["detected_face_count"]),
                        usable_face_count=int(face_analysis["usable_face_count"]),
                    )
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
                if item.get("gallery_excluded"):
                    self.store.mark_derivatives_excluded(item_id)
                    self.store.mark_face_analysis_excluded(item_id)
            elif variant in {value.value for value in DERIVATIVE_VARIANTS}:
                derivative_variant = AssetVariant(variant)
                if item.get("gallery_excluded"):
                    self.store.mark_derivatives_excluded(item_id)
                    self.store.mark_face_analysis_excluded(item_id)
                elif item["state"] == "verified":
                    self.store.mark_derivative_verified(item_id, derivative_variant)
                elif item["state"] == "failed":
                    self.store.mark_derivative_failed(
                        item_id,
                        derivative_variant,
                        item["failure_code"] or "server_rejected",
                    )
        if response.get("state") == "not_included":
            self.store.mark_batch_not_included(batch_id)


def operation_key(identifier: UUID, operation: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"openfotos:{identifier}:{operation}")
