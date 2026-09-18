import base64
import hashlib
from datetime import UTC, datetime, timedelta
from io import BytesIO
from uuid import uuid4

import pytest
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import Client, RequestFactory
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from openfotos_contracts import EventState, UploadObjectState
from openfotos_server.events import share_views
from openfotos_server.events.cookies import access_cookie_name
from openfotos_server.events.desktop_auth import authenticate_photographer
from openfotos_server.events.face_search_services import FaceSearchError, create_face_search
from openfotos_server.events.face_services import FaceSearchResult, submit_face_analysis
from openfotos_server.events.models import (
    Asset,
    AssetObject,
    AuditAction,
    ContributionBatch,
    Event,
    EventInstallation,
    FaceSearchResultSet,
    Photographer,
    PhotographerMembership,
    RateLimitPurpose,
    SubEvent,
)
from openfotos_server.events.rate_limits import consume_attempt
from openfotos_server.events.sharing_services import (
    ShareAccessError,
    create_guest_capability,
    issue_owner_capability,
    revoke_owner_capability,
)
from openfotos_storage import PresignedGet
from openfotos_vision import (
    ACCEPTED_FACE_MODEL_CONTRACT,
    ACCEPTED_SFACE_DETECTOR_FLOOR,
    DetectedFace,
    build_face_analysis_document,
    normalized_embedding,
)

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def configure_test_static_files(settings):
    settings.STORAGES = {
        **settings.STORAGES,
        "staticfiles": {
            "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
        },
    }
    settings.MIDDLEWARE = [
        middleware
        for middleware in settings.MIDDLEWARE
        if middleware != "whitenoise.middleware.WhiteNoiseMiddleware"
    ]


class TrackingStore:
    def __init__(self):
        self.requests = []

    def presign_get(self, *, key, expires_in_seconds, content_disposition="inline"):
        self.requests.append((key, content_disposition))
        return PresignedGet(
            url=f"https://storage.invalid/read/{len(self.requests)}?ttl={expires_in_seconds}",
            expires_at=datetime.now(UTC) + timedelta(seconds=expires_in_seconds),
        )


class FakeEngine:
    def __init__(self, faces):
        self.faces = tuple(faces)

    def detect_and_embed(self, image_bytes):
        assert image_bytes.startswith(b"\xff\xd8")
        return self.faces


def _jpeg_bytes() -> bytes:
    output = BytesIO()
    Image.new("RGB", (120, 120), "#64808a").save(output, format="JPEG")
    return output.getvalue()


def _embedding(axis=0):
    values = [0.0] * ACCEPTED_FACE_MODEL_CONTRACT.model.embedding_dimensions
    values[axis] = 1.0
    return normalized_embedding(values, model=ACCEPTED_FACE_MODEL_CONTRACT.model)


def _face(axis=0, *, confidence=0.99, size=80):
    return DetectedFace(
        embedding=_embedding(axis),
        detector_confidence=confidence,
        bounding_box_width=size,
        bounding_box_height=size,
    )


def _event_context(*, slug="alpha", expires_in_days=500):
    photographer = Photographer.objects.create(slug=slug, display_name=f"{slug.title()} Photos")
    user = get_user_model().objects.create_user(
        username=f"{slug}-photographer",
        password="correct-password",
    )
    PhotographerMembership.objects.create(photographer=photographer, user=user)
    event = Event.objects.create(
        photographer=photographer,
        name="Private Wedding",
        state=EventState.PUBLISHED.value,
        expires_at=timezone.now() + timedelta(days=expires_in_days),
    )
    reception = SubEvent.objects.create(event=event, name="Reception", position=1)
    haldi = SubEvent.objects.create(event=event, name="Haldi", position=2)
    return user, event, reception, haldi


def _unlock(client, capability, *, secret, pin, kind, host="alpha.localhost"):
    client.post(
        reverse(f"events:{kind}-present", args=(capability.id,)),
        {"secret": secret},
        headers={"host": host},
    )
    response = client.post(
        reverse(f"events:{kind}-unlock", args=(capability.id,)),
        {"pin": pin},
        headers={"host": host},
    )
    assert response.status_code == 302
    assert access_cookie_name(capability) in response.cookies


def _asset(event, sub_event, installation, *, position):
    content = f"synthetic-{uuid4()}".encode()
    batch = ContributionBatch.objects.create(
        id=uuid4(),
        installation=installation,
        sub_event=sub_event,
        intake_generation=event.intake_generation,
        state="complete",
        label=sub_event.name,
        processing_profile_id=event.processing_profile_id,
        declared_asset_count=1,
        declared_original_bytes=len(content),
        manifest_sha256=hashlib.sha256(content + b"manifest").hexdigest(),
    )
    asset = Asset.objects.create(
        id=uuid4(),
        batch=batch,
        original_filename="private-client-name.jpg",
        width=800,
        height=600,
        sha256=hashlib.sha256(content).hexdigest(),
        gallery_position=position,
    )
    for variant, width, height in (
        ("originals", 800, 600),
        ("previews", 800, 600),
        ("thumbnails", 512, 384),
    ):
        AssetObject.objects.create(
            asset=asset,
            variant=variant,
            object_key=f"events/{event.id}/{variant}/{asset.id}.jpg",
            expected_bytes=len(content),
            sha256=asset.sha256,
            content_md5=base64.b64encode(
                hashlib.md5(content, usedforsecurity=False).digest()
            ).decode(),
            width=width,
            height=height,
            state=UploadObjectState.VERIFIED.value,
            verified_at=timezone.now(),
        )
    return asset


def test_capabilities_have_one_year_expiry_limits_and_owner_revocation_cascades(settings) -> None:
    user, event, reception, _haldi = _event_context()
    before = timezone.now()
    owner_issued = issue_owner_capability(event=event, actor=user)
    owner = owner_issued.capability

    assert (
        timedelta(days=364, hours=23) < owner.expires_at - before <= timedelta(days=365, seconds=1)
    )
    assert owner.check_pin(owner_issued.pin)
    assert owner_issued.secret not in owner.secret_digest

    guest_issued = create_guest_capability(
        owner=owner,
        label="Bride's family",
        sub_event_id=reception.id,
    )
    guest = guest_issued.capability
    assert guest.expires_at <= owner.expires_at
    assert guest.check_pin(guest_issued.pin)
    audit = event.audit_events.get(action=AuditAction.GUEST_CAPABILITY_CREATED)
    assert "Bride's family" not in str(audit.metadata)

    FaceSearchResultSet.objects.create(
        guest_capability=guest,
        event=event,
        sub_event=reception,
        ordered_asset_ids=[],
        expires_at=timezone.now() + timedelta(hours=1),
    )
    revoke_owner_capability(event=event, actor=user)
    owner.refresh_from_db()
    guest.refresh_from_db()
    assert owner.revoked_at is not None
    assert guest.revoked_at is not None
    assert not FaceSearchResultSet.objects.exists()

    settings.MAX_ACTIVE_GUEST_CAPABILITIES = 1
    replacement = issue_owner_capability(event=event, actor=user).capability
    create_guest_capability(owner=replacement, label="Family", sub_event_id=None)
    with pytest.raises(ShareAccessError, match="Revoke a guest link"):
        create_guest_capability(owner=replacement, label="Friends", sub_event_id=None)


def test_reference_processing_keeps_only_short_lived_ordered_asset_ids(monkeypatch) -> None:
    user, event, reception, _haldi = _event_context()
    owner = issue_owner_capability(event=event, actor=user).capability
    expected_asset_id = uuid4()
    monkeypatch.setattr(
        "openfotos_server.events.face_search_services.search_face_index",
        lambda **_kwargs: [FaceSearchResult(asset_id=expected_asset_id, distance=0.1)],
    )
    raw = _jpeg_bytes()
    upload = SimpleUploadedFile("private-name.jpg", raw, content_type="image/jpeg")

    result = create_face_search(
        capability=owner,
        sub_event=reception,
        uploaded_photo=upload,
        engine=FakeEngine((_face(),)),
    )

    assert result.ordered_asset_ids == [str(expected_asset_id)]
    assert timedelta(minutes=59) < result.expires_at - timezone.now() <= timedelta(hours=1)
    persisted = str(result.__dict__)
    assert "private-name.jpg" not in persisted
    assert raw.hex() not in persisted
    assert str(list(_embedding().values)) not in persisted
    assert upload.closed

    for faces, expected_code in (
        ((), "no_usable_face"),
        ((_face(), _face(1)), "multiple_usable_faces"),
    ):
        rejected = SimpleUploadedFile("rejected.jpg", raw, content_type="image/jpeg")
        with pytest.raises(FaceSearchError) as error:
            create_face_search(
                capability=owner,
                sub_event=None,
                uploaded_photo=rejected,
                engine=FakeEngine(faces),
            )
        assert error.value.code == expected_code
        assert rejected.closed
    assert FaceSearchResultSet.objects.count() == 1


def test_expired_search_records_are_purged_without_removing_active_results() -> None:
    user, event, _reception, _haldi = _event_context()
    owner = issue_owner_capability(event=event, actor=user).capability
    expired = FaceSearchResultSet.objects.create(
        owner_capability=owner,
        event=event,
        ordered_asset_ids=[],
        expires_at=timezone.now() - timedelta(seconds=1),
    )
    active = FaceSearchResultSet.objects.create(
        owner_capability=owner,
        event=event,
        ordered_asset_ids=[],
        expires_at=timezone.now() + timedelta(hours=1),
    )

    call_command("purge_expired_face_searches")
    call_command("purge_expired_face_searches")

    assert not FaceSearchResultSet.objects.filter(pk=expired.pk).exists()
    assert FaceSearchResultSet.objects.filter(pk=active.pk).exists()


def test_face_search_limits_are_browser_link_scoped_and_capability_wide() -> None:
    request = RequestFactory().post("/share/search/", REMOTE_ADDR="203.0.113.10")
    subject = str(uuid4())

    for _ in range(10):
        assert not consume_attempt(
            purpose=RateLimitPurpose.FACE_SEARCH_CLIENT,
            subject=subject,
            request=request,
            client_identifier="first-browser-access-cookie",
        ).limited
    assert consume_attempt(
        purpose=RateLimitPurpose.FACE_SEARCH_CLIENT,
        subject=subject,
        request=request,
        client_identifier="first-browser-access-cookie",
    ).limited
    assert not consume_attempt(
        purpose=RateLimitPurpose.FACE_SEARCH_CLIENT,
        subject=subject,
        request=request,
        client_identifier="second-browser-access-cookie",
    ).limited

    for _ in range(100):
        assert not consume_attempt(
            purpose=RateLimitPurpose.FACE_SEARCH_CAPABILITY,
            subject=subject,
            request=request,
            client_scoped=False,
        ).limited
    assert consume_attempt(
        purpose=RateLimitPurpose.FACE_SEARCH_CAPABILITY,
        subject=subject,
        request=request,
        client_scoped=False,
    ).limited


def test_emergency_share_reset_requires_confirmation_and_revokes_every_link() -> None:
    user, event, _reception, _haldi = _event_context()
    owner = issue_owner_capability(event=event, actor=user).capability
    guest = create_guest_capability(owner=owner, label="Family", sub_event_id=None).capability

    with pytest.raises(CommandError, match="RESET-ALL-SHARE-ACCESS"):
        call_command("reset_share_access", confirm="wrong")
    owner.refresh_from_db()
    guest.refresh_from_db()
    assert owner.revoked_at is None
    assert guest.revoked_at is None

    call_command("reset_share_access", confirm="RESET-ALL-SHARE-ACCESS")
    owner.refresh_from_db()
    guest.refresh_from_db()
    assert owner.revoked_at is not None
    assert guest.revoked_at is not None
    assert event.audit_events.filter(action=AuditAction.SHARE_ACCESS_RESET).exists()


@pytest.mark.skipif(
    connection.vendor != "postgresql",
    reason="public face search requires the PostgreSQL pgvector query boundary",
)
def test_sub_event_guest_search_and_download_never_sign_a_sibling_photo(monkeypatch) -> None:
    photographer = Photographer.objects.create(slug="alpha", display_name="Alpha Photos")
    user = get_user_model().objects.create_user(
        username="alpha-photographer", password="correct-password"
    )
    PhotographerMembership.objects.create(photographer=photographer, user=user)
    event = Event.objects.create(
        photographer=photographer,
        name="Private Wedding",
        state=EventState.PROCESSING.value,
        expires_at=timezone.now() + timedelta(days=500),
    )
    reception = SubEvent.objects.create(event=event, name="Reception", position=1)
    haldi = SubEvent.objects.create(event=event, name="Haldi", position=2)
    installation_id = uuid4()
    tokens = authenticate_photographer(
        photographer=photographer,
        username="alpha-photographer",
        password="correct-password",
        installation_id=installation_id,
    )
    installation = EventInstallation.objects.create(
        event=event,
        user=user,
        installation_id=installation_id,
        label="Studio workstation",
    )
    reception_asset = _asset(event, reception, installation, position=1)
    haldi_asset = _asset(event, haldi, installation, position=2)
    for asset in (reception_asset, haldi_asset):
        document = build_face_analysis_document(
            asset_id=asset.id,
            source_sha256=asset.sha256,
            detected_faces=(_face(),),
            contract=ACCEPTED_FACE_MODEL_CONTRACT,
            detector_floor=ACCEPTED_SFACE_DETECTOR_FLOOR,
        )
        submit_face_analysis(
            session=tokens.session,
            event_id=event.id,
            sub_event_id=asset.batch.sub_event_id,
            document=document,
        )
    event.state = EventState.PUBLISHED.value
    event.save(update_fields=("state",))
    owner = issue_owner_capability(event=event, actor=user).capability
    guest_issued = create_guest_capability(
        owner=owner,
        label="Reception family",
        sub_event_id=reception.id,
    )
    guest = guest_issued.capability
    client = Client()
    _unlock(
        client,
        guest,
        secret=guest_issued.secret,
        pin=guest_issued.pin,
        kind="guest",
    )
    store = TrackingStore()
    monkeypatch.setattr(share_views, "configured_object_store", lambda: store)
    monkeypatch.setattr(
        share_views,
        "configured_face_search_engine",
        lambda: FakeEngine((_face(),)),
    )

    search = client.post(
        reverse("events:guest-search", args=(guest.id,)),
        {
            "reference_photo": SimpleUploadedFile(
                "reference.jpg", _jpeg_bytes(), content_type="image/jpeg"
            ),
            "consent": "on",
        },
        headers={"host": "alpha.localhost"},
    )
    assert search.status_code == 302
    result_set = FaceSearchResultSet.objects.get(guest_capability=guest)
    assert result_set.ordered_asset_ids == [str(reception_asset.id)]
    assert str(haldi_asset.id) not in result_set.ordered_asset_ids

    results = client.get(search.url, headers={"host": "alpha.localhost"})
    assert results.status_code == 200
    assert all(str(haldi_asset.id) not in key for key, _disposition in store.requests)

    before = list(store.requests)
    sibling_photo = client.get(
        reverse("events:guest-photo", args=(guest.id, haldi_asset.id)),
        headers={"host": "alpha.localhost"},
    )
    sibling_download = client.get(
        reverse("events:guest-download", args=(guest.id, haldi_asset.id)),
        headers={"host": "alpha.localhost"},
    )
    assert sibling_photo.status_code == 404
    assert sibling_download.status_code == 404
    assert store.requests == before

    valid_download = client.get(
        reverse("events:guest-download", args=(guest.id, reception_asset.id)),
        headers={"host": "alpha.localhost"},
    )
    assert valid_download.status_code == 302
    assert store.requests[-1][1].startswith('attachment; filename="photo-')
    assert event.audit_events.filter(action=AuditAction.ORIGINAL_DOWNLOAD_ISSUED).exists()
