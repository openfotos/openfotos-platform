import base64
import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse
from django.utils import timezone

import openfotos_server.events
from openfotos_contracts import EventState, IngestionManifestState, UploadObjectState
from openfotos_server.events import share_views, views
from openfotos_server.events.models import (
    Asset,
    AssetObject,
    ConsentAttestation,
    ContributionBatch,
    Event,
    EventInstallation,
    FaceSearchResultSet,
    IngestionManifest,
    Photographer,
    PhotographerMembership,
    PortalCapability,
    PreviewPolicy,
    SubEvent,
)
from openfotos_storage import PresignedGet

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


class SigningStore:
    def presign_get(self, *, key, expires_in_seconds, content_disposition="inline"):
        del content_disposition
        return PresignedGet(
            url=f"https://storage.invalid/{key}?expires={expires_in_seconds}",
            expires_at=datetime.now(UTC) + timedelta(seconds=expires_in_seconds),
        )


def _md5(content: bytes) -> str:
    return base64.b64encode(hashlib.md5(content, usedforsecurity=False).digest()).decode()


def _published_event() -> tuple[Event, ContributionBatch, PortalCapability]:
    now = timezone.now()
    photographer = Photographer.objects.create(slug="alpha", display_name="Ballads of Love")
    user = get_user_model().objects.create_user(username="studio", password="correct-password")
    PhotographerMembership.objects.create(photographer=photographer, user=user)
    event = Event.objects.create(
        photographer=photographer,
        name="Mallur House Warming",
        slug="mallur-house-warming",
        state=EventState.PUBLISHED.value,
        intake_state="closed",
        cover_object_key=f"events/{uuid4()}/portfolio/cover.jpg",
        cover_sha256="a" * 64,
        cover_width=1200,
        cover_height=800,
        expires_at=now + timedelta(days=365),
        first_published_at=now,
        purge_after=now + timedelta(days=395),
    )
    ConsentAttestation.objects.create(
        event=event,
        actor=user,
        notice_version="portfolio-face-index-consent-v1",
    )
    installation = EventInstallation.objects.create(
        event=event,
        user=user,
        installation_id=uuid4(),
        label="Studio workstation",
    )
    sub_event = SubEvent.objects.create(event=event, name="Haldi", position=1)
    batch = ContributionBatch.objects.create(
        id=uuid4(),
        installation=installation,
        sub_event=sub_event,
        intake_generation=1,
        state="complete",
        label="Haldi",
        processing_profile_id="pilot-profile-v1",
        declared_asset_count=1,
        declared_original_bytes=100,
        manifest_sha256="0" * 64,
    )
    manifest = IngestionManifest.objects.create(
        event=event,
        generation=1,
        state=IngestionManifestState.COMMITTED.value,
        object_key=f"events/{event.id}/manifests/generation-000001.json",
        content_sha256="1" * 64,
        document={},
        asset_count=1,
        original_bytes=100,
        committed_at=now,
    )
    event.current_ingestion_manifest = manifest
    PreviewPolicy.objects.create(event=event, enabled=False, confirmed_by=user)
    event.derivatives_ready_generation = 1
    event.face_index_ready_generation = 1
    event.save(
        update_fields=(
            "current_ingestion_manifest",
            "derivatives_ready_generation",
            "face_index_ready_generation",
        )
    )
    portal = PortalCapability(event=event, expires_at=event.expires_at)
    portal.set_pin("0427")
    portal.save()
    return event, batch, portal


def _gallery_asset(event: Event, batch: ContributionBatch, *, position: int = 1) -> Asset:
    content = f"synthetic-gallery-bytes-{position}".encode()
    asset = Asset.objects.create(
        id=uuid4(),
        batch=batch,
        original_filename=f"private-source-name-{position}.jpg",
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
            sha256=hashlib.sha256(content).hexdigest(),
            content_md5=_md5(content),
            width=width,
            height=height,
            state=UploadObjectState.VERIFIED.value,
            verified_at=timezone.now(),
        )
    return asset


def _unlocked_client(event: Event, portal: PortalCapability) -> Client:
    client = Client()
    response = client.post(
        reverse("events:portal-unlock", args=(event.slug,)),
        {"pin": "0427"},
        headers={"host": "alpha.localhost"},
    )
    assert response.status_code == 302
    assert response.cookies
    return client


def test_cover_page_is_public_and_the_unlock_page_holds_the_pin_form(monkeypatch) -> None:
    event, _batch, _portal = _published_event()
    store = SigningStore()
    monkeypatch.setattr(share_views, "configured_object_store", lambda: store)
    client = Client()
    cover_url = reverse("events:portal-gallery", args=(event.slug,))

    cover = client.get(cover_url, headers={"host": "alpha.localhost"})

    assert cover.status_code == 200
    assert b"View gallery" in cover.content
    assert b"Mallur House Warming" in cover.content
    assert b"Ballads of Love" in cover.content
    assert b"Private gallery" in cover.content
    assert event.cover_object_key.encode() in cover.content
    assert b"Enter the four-digit PIN" not in cover.content
    assert b"gallery-grid" not in cover.content
    assert cover.headers["X-Robots-Tag"] == "noindex, nofollow, noarchive"

    unlock = client.get(
        reverse("events:portal-unlock", args=(event.slug,)),
        headers={"host": "alpha.localhost"},
    )

    assert unlock.status_code == 200
    assert b"Enter the four-digit PIN" in unlock.content
    assert b'class="pin-input"' in unlock.content

    with_access = _unlocked_client(event, _portal)
    assert (
        with_access.get(
            reverse("events:portal-unlock", args=(event.slug,)),
            headers={"host": "alpha.localhost"},
        ).status_code
        == 302
    )


def test_gallery_keeps_face_search_behind_a_button_and_one_aligned_grid(monkeypatch) -> None:
    event, batch, portal = _published_event()
    asset = _gallery_asset(event, batch)
    monkeypatch.setattr(share_views, "configured_object_store", SigningStore)
    client = _unlocked_client(event, portal)

    gallery = client.get(
        reverse("events:portal-gallery", args=(event.slug,)),
        headers={"host": "alpha.localhost"},
    )

    assert gallery.status_code == 200
    assert b"Search with your face" in gallery.content
    # The reference-photo form is disclosed by a details/summary action, not always visible.
    assert b'<details class="gallery-search">' in gallery.content
    assert b'<details class="gallery-search" open>' not in gallery.content
    assert b'<div class="gallery-grid">' in gallery.content
    assert b'class="gallery-tile"' in gallery.content
    # One shared grid container; the old results view double-wrapped it inside a card.
    assert gallery.content.count(b'<div class="gallery-grid">') == 1

    result_set = FaceSearchResultSet.objects.create(
        portal_capability=portal,
        event=event,
        ordered_asset_ids=[str(asset.id)],
        expires_at=timezone.now() + timedelta(hours=1),
    )
    results = client.get(
        reverse("events:portal-search-results", args=(event.slug, result_set.id)),
        headers={"host": "alpha.localhost"},
    )

    assert results.status_code == 200
    assert b"Clear search" in results.content
    assert b'<div class="gallery-grid">' in results.content
    assert b'class="gallery-tile"' in results.content
    assert results.content.count(b'<div class="gallery-grid">') == 1
    assert b'<details class="gallery-search" open>' not in results.content


def test_photo_page_uses_gallery_chrome_and_exact_download(monkeypatch) -> None:
    event, batch, portal = _published_event()
    asset = _gallery_asset(event, batch, position=1)
    _gallery_asset(event, batch, position=2)
    monkeypatch.setattr(share_views, "configured_object_store", SigningStore)
    client = _unlocked_client(event, portal)

    response = client.get(
        reverse("events:portal-photo", args=(event.slug, asset.id)),
        headers={"host": "alpha.localhost"},
    )

    assert response.status_code == 200
    assert b"photo-arrow" in response.content
    assert b"arrow-right.svg" in response.content
    assert b"photo-counter" in response.content
    assert b"Download original" in response.content
    assert b"https://storage.invalid/events/" in response.content


def test_gallery_and_portfolio_css_pin_thumbnail_alignment() -> None:
    static_root = Path(openfotos_server.events.__file__).parent / "static" / "openfotos_events"
    css = (static_root / "app.css").read_text(encoding="utf-8")

    portfolio_block = css.split(".portfolio-card-media img {")[1].split("}")[0]
    assert "height: auto;" in portfolio_block
    assert "aspect-ratio: 3 / 2;" in portfolio_block
    tile_block = css.split(".gallery-tile img {")[1].split("}")[0]
    assert "height: auto;" in tile_block
    assert "width: 100%;" in tile_block


def test_public_pages_use_studio_branding_and_keep_the_agpl_footer(monkeypatch) -> None:
    event, _batch, portal = _published_event()
    store = SigningStore()
    monkeypatch.setattr(share_views, "configured_object_store", lambda: store)
    monkeypatch.setattr(views, "configured_object_store", lambda: store)
    client = Client()

    pages = [
        client.get(reverse("events:portfolio"), headers={"host": "alpha.localhost"}),
        client.get(
            reverse("events:portal-gallery", args=(event.slug,)),
            headers={"host": "alpha.localhost"},
        ),
        client.get(
            reverse("events:portal-unlock", args=(event.slug,)),
            headers={"host": "alpha.localhost"},
        ),
    ]

    for page in pages:
        assert page.status_code == 200
        assert b"OpenFotos" not in page.content
        assert b"OneNodeAI" in page.content
        assert b"Source code" in page.content
    assert b"Ballads of Love" in pages[0].content
