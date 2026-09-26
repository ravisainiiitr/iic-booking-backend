from django.urls import path

from . import group_views, views

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
    # Research Groups (behind MY_RESEARCH_GROUPS_ENABLED)
    path("groups/home/", group_views.groups_home, name="groups-home"),
    path("groups/", group_views.groups_collection, name="groups"),
    path("groups/<uuid:group_id>/", group_views.group_detail, name="group-detail"),
    path("groups/<uuid:group_id>/archive/", group_views.group_archive, name="group-archive"),
    path("groups/<uuid:group_id>/members/", group_views.members_collection, name="group-members"),
    path("groups/<uuid:group_id>/members/<int:member_id>/", group_views.member_detail, name="group-member-detail"),
    path("groups/<uuid:group_id>/categories/", group_views.categories_collection, name="group-categories"),
    path("groups/<uuid:group_id>/categories/reorder/", group_views.categories_reorder, name="group-categories-reorder"),
    path(
        "groups/<uuid:group_id>/categories/<int:category_id>/",
        group_views.category_detail,
        name="group-category-detail",
    ),
    path("groups/<uuid:group_id>/activities/", group_views.activities_collection, name="group-activities"),
    path("groups/<uuid:group_id>/updates/", group_views.updates_list, name="group-updates"),
    path("groups/<uuid:group_id>/update-requests/", group_views.update_requests_create, name="group-update-requests"),
    path("groups/<uuid:group_id>/workspaces/", group_views.group_workspaces, name="group-workspaces"),
    path(
        "groups/<uuid:group_id>/workspaces/<uuid:workspace_id>/",
        group_views.group_workspace_unlink,
        name="group-workspace-unlink",
    ),
    path(
        "groups/<uuid:group_id>/linkable-workspaces/",
        group_views.linkable_workspaces,
        name="group-linkable-workspaces",
    ),
    path("groups/<uuid:group_id>/publications/", group_views.group_publications, name="group-publications"),
    path(
        "groups/<uuid:group_id>/publications/<int:claim_id>/",
        group_views.group_publication_unlink,
        name="group-publication-unlink",
    ),
    path(
        "groups/<uuid:group_id>/linkable-publications/",
        group_views.linkable_group_publications,
        name="group-linkable-publications",
    ),
    path("groups/<uuid:group_id>/linkable-bookings/", group_views.linkable_bookings, name="group-linkable-bookings"),
    path(
        "groups/<uuid:group_id>/linkable-equipment/", group_views.linkable_equipment, name="group-linkable-equipment"
    ),
    path("groups/<uuid:group_id>/feed/", group_views.group_feed, name="group-feed"),
    path("activities/<uuid:activity_id>/", group_views.activity_detail, name="group-activity-detail"),
    path("update-requests/<uuid:request_id>/", group_views.update_request_detail, name="update-request-detail"),
    path("update-requests/<uuid:request_id>/submit/", group_views.update_request_submit, name="update-request-submit"),
    path("update-requests/<uuid:request_id>/review/", group_views.update_request_review, name="update-request-review"),
    path("update-requests/<uuid:request_id>/cancel/", group_views.update_request_cancel, name="update-request-cancel"),
    path(
        "update-requests/<uuid:request_id>/attachments/",
        group_views.attachment_initiate,
        name="update-attachment-initiate",
    ),
    path("update-attachments/<uuid:attachment_id>/", group_views.attachment_delete, name="update-attachment-delete"),
    path(
        "update-attachments/<uuid:attachment_id>/complete/",
        group_views.attachment_complete,
        name="update-attachment-complete",
    ),
    path(
        "update-attachments/<uuid:attachment_id>/download/",
        group_views.attachment_download,
        name="update-attachment-download",
    ),
]
