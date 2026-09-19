from django.urls import path

from . import legal_views, share_views, views

app_name = "events"

urlpatterns = [
    path("", views.portfolio, name="portfolio"),
    path("legal/privacy/", legal_views.privacy_notice, name="privacy-notice"),
    path("legal/terms/", legal_views.terms, name="terms"),
    path("login/", views.photographer_login, name="login"),
    path("logout/", views.photographer_logout, name="logout"),
    path("dashboard/", views.dashboard, name="dashboard"),
    path("dashboard/events/create/", views.create_event, name="create-event"),
    path("dashboard/portfolio/", views.update_portfolio, name="update-portfolio"),
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
        "dashboard/events/<uuid:event_id>/portal-pin/rotate/",
        views.rotate_event_portal_pin,
        name="rotate-portal-pin",
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
        "dashboard/events/<uuid:event_id>/photos/<uuid:asset_id>/download/",
        views.photographer_download,
        name="photographer-download",
    ),
    path(
        "dashboard/events/<uuid:event_id>/sub-events/<uuid:sub_event_id>/photos/<uuid:asset_id>/download/",
        views.photographer_download,
        name="photographer-sub-event-download",
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
    path(
        "portfolio/events/<slug:event_slug>/",
        share_views.portal_gallery,
        name="portal-gallery",
    ),
    path(
        "portfolio/events/<slug:event_slug>/unlock/",
        share_views.portal_unlock,
        name="portal-unlock",
    ),
    path(
        "portfolio/events/<slug:event_slug>/sub-events/<uuid:sub_event_id>/",
        share_views.portal_gallery,
        name="portal-sub-event",
    ),
    path(
        "portfolio/events/<slug:event_slug>/photos/<uuid:asset_id>/",
        share_views.portal_photo,
        name="portal-photo",
    ),
    path(
        "portfolio/events/<slug:event_slug>/sub-events/<uuid:sub_event_id>/photos/<uuid:asset_id>/",
        share_views.portal_photo,
        name="portal-sub-event-photo",
    ),
    path(
        "portfolio/events/<slug:event_slug>/photos/<uuid:asset_id>/download/",
        share_views.portal_download,
        name="portal-download",
    ),
    path(
        "portfolio/events/<slug:event_slug>/sub-events/<uuid:sub_event_id>/photos/<uuid:asset_id>/download/",
        share_views.portal_download,
        name="portal-sub-event-download",
    ),
    path(
        "portfolio/events/<slug:event_slug>/search/",
        share_views.portal_search,
        name="portal-search",
    ),
    path(
        "portfolio/events/<slug:event_slug>/sub-events/<uuid:sub_event_id>/search/",
        share_views.portal_search,
        name="portal-sub-event-search",
    ),
    path(
        "portfolio/events/<slug:event_slug>/search/<uuid:result_id>/",
        share_views.portal_search_results,
        name="portal-search-results",
    ),
    path(
        "portfolio/events/<slug:event_slug>/search/<uuid:result_id>/clear/",
        share_views.portal_clear_search,
        name="portal-clear-search",
    ),
]
