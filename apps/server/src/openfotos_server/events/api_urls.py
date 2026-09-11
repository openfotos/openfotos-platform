from django.urls import path

from . import api

app_name = "desktop-api"

urlpatterns = [
    path("auth/login/", api.login, name="login"),
    path("auth/refresh/", api.refresh, name="refresh"),
    path("uploader-invitations/redeem/", api.redeem, name="redeem"),
    path("events/", api.events, name="events"),
    path("events/<uuid:event_id>/invitations/", api.invitations, name="invitations"),
    path(
        "events/<uuid:event_id>/invitations/<uuid:invitation_id>/revoke/",
        api.revoke_invitation_view,
        name="revoke-invitation",
    ),
    path("events/<uuid:event_id>/batches/", api.reserve_batch, name="reserve-batch"),
    path(
        "events/<uuid:event_id>/batches/<uuid:batch_id>/",
        api.batch_detail,
        name="batch-detail",
    ),
    path(
        "events/<uuid:event_id>/batches/<uuid:batch_id>/upload-leases/",
        api.upload_leases,
        name="upload-leases",
    ),
    path(
        "events/<uuid:event_id>/batches/<uuid:batch_id>/cancel/",
        api.cancel_batch_view,
        name="cancel-batch",
    ),
    path(
        "events/<uuid:event_id>/assets/<uuid:asset_id>/complete/",
        api.complete_asset,
        name="complete-asset",
    ),
    path(
        "events/<uuid:event_id>/assets/<uuid:asset_id>/exclude/",
        api.exclude_asset_view,
        name="exclude-asset",
    ),
    path("events/<uuid:event_id>/intake/close/", api.intake_action, {"action": "close"}),
    path("events/<uuid:event_id>/intake/reopen/", api.intake_action, {"action": "reopen"}),
    path("events/<uuid:event_id>/finalize/", api.finalize, name="finalize"),
    path(
        "events/<uuid:event_id>/devices/<uuid:device_id>/revoke/",
        api.revoke_device_view,
        name="revoke-device",
    ),
]
