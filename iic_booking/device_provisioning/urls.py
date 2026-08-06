"""URL routes for Device Provisioning."""

from django.urls import path

from iic_booking.device_provisioning import views

app_name = "device_provisioning"

urlpatterns = [
    path("capabilities/", views.capabilities, name="capabilities"),
    path("self-test/", views.provisioning_self_test, name="self-test"),
    path("console/", views.console_summary, name="console"),
    path("sessions/", views.sessions_create, name="sessions-create"),
    path("sessions/<uuid:session_id>/", views.session_detail, name="session-detail"),
    path("sessions/<uuid:session_id>/claim/", views.session_claim, name="session-claim"),
    path("pending/", views.pending_list, name="pending-list"),
    path("pending/approve-by-code/", views.pending_approve_by_code, name="pending-approve-by-code"),
    path("pending/<uuid:session_id>/approve/", views.pending_approve, name="pending-approve"),
    path("pending/<uuid:session_id>/reject/", views.pending_reject, name="pending-reject"),
    path("pending/<uuid:session_id>/", views.pending_update, name="pending-update"),
    path("policies/", views.department_policy_list, name="policies-list"),
    path("policies/<int:department_id>/", views.department_policy_detail, name="policies-detail"),
    path("unassigned-equipment/", views.unassigned_equipment, name="unassigned-equipment"),
    path("dsa/equipment-tree/", views.dsa_equipment_tree, name="dsa-equipment-tree"),
    path("devices/", views.devices_list, name="devices-list"),
    path("devices/retired/", views.devices_retired, name="devices-retired"),
    path("devices/<uuid:device_id>/", views.device_detail, name="device-detail"),
    path("devices/<uuid:device_id>/suspend/", views.device_suspend, name="device-suspend"),
    path("devices/<uuid:device_id>/revoke/", views.device_revoke, name="device-revoke"),
    path("devices/<uuid:device_id>/retire/", views.device_retire, name="device-retire"),
    path("devices/<uuid:device_id>/replace/", views.device_replace, name="device-replace"),
    path("devices/<uuid:device_id>/heartbeat/", views.device_heartbeat, name="device-heartbeat"),
    path("audit/", views.audit_list, name="audit-list"),
]
