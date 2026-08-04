"""Deployment Center — unified installer catalog for Main Administrators."""

from django.apps import AppConfig


class DeploymentConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "iic_booking.deployment"
    verbose_name = "Deployment Center"
