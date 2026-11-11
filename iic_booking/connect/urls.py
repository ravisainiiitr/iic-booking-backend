from django.urls import path

from .api import communities
from .api import equipment
from .api import home
from .api import knowledge
from .api import search
from .api import threads
from .api import views

app_name = "connect"

urlpatterns = [
    path("bootstrap/", views.BootstrapView.as_view(), name="bootstrap"),
    path("home/", home.HomeView.as_view(), name="home"),
    path("search/", search.SearchView.as_view(), name="search"),
    path("suggest/", search.SuggestView.as_view(), name="suggest"),
    # Communities and membership
    path("communities/", communities.CommunityListView.as_view(), name="communities"),
    path("communities/<slug:slug>/", communities.CommunityDetailView.as_view(), name="community"),
    path("communities/<slug:slug>/follow/", threads.CommunityFollowView.as_view(), name="community-follow"),
    path("communities/<slug:slug>/members/", communities.CommunityMembersView.as_view(), name="community-members"),
    path(
        "communities/<slug:slug>/members/<int:user_id>/",
        communities.CommunityMemberActionView.as_view(),
        name="community-member-action",
    ),
    path("communities/<slug:slug>/mention-candidates/", home.MentionCandidatesView.as_view(), name="mention-candidates"),
    path("communities/<slug:slug>/audit/", communities.CommunityAuditView.as_view(), name="community-audit"),
    # Threads and posts
    path("threads/", threads.ThreadListView.as_view(), name="threads"),
    path("threads/<int:pk>/", threads.ThreadDetailView.as_view(), name="thread"),
    path("threads/<int:pk>/posts/", threads.ThreadPostsView.as_view(), name="thread-posts"),
    path("threads/<int:pk>/bookmark/", threads.ThreadBookmarkView.as_view(), name="thread-bookmark"),
    path("threads/<int:pk>/follow/", threads.ThreadFollowView.as_view(), name="thread-follow"),
    path("threads/<int:pk>/vote/", threads.ThreadVoteView.as_view(), name="thread-vote"),
    path("threads/<int:pk>/solution/", knowledge.ThreadSolutionView.as_view(), name="thread-solution"),
    path("threads/<int:pk>/staff/", equipment.ThreadStaffToolsView.as_view(), name="thread-staff"),
    path("equipment/<int:equipment_id>/discussions/", equipment.EquipmentDiscussionsView.as_view(), name="equipment-discussions"),
    # Knowledge Hub
    path("articles/", knowledge.ArticleListView.as_view(), name="articles"),
    path("articles/<int:pk>/", knowledge.ArticleDetailView.as_view(), name="article"),
    path("articles/<int:pk>/revisions/", knowledge.ArticleRevisionsView.as_view(), name="article-revisions"),
    path("articles/<int:pk>/<str:action>/", knowledge.ArticleActionView.as_view(), name="article-action"),
    path("posts/<int:pk>/", threads.PostDetailView.as_view(), name="post"),
    path("posts/<int:pk>/revisions/", threads.PostRevisionsView.as_view(), name="post-revisions"),
    path("posts/<int:pk>/react/", threads.PostReactView.as_view(), name="post-react"),
    # Categories, tags, drafts, attachments
    path("categories/", threads.CategoryListView.as_view(), name="categories"),
    path("categories/<int:pk>/", threads.CategoryDetailView.as_view(), name="category"),
    path("tags/", threads.TagListView.as_view(), name="tags"),
    path("tags/<slug:slug>/", threads.TagAdminView.as_view(), name="tag"),
    path("drafts/", threads.DraftListView.as_view(), name="drafts"),
    path("drafts/<str:key>/", threads.DraftDetailView.as_view(), name="draft"),
    path("attachments/", threads.AttachmentUploadView.as_view(), name="attachments"),
    path("attachments/<uuid:pk>/", threads.AttachmentDetailView.as_view(), name="attachment"),
    # Administration
    path("admin/sync/", communities.AdminSyncView.as_view(), name="admin-sync"),
]
