from django.urls import path

from . import views

app_name = "learning"

urlpatterns = [
    path("equipment/<int:equipment_id>/modules/", views.equipment_modules, name="equipment-modules"),
    path("equipment/<int:equipment_id>/settings/", views.equipment_setting, name="equipment-setting"),
    path("equipment/<int:equipment_id>/progress/", views.equipment_progress, name="equipment-progress"),
    path("equipment/<int:equipment_id>/logbook/", views.equipment_logbook, name="equipment-logbook"),
    path("modules/<int:pk>/", views.module_detail, name="module-detail"),
    path("modules/<int:pk>/document/", views.module_document, name="module-document"),
    path("modules/<int:pk>/questions/", views.module_questions, name="module-questions"),
    path("modules/<int:pk>/viewed/", views.module_viewed, name="module-viewed"),
    path("modules/<int:pk>/acknowledge/", views.module_acknowledge, name="module-acknowledge"),
    path("modules/<int:pk>/attempt/", views.module_attempt, name="module-attempt"),
    path("questions/<int:pk>/", views.question_detail, name="question-detail"),
    path("me/progress/", views.my_progress, name="my-progress"),
    path("bookings/<int:booking_id>/logbook/", views.booking_logbook, name="booking-logbook"),
]
