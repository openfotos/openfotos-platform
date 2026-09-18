import base64
import hashlib
from datetime import timedelta
from uuid import uuid4

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from openfotos_contracts import ContributionInput, EventState
from openfotos_server.events.desktop_auth import authenticate_photographer
from openfotos_server.events.ingestion_services import IngestionError, reserve_contribution
from openfotos_server.events.models import (
    AuditAction,
    Event,
    Photographer,
    PhotographerMembership,
)
from openfotos_server.events.sub_event_services import (
    create_sub_event,
    reassign_contribution,
    set_sub_event_archived,
    update_sub_event,
)

pytestmark = pytest.mark.django_db


def setup_event():
    photographer = Photographer.objects.create(slug="alpha", display_name="Alpha Photos")
    user = get_user_model().objects.create_user(
        username="photographer",
        password="correct-password",
    )
    PhotographerMembership.objects.create(photographer=photographer, user=user)
    event = Event.objects.create(
        photographer=photographer,
        name="ABC Wedding",
        expires_at=timezone.now() + timedelta(days=30),
    )
    session = authenticate_photographer(
        photographer=photographer,
        username="photographer",
        password="correct-password",
        installation_id=uuid4(),
    ).session
    return user, event, session


def contribution(sub_event_id):
    content = b"synthetic jpeg"
    return ContributionInput.from_dict(
        {
            "batch_id": str(uuid4()),
            "sub_event_id": str(sub_event_id),
            "label": "Edited photos",
            "processing_profile_id": "pilot-profile-v1",
            "device_label": "Studio workstation",
            "assets": [
                {
                    "id": str(uuid4()),
                    "filename": "photo.jpg",
                    "content_type": "image/jpeg",
                    "size_bytes": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "content_md5": base64.b64encode(
                        hashlib.md5(content, usedforsecurity=False).digest()
                    ).decode(),
                    "width": 8,
                    "height": 6,
                }
            ],
        }
    )


def test_sub_event_management_is_audited_and_names_are_unique_case_insensitively() -> None:
    user, event, _ = setup_event()
    reception = create_sub_event(event=event, name=" Reception ", position=2, actor=user)

    updated = update_sub_event(
        sub_event=reception,
        name="Wedding Reception",
        position=1,
        actor=user,
    )

    assert (updated.name, updated.position) == ("Wedding Reception", 1)
    assert event.audit_events.filter(action=AuditAction.SUB_EVENT_CREATED).count() == 1
    assert event.audit_events.filter(action=AuditAction.SUB_EVENT_CHANGED).count() == 1
    with pytest.raises(IngestionError) as conflict:
        create_sub_event(event=event, name="wedding reception", position=2, actor=user)
    assert conflict.value.code == "sub_event_name_conflict"


def test_every_contribution_requires_an_active_sub_event_in_the_same_event() -> None:
    user, event, session = setup_event()
    reception = create_sub_event(event=event, name="Reception", position=1, actor=user)
    other_event = Event.objects.create(photographer=event.photographer, name="Other wedding")
    foreign = create_sub_event(event=other_event, name="Haldi", position=1, actor=user)

    with pytest.raises(IngestionError) as wrong_event:
        reserve_contribution(
            session=session,
            event_id=event.id,
            contribution=contribution(foreign.id),
        )
    assert wrong_event.value.code == "sub_event_not_found"

    set_sub_event_archived(sub_event=reception, archived=True, actor=user)
    with pytest.raises(IngestionError) as archived:
        reserve_contribution(
            session=session,
            event_id=event.id,
            contribution=contribution(reception.id),
        )
    assert archived.value.code == "sub_event_not_found"


def test_whole_contribution_can_move_between_active_sections_before_publication() -> None:
    user, event, session = setup_event()
    reception = create_sub_event(event=event, name="Reception", position=1, actor=user)
    haldi = create_sub_event(event=event, name="Haldi", position=2, actor=user)
    batch = reserve_contribution(
        session=session,
        event_id=event.id,
        contribution=contribution(reception.id),
    )

    moved = reassign_contribution(
        event=event,
        batch_id=batch.id,
        sub_event_id=haldi.id,
        actor=user,
    )

    assert moved.sub_event == haldi
    audit = event.audit_events.get(action=AuditAction.CONTRIBUTION_REASSIGNED)
    assert audit.metadata["from_sub_event_id"] == str(reception.id)
    assert audit.metadata["to_sub_event_id"] == str(haldi.id)

    Event.objects.filter(pk=event.id).update(state=EventState.PUBLISHED.value)
    event.refresh_from_db()
    with pytest.raises(IngestionError) as locked:
        reassign_contribution(
            event=event,
            batch_id=batch.id,
            sub_event_id=reception.id,
            actor=user,
        )
    assert locked.value.code == "event_structure_locked"


def test_archive_and_restore_are_reversible_only_while_unpublished() -> None:
    user, event, _ = setup_event()
    reception = create_sub_event(event=event, name="Reception", position=1, actor=user)

    archived = set_sub_event_archived(sub_event=reception, archived=True, actor=user)
    restored = set_sub_event_archived(sub_event=archived, archived=False, actor=user)

    assert not restored.is_archived
    assert event.audit_events.filter(action=AuditAction.SUB_EVENT_ARCHIVED).count() == 1
    assert event.audit_events.filter(action=AuditAction.SUB_EVENT_RESTORED).count() == 1

    Event.objects.filter(pk=event.id).update(state=EventState.PUBLISHED.value)
    with pytest.raises(IngestionError) as locked:
        set_sub_event_archived(sub_event=restored, archived=True, actor=user)
    assert locked.value.code == "event_structure_locked"
