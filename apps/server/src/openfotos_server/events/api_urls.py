from django.urls import path

from . import api

app_name = "desktop-api"

urlpatterns = [
    path("auth/login/", api.login, name="login"),
    path("auth/refresh/", api.refresh, name="refresh"),
    path("events/", api.events, name="events"),
    path("events/<uuid:event_id>/batches/", api.reserve_batch, name="reserve-batch"),
    path(
        "events/<uuid:event_id>/preview-policy/confirm/",
        api.confirm_preview_policy_view,
        name="confirm-preview-policy",
    ),
    path(
        "events/<uuid:event_id>/preview-policy/mark/",
        api.preview_policy_mark,
        name="preview-policy-mark",
    ),
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
        "events/<uuid:event_id>/assets/<uuid:asset_id>/derivatives/",
        api.register_derivatives,
        name="register-derivatives",
    ),
    path(
        "events/<uuid:event_id>/assets/<uuid:asset_id>/derivative-leases/",
        api.derivative_leases,
        name="derivative-leases",
    ),
    path(
        "events/<uuid:event_id>/assets/<uuid:asset_id>/derivatives/<str:variant>/complete/",
        api.complete_derivative,
        name="complete-derivative",
    ),
    path(
        "events/<uuid:event_id>/assets/<uuid:asset_id>/source-url/",
        api.owned_original_url,
        name="owned-original-url",
    ),
    path(
        "events/<uuid:event_id>/assets/<uuid:asset_id>/derivative-failure/",
        api.derivative_failure,
        name="derivative-failure",
    ),
    path(
        "events/<uuid:event_id>/assets/<uuid:asset_id>/exclude/",
        api.exclude_asset_view,
        name="exclude-asset",
    ),
    path(
        "events/<uuid:event_id>/sub-events/<uuid:sub_event_id>/assets/"
        "<uuid:asset_id>/face-analysis/",
        api.face_analysis,
        name="face-analysis",
    ),
    path(
        "events/<uuid:event_id>/sub-events/<uuid:sub_event_id>/assets/"
        "<uuid:asset_id>/face-analysis-failure/",
        api.face_analysis_failure,
        name="face-analysis-failure",
    ),
    path("events/<uuid:event_id>/intake/close/", api.intake_action, {"action": "close"}),
    path("events/<uuid:event_id>/intake/reopen/", api.intake_action, {"action": "reopen"}),
    path("events/<uuid:event_id>/finalize/", api.finalize, name="finalize"),
    path(
        "events/<uuid:event_id>/installations/<uuid:installation_id>/revoke/",
        api.revoke_installation_view,
        name="revoke-installation",
    ),
]
