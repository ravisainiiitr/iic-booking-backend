from django.urls import path

from . import views

app_name = "my_research"

urlpatterns = [
    path("bootstrap/", views.bootstrap, name="bootstrap"),
    path("", views.home, name="home"),
    path("workspaces/", views.workspaces_collection, name="workspaces"),
    path("workspaces/<uuid:workspace_id>/", views.workspace_detail, name="workspace-detail"),
    path("workspaces/<uuid:workspace_id>/archive/", views.workspace_archive, name="workspace-archive"),
    path("workspaces/<uuid:workspace_id>/restore/", views.workspace_restore, name="workspace-restore"),
    path("workspaces/<uuid:workspace_id>/folders/", views.folders_collection, name="folders"),
    path("workspaces/<uuid:workspace_id>/files/", views.files_collection, name="files"),
    path("workspaces/<uuid:workspace_id>/uploads/initiate/", views.upload_initiate, name="upload-initiate"),
    path("workspaces/<uuid:workspace_id>/bookings/", views.bookings_collection, name="bookings"),
    path(
        "workspaces/<uuid:workspace_id>/bookings/<int:booking_id>/", views.booking_unlink, name="booking-unlink"
    ),
    path("workspaces/<uuid:workspace_id>/linkable-bookings/", views.linkable_bookings, name="linkable-bookings"),
    path("workspaces/<uuid:workspace_id>/equipment/", views.equipment_used, name="equipment"),
    path("workspaces/<uuid:workspace_id>/publications/", views.publications_collection, name="publications"),
    path(
        "workspaces/<uuid:workspace_id>/publications/<int:claim_id>/",
        views.publication_unlink,
        name="publication-unlink",
    ),
    path(
        "workspaces/<uuid:workspace_id>/linkable-publications/",
        views.linkable_publications,
        name="linkable-publications",
    ),
    path("workspaces/<uuid:workspace_id>/members/", views.members_collection, name="members"),
    path("workspaces/<uuid:workspace_id>/members/<int:member_id>/", views.member_remove, name="member-remove"),
    path("workspaces/<uuid:workspace_id>/activity/", views.activity_list, name="activity"),
    path("workspaces/<uuid:workspace_id>/search/", views.workspace_search, name="search"),
    path("folders/<uuid:folder_id>/", views.folder_detail, name="folder-detail"),
    path("files/<uuid:file_id>/", views.file_detail, name="file-detail"),
    path("files/<uuid:file_id>/download/", views.file_download, name="file-download"),
    path("files/<uuid:file_id>/preview/", views.file_preview, name="file-preview"),
    path("uploads/<uuid:file_id>/parts/", views.upload_parts, name="upload-parts"),
    path("uploads/<uuid:file_id>/complete/", views.upload_complete, name="upload-complete"),
    path("uploads/<uuid:file_id>/abort/", views.upload_abort, name="upload-abort"),
]
