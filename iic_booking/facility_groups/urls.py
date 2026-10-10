from django.urls import path

from . import views

app_name = "facility_groups"

# Mounted at /api/v1/admin/facility-groups/ (Main Administrator only).
urlpatterns = [
    path("", views.groups, name="groups"),
    path("options/", views.options, name="options"),
    path("users/search/", views.user_search, name="user-search"),
    path("email/preview/", views.email_preview, name="email-preview"),
    path("email/render/", views.email_render, name="email-render"),
    path("email/test/", views.email_test, name="email-test"),
    path("email/send/", views.email_send, name="email-send"),
    path("email/campaigns/", views.campaigns, name="campaigns"),
    path("email/campaigns/<int:campaign_id>/", views.campaign_detail, name="campaign-detail"),
    path("email/campaigns/<int:campaign_id>/resume/", views.campaign_resume, name="campaign-resume"),
    path("email/campaigns/<int:campaign_id>/cancel/", views.campaign_cancel, name="campaign-cancel"),
    path("<int:group_id>/", views.group_detail, name="group-detail"),
    path("<int:group_id>/members/", views.members, name="members"),
    path("<int:group_id>/members/export/", views.members_export, name="members-export"),
    path("<int:group_id>/members/add/", views.members_add, name="members-add"),
    path("<int:group_id>/members/remove/", views.members_remove, name="members-remove"),
    path("<int:group_id>/departments/", views.departments, name="departments"),
]
