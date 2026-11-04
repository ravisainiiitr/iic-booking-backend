from django.urls import path

from . import views

app_name = "upkeep"

urlpatterns = [
    path("equipment/", views.equipment_list, name="equipment-list"),
    path("equipment/<int:equipment_id>/", views.equipment_detail, name="equipment-detail"),
    path("equipment/<int:equipment_id>/plans/", views.plan_create, name="plan-create"),
    path("plans/<int:pk>/", views.plan_detail, name="plan-detail"),
    path("plans/<int:pk>/close/", views.plan_close, name="plan-close"),
    path("plans/<int:pk>/complete/", views.plan_complete, name="plan-complete"),
    path("alerts/", views.alert_list, name="alert-list"),
    path("alerts/<int:pk>/acknowledge/", views.alert_acknowledge, name="alert-acknowledge"),
]
