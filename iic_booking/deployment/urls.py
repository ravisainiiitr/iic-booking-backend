"""URL routes for Deployment Center."""

from django.urls import path

from iic_booking.deployment import views

app_name = "deployment"

urlpatterns = [
    path("center/", views.deployment_center, name="center"),
    path("wizard/releases/", views.wizard_releases_collection, name="wizard-releases"),
    path("wizard/releases/latest/", views.wizard_release_latest, name="wizard-latest"),
    path(
        "wizard/releases/latest/download-ticket/",
        views.wizard_download_ticket,
        name="wizard-latest-ticket",
    ),
    path(
        "wizard/releases/<uuid:release_id>/download-ticket/",
        views.wizard_download_ticket,
        name="wizard-ticket",
    ),
    path(
        "wizard/download/<str:token>/",
        views.wizard_download_by_ticket,
        name="wizard-ticket-download",
    ),
]
