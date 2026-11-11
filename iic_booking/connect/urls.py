from django.urls import path

from .api import views

app_name = "connect"

urlpatterns = [
    path("bootstrap/", views.BootstrapView.as_view(), name="bootstrap"),
]
