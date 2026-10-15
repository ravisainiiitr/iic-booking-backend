from django.urls import path

from . import views

app_name = "automation"

urlpatterns = [
    path("features/", views.features, name="features"),
    path("instrument-finder/", views.instrument_finder, name="instrument-finder"),
    path("slot-suggestions/", views.slot_suggestions, name="slot-suggestions"),
    path("settings/", views.automation_settings, name="settings"),
    path("preview/<str:kind>/", views.automation_preview, name="preview"),
    path("log/", views.automation_log, name="log"),
    path("tickets/<int:ticket_id>/triage/", views.ticket_triage, name="ticket-triage"),
    path("tickets/<int:ticket_id>/triage/decision/", views.ticket_triage_decision, name="ticket-triage-decision"),
]
