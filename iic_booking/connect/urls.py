from django.urls import path

from .api import communities
from .api import views

app_name = "connect"

urlpatterns = [
    path("bootstrap/", views.BootstrapView.as_view(), name="bootstrap"),
    path("communities/", communities.CommunityListView.as_view(), name="communities"),
    path("communities/<slug:slug>/", communities.CommunityDetailView.as_view(), name="community"),
    path("communities/<slug:slug>/members/", communities.CommunityMembersView.as_view(), name="community-members"),
    path(
        "communities/<slug:slug>/members/<int:user_id>/",
        communities.CommunityMemberActionView.as_view(),
        name="community-member-action",
    ),
    path("communities/<slug:slug>/audit/", communities.CommunityAuditView.as_view(), name="community-audit"),
    path("admin/sync/", communities.AdminSyncView.as_view(), name="admin-sync"),
]
