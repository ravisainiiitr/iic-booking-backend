from django.urls import path

from . import mapping_views, views

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
    path("facility-aliases/", mapping_views.facility_aliases, name="facility-aliases"),
    path("facility-aliases/<int:pk>/", mapping_views.facility_alias_detail, name="facility-alias-detail"),
    path("author-candidates/", mapping_views.author_candidates, name="author-candidates"),
    path("author-candidates/<int:pk>/decide/", mapping_views.author_candidate_decide, name="author-candidate-decide"),
    path("author-candidates/<int:pk>/works/", mapping_views.author_candidate_works, name="author-candidate-works"),
    path("author-confirmations/", mapping_views.author_confirmations, name="author-confirmations"),
    path("author-confirmations/count/", mapping_views.author_confirmations_count, name="author-confirmations-count"),
    path("author-confirmations/mine/", mapping_views.author_confirmations_mine, name="author-confirmations-mine"),
    path("author-confirmations/<int:user_id>/", mapping_views.author_confirmations_decide, name="author-confirmations-decide"),
    path("publication-mappings/", mapping_views.publication_mappings, name="publication-mappings"),
    path("publication-matches/", mapping_views.publication_matches, name="publication-matches"),
    path("publication-matches/count/", mapping_views.publication_matches_count, name="publication-matches-count"),
    path("publication-matches/<int:pk>/approve/", mapping_views.publication_match_approve, name="publication-match-approve"),
    path("publication-matches/<int:pk>/reject/", mapping_views.publication_match_reject, name="publication-match-reject"),
]
