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
        "dashboard/events/<uuid:event_id>/download-policy/",
        views.update_download_policy,
        name="update-download-policy",
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
        "dashboard/events/<uuid:event_id>/photos/<uuid:asset_id>/",
        views.photographer_photo,
        name="photographer-photo",
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
    path("e/<str:token>/", views.event_access, name="event-access"),
    path(
        "e/<str:token>/photos/<uuid:asset_id>/",
        views.visitor_photo,
        name="visitor-photo",
    ),
]
