from django.urls import path

from . import views

app_name = "impact"

urlpatterns = [
    path("funding/", views.funding_list, name="funding-list"),
    path("funding/<int:pk>/", views.funding_detail, name="funding-detail"),
    path("acknowledgement/", views.acknowledgement_policy, name="acknowledgement-policy"),
    path("acknowledgement/check/", views.acknowledgement_check, name="acknowledgement-check"),
    path("me/orcid/", views.my_orcid, name="my-orcid"),
    path("me/suggestions/", views.my_suggestions, name="my-suggestions"),
    path("me/suggestions/check/", views.my_suggestions_check, name="my-suggestions-check"),
    path("me/suggestions/<int:pk>/dismiss/", views.suggestion_dismiss, name="suggestion-dismiss"),
    path("report/", views.funder_report, name="funder-report"),
]
