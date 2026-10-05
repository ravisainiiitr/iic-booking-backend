from django.urls import path

from . import views

app_name = "department_modules"

# Mounted at /api/v1/admin/department-modules/ (Main Administrator only).
admin_urlpatterns = [
    path("", views.matrix, name="matrix"),
    path("history/", views.history, name="history"),
    path("<int:department_id>/<str:module_key>/", views.update_cell, name="update-cell"),
]

# Mounted at /api/v1/department-modules/ (any signed-in user).
urlpatterns = [
    path("me/", views.me, name="me"),
]
