from django.urls import path

from . import views

app_name = "events"

urlpatterns = [
    path("login/", views.photographer_login, name="login"),
    path("logout/", views.photographer_logout, name="logout"),
    path("dashboard/", views.dashboard, name="dashboard"),
    path("e/<str:token>/", views.event_access, name="event-access"),
]
