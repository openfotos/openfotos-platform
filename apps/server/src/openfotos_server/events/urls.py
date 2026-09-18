from django.urls import path

from . import views

app_name = "events"

urlpatterns = [
    path("login/", views.photographer_login, name="login"),
    path("logout/", views.photographer_logout, name="logout"),
    path("dashboard/", views.dashboard, name="dashboard"),
    path(
        "dashboard/events/<uuid:event_id>/",
        views.photographer_event,
        name="photographer-event",
    ),
    path(
        "dashboard/events/<uuid:event_id>/sub-events/",
        views.create_event_sub_event,
        name="create-sub-event",
    ),
    path(
        "dashboard/events/<uuid:event_id>/sub-events/<uuid:sub_event_id>/manage/",
        views.update_event_sub_event,
        name="update-sub-event",
    ),
    path(
        "dashboard/events/<uuid:event_id>/sub-events/<uuid:sub_event_id>/archive/",
        views.archive_event_sub_event,
        name="archive-sub-event",
    ),
    path(
        "dashboard/events/<uuid:event_id>/batches/<uuid:batch_id>/sub-event/",
        views.reassign_event_batch,
        name="reassign-batch",
    ),
    path(
        "dashboard/events/<uuid:event_id>/publish/",
        views.publish_event,
        name="publish-event",
    ),
    path(
        "dashboard/events/<uuid:event_id>/unpublish/",
        views.unpublish_event,
        name="unpublish-event",
    ),
    path(
        "dashboard/events/<uuid:event_id>/sub-events/<uuid:sub_event_id>/",
        views.photographer_event,
        name="photographer-sub-event",
    ),
    path(
        "dashboard/events/<uuid:event_id>/photos/<uuid:asset_id>/",
        views.photographer_photo,
        name="photographer-photo",
    ),
    path(
        "dashboard/events/<uuid:event_id>/sub-events/<uuid:sub_event_id>/photos/<uuid:asset_id>/",
        views.photographer_photo,
        name="photographer-sub-event-photo",
    ),
    path(
        "dashboard/events/<uuid:event_id>/photos/<uuid:asset_id>/exclude/",
        views.exclude_gallery_asset,
        name="exclude-gallery-asset",
    ),
    path(
        "dashboard/events/<uuid:event_id>/photos/<uuid:asset_id>/restore/",
        views.restore_gallery_asset,
        name="restore-gallery-asset",
    ),
    path(
        "dashboard/events/<uuid:event_id>/photos/<uuid:asset_id>/face-analysis/reset/",
        views.reset_asset_face_analysis,
        name="reset-face-analysis",
    ),
    path("e/<str:token>/", views.event_access, name="event-access"),
    path(
        "e/<str:token>/sub-events/<uuid:sub_event_id>/",
        views.event_access,
        name="visitor-sub-event",
    ),
    path(
        "e/<str:token>/photos/<uuid:asset_id>/",
        views.visitor_photo,
        name="visitor-photo",
    ),
    path(
        "e/<str:token>/sub-events/<uuid:sub_event_id>/photos/<uuid:asset_id>/",
        views.visitor_photo,
        name="visitor-sub-event-photo",
    ),
]
