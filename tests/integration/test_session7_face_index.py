import base64
import hashlib
import json
from datetime import timedelta
from importlib import import_module
from types import SimpleNamespace
from uuid import uuid4

import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from openfotos_contracts import EventState, IngestionManifestState, UploadObjectState
from openfotos_server.events.desktop_auth import authenticate_photographer
from openfotos_server.events.face_services import (
    refresh_face_index_readiness,
    report_face_analysis_failure,
    reset_face_analysis,
    search_face_index,
    submit_face_analysis,
)
from openfotos_server.events.ingestion_services import IngestionError
from openfotos_server.events.models import (
    Asset,
    AssetObject,
    ContributionBatch,
    Event,
    EventInstallation,
    FaceAnalysis,
    FaceAnalysisState,
    FaceEmbedding,
    IngestionManifest,
    Photographer,
    PhotographerMembership,
    SubEvent,
)
from openfotos_vision import (
    ACCEPTED_FACE_MODEL_CONTRACT,
    ACCEPTED_SFACE_DETECTOR_FLOOR,
    DetectedFace,
    build_face_analysis_document,
    normalized_embedding,
)

pytestmark = pytest.mark.django_db


def _context(*, slug: str, username: str):
    photographer = Photographer.objects.create(slug=slug, display_name=slug.title())
    user = get_user_model().objects.create_user(
        username=username,
        password="correct-password",
    )
    PhotographerMembership.objects.create(photographer=photographer, user=user)
    event = Event.objects.create(
        photographer=photographer,
        name="Reception",
        state=EventState.PROCESSING.value,
        expires_at=timezone.now() + timedelta(days=30),
    )
    sub_event = SubEvent.objects.create(event=event, name="Reception", position=1)
    installation_id = uuid4()
    tokens = authenticate_photographer(
        photographer=photographer,
        username=username,
        password="correct-password",
        installation_id=installation_id,
    )
    installation = EventInstallation.objects.create(
        event=event,
        user=user,
        installation_id=installation_id,
        label="Studio workstation",
    )
    return user, event, sub_event, tokens, installation


def _asset(event: Event, sub_event: SubEvent, installation: EventInstallation) -> Asset:
    content = f"synthetic-{uuid4()}".encode()
    batch = ContributionBatch.objects.create(
        id=uuid4(),
        installation=installation,
        sub_event=sub_event,
        intake_generation=event.intake_generation,
        state="complete",
        label="Camera",
        processing_profile_id=event.processing_profile_id,
        declared_asset_count=1,
        declared_original_bytes=len(content),
        manifest_sha256=hashlib.sha256(content + b"manifest").hexdigest(),
    )
    asset = Asset.objects.create(
        id=uuid4(),
        batch=batch,
        original_filename="synthetic.jpg",
        width=100,
        height=80,
        sha256=hashlib.sha256(content).hexdigest(),
    )
    AssetObject.objects.create(
        asset=asset,
        variant="originals",
        object_key=f"events/{event.id}/originals/{asset.id}.jpg",
        expected_bytes=len(content),
        sha256=asset.sha256,
        content_md5=base64.b64encode(hashlib.md5(content, usedforsecurity=False).digest()).decode(),
        width=asset.width,
        height=asset.height,
        state=UploadObjectState.VERIFIED.value,
        verified_at=timezone.now(),
    )
    return asset


def _document(asset: Asset, *, axis: int | None = 0):
    faces = ()
    if axis is not None:
        values = [0.0] * ACCEPTED_FACE_MODEL_CONTRACT.model.embedding_dimensions
        values[axis] = 1.0
        faces = (
            DetectedFace(
                embedding=normalized_embedding(
                    values,
                    model=ACCEPTED_FACE_MODEL_CONTRACT.model,
                ),
                detector_confidence=0.99,
                bounding_box_width=100,
                bounding_box_height=90,
            ),
        )
    return build_face_analysis_document(
        asset_id=asset.id,
        source_sha256=asset.sha256,
        detected_faces=faces,
        contract=ACCEPTED_FACE_MODEL_CONTRACT,
        detector_floor=ACCEPTED_SFACE_DETECTOR_FLOOR,
    )


def _post_analysis(client, *, event, sub_event, asset, token, body):
    return client.post(
        reverse(
            "desktop-api:face-analysis",
            args=(event.id, sub_event.id, asset.id),
        ),
        data=json.dumps(body),
        content_type="application/json",
        headers={
            "host": f"{event.photographer.slug}.localhost",
            "authorization": f"Bearer {token}",
            "idempotency-key": str(uuid4()),
        },
    )


def _attach_manifest(event: Event, *, asset_count: int) -> None:
    manifest = IngestionManifest.objects.create(
        event=event,
        generation=event.intake_generation,
        state=IngestionManifestState.COMMITTED.value,
        object_key=f"events/{event.id}/manifests/generation-000001.json",
        content_sha256="a" * 64,
        document={},
        asset_count=asset_count,
        original_bytes=asset_count,
    )
    event.current_ingestion_manifest = manifest
    event.save(update_fields=("current_ingestion_manifest",))


def test_face_index_migration_refuses_existing_published_events() -> None:
    _user, event, _sub_event, _tokens, _installation = _context(
        slug="alpha", username="alpha-photographer"
    )
    event.state = EventState.PUBLISHED.value
    event.save(update_fields=("state",))
    migration = import_module("openfotos_server.events.migrations.0005_face_index")

    with pytest.raises(RuntimeError, match="cannot migrate a published event"):
        migration.reject_published_events(
            import_module("django.apps").apps,
            SimpleNamespace(connection=connection),
        )


def test_cross_sub_event_face_upload_writes_no_analysis_or_vector() -> None:
    _user, event, sub_event, tokens, installation = _context(
        slug="alpha", username="alpha-photographer"
    )
    asset = _asset(event, sub_event, installation)
    other_sub_event = SubEvent.objects.create(event=event, name="Haldi", position=2)

    response = _post_analysis(
        Client(),
        event=event,
        sub_event=other_sub_event,
        asset=asset,
        token=tokens.access_token,
        body=_document(asset).as_dict(),
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "asset_not_found"
    assert not FaceAnalysis.objects.filter(asset=asset).exists()
    assert not FaceEmbedding.objects.filter(analysis_id=asset.id).exists()


def test_malformed_model_and_vector_fail_before_database_write() -> None:
    _user, event, sub_event, tokens, installation = _context(
        slug="alpha", username="alpha-photographer"
    )
    asset = _asset(event, sub_event, installation)
    valid = _document(asset).as_dict()
    wrong_model = json.loads(json.dumps(valid))
    wrong_model["model_contract"]["model_id"] = "unaccepted-model"
    malformed_vector = json.loads(json.dumps(valid))
    malformed_vector["faces"][0]["values"] = malformed_vector["faces"][0]["values"][:-1]

    model_response = _post_analysis(
        Client(),
        event=event,
        sub_event=sub_event,
        asset=asset,
        token=tokens.access_token,
        body=wrong_model,
    )
    vector_response = _post_analysis(
        Client(),
        event=event,
        sub_event=sub_event,
        asset=asset,
        token=tokens.access_token,
        body=malformed_vector,
    )

    assert model_response.status_code == 400
    assert model_response.json()["error"]["code"] == "model_contract_mismatch"
    assert vector_response.status_code == 400
    assert vector_response.json()["error"]["code"] == "invalid_embedding"
    assert not FaceAnalysis.objects.filter(asset=asset).exists()
    assert FaceEmbedding.objects.count() == 0


def test_retry_is_idempotent_and_changed_result_requires_audited_reset() -> None:
    user, event, sub_event, tokens, installation = _context(
        slug="alpha", username="alpha-photographer"
    )
    asset = _asset(event, sub_event, installation)
    _attach_manifest(event, asset_count=1)
    first_document = _document(asset, axis=0)

    first = submit_face_analysis(
        session=tokens.session,
        event_id=event.id,
        sub_event_id=sub_event.id,
        document=first_document,
    )
    replay = submit_face_analysis(
        session=tokens.session,
        event_id=event.id,
        sub_event_id=sub_event.id,
        document=first_document,
    )

    assert first.state == FaceAnalysisState.INDEXED
    assert replay.document_sha256 == first.document_sha256
    assert FaceEmbedding.objects.filter(analysis=first).count() == 1
    assert FaceAnalysis.objects.get(asset=asset).attempt_count == 1
    event.face_index_ready_generation = None
    event.save(update_fields=("face_index_ready_generation",))
    submit_face_analysis(
        session=tokens.session,
        event_id=event.id,
        sub_event_id=sub_event.id,
        document=first_document,
    )
    event.refresh_from_db()
    assert event.face_index_ready_generation == event.intake_generation

    with pytest.raises(IngestionError) as conflict:
        submit_face_analysis(
            session=tokens.session,
            event_id=event.id,
            sub_event_id=sub_event.id,
            document=_document(asset, axis=1),
        )
    assert conflict.value.code == "face_analysis_conflict"
    stored = FaceAnalysis.objects.get(asset=asset)
    assert stored.state == FaceAnalysisState.CONFLICT
    assert FaceEmbedding.objects.filter(analysis=stored).count() == 1
    with pytest.raises(IngestionError) as failure_override:
        report_face_analysis_failure(
            session=tokens.session,
            event_id=event.id,
            sub_event_id=sub_event.id,
            asset_id=asset.id,
            code="face_engine_failed",
        )
    assert failure_override.value.code == "face_analysis_conflict"

    reset = reset_face_analysis(event=event, asset_id=asset.id, actor=user)
    assert reset.state == FaceAnalysisState.PENDING
    assert reset.attempt_count == 0
    assert not FaceEmbedding.objects.filter(analysis=reset).exists()


def test_five_technical_failures_require_an_explicit_reset() -> None:
    user, event, sub_event, tokens, installation = _context(
        slug="alpha", username="alpha-photographer"
    )
    asset = _asset(event, sub_event, installation)

    for expected_attempt in range(1, 6):
        analysis = report_face_analysis_failure(
            session=tokens.session,
            event_id=event.id,
            sub_event_id=sub_event.id,
            asset_id=asset.id,
            code="face_engine_failed",
        )
        assert analysis.attempt_count == expected_attempt

    unchanged = report_face_analysis_failure(
        session=tokens.session,
        event_id=event.id,
        sub_event_id=sub_event.id,
        asset_id=asset.id,
        code="face_engine_failed",
    )
    assert unchanged.attempt_count == 5
    with pytest.raises(IngestionError) as exhausted:
        submit_face_analysis(
            session=tokens.session,
            event_id=event.id,
            sub_event_id=sub_event.id,
            document=_document(asset, axis=None),
        )
    assert exhausted.value.code == "face_analysis_attempts_exhausted"

    reset_face_analysis(event=event, asset_id=asset.id, actor=user)
    completed = submit_face_analysis(
        session=tokens.session,
        event_id=event.id,
        sub_event_id=sub_event.id,
        document=_document(asset, axis=None),
    )
    assert completed.state == FaceAnalysisState.NO_USABLE_FACE


def test_only_the_originating_active_workstation_can_process_an_asset() -> None:
    _user, event, sub_event, _tokens, installation = _context(
        slug="alpha", username="alpha-photographer"
    )
    asset = _asset(event, sub_event, installation)
    other_tokens = authenticate_photographer(
        photographer=event.photographer,
        username="alpha-photographer",
        password="correct-password",
        installation_id=uuid4(),
    )

    with pytest.raises(IngestionError) as denied:
        submit_face_analysis(
            session=other_tokens.session,
            event_id=event.id,
            sub_event_id=sub_event.id,
            document=_document(asset),
        )

    assert denied.value.code == "asset_not_found"
    assert not FaceAnalysis.objects.filter(asset=asset).exists()


def test_visible_terminal_analysis_marks_current_generation_ready() -> None:
    _user, event, sub_event, tokens, installation = _context(
        slug="alpha", username="alpha-photographer"
    )
    visible_asset = _asset(event, sub_event, installation)
    archived_sub_event = SubEvent.objects.create(
        event=event,
        name="Archived",
        position=2,
        is_archived=True,
    )
    _asset(event, archived_sub_event, installation)
    _attach_manifest(event, asset_count=2)

    analysis = submit_face_analysis(
        session=tokens.session,
        event_id=event.id,
        sub_event_id=sub_event.id,
        document=_document(visible_asset, axis=None),
    )

    event.refresh_from_db()
    assert analysis.state == FaceAnalysisState.NO_USABLE_FACE
    assert event.face_index_ready_generation == event.intake_generation
    assert refresh_face_index_readiness(event.id)


@pytest.mark.skipif(
    connection.vendor != "postgresql",
    reason="pgvector cosine queries require PostgreSQL",
)
def test_pgvector_search_is_exactly_event_and_sub_event_scoped() -> None:
    user, event, reception, tokens, installation = _context(
        slug="alpha", username="alpha-photographer"
    )
    haldi = SubEvent.objects.create(event=event, name="Haldi", position=2)
    reception_asset = _asset(event, reception, installation)
    haldi_asset = _asset(event, haldi, installation)
    submit_face_analysis(
        session=tokens.session,
        event_id=event.id,
        sub_event_id=reception.id,
        document=_document(reception_asset, axis=0),
    )
    submit_face_analysis(
        session=tokens.session,
        event_id=event.id,
        sub_event_id=haldi.id,
        document=_document(haldi_asset, axis=0),
    )

    _other_user, other_event, other_sub_event, other_tokens, other_installation = _context(
        slug="beta", username="beta-photographer"
    )
    other_asset = _asset(other_event, other_sub_event, other_installation)
    submit_face_analysis(
        session=other_tokens.session,
        event_id=other_event.id,
        sub_event_id=other_sub_event.id,
        document=_document(other_asset, axis=0),
    )
    query_embedding = normalized_embedding(
        [1.0] + [0.0] * 127,
        model=ACCEPTED_FACE_MODEL_CONTRACT.model,
    )

    scoped = search_face_index(
        event_id=event.id,
        sub_event_id=reception.id,
        embedding=query_embedding,
    )
    assert [result.asset_id for result in scoped] == [reception_asset.id]

    haldi.is_archived = True
    haldi.save(update_fields=("is_archived",))
    all_active = search_face_index(event_id=event.id, embedding=query_embedding)
    assert [result.asset_id for result in all_active] == [reception_asset.id]
    assert other_asset.id not in {result.asset_id for result in all_active}

    reception_asset.gallery_excluded_at = timezone.now()
    reception_asset.gallery_excluded_by = user
    reception_asset.gallery_exclusion_reason = "Intentional test exclusion"
    reception_asset.save(
        update_fields=(
            "gallery_excluded_at",
            "gallery_excluded_by",
            "gallery_exclusion_reason",
        )
    )
    assert search_face_index(event_id=event.id, embedding=query_embedding) == []
