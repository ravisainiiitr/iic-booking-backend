"""Minimal reverse-tunnel lifecycle metadata (no per-packet storage)."""

from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from iic_booking.remote_analysis.constants import TunnelSessionStatus


class TunnelSession(models.Model):
    """One reverse-tunnel binding for a desktop session / analysis job."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    desktop_session = models.ForeignKey(
        "remote_analysis.RemoteDesktopSession",
        on_delete=models.CASCADE,
        related_name="tunnel_sessions",
        null=True,
        blank=True,
    )
    booking = models.ForeignKey(
        "equipment.Booking",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="tunnel_sessions",
    )
    analysis_job = models.ForeignKey(
        "remote_analysis.AnalysisJob",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="tunnel_sessions",
    )
    workstation = models.ForeignKey(
        "remote_analysis.AnalysisWorkstation",
        on_delete=models.PROTECT,
        related_name="tunnel_sessions",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="ra_tunnel_sessions",
    )
    status = models.CharField(
        max_length=32,
        choices=TunnelSessionStatus.choices,
        default=TunnelSessionStatus.PENDING,
        db_index=True,
    )
    session_version = models.PositiveIntegerField(default=1)
    nonce = models.CharField(max_length=64, unique=True)
    adapter_hostname = models.CharField(max_length=255, blank=True, default="")
    adapter_port = models.PositiveIntegerField(null=True, blank=True)
    gateway_instance = models.CharField(max_length=128, blank=True, default="")
    token_expires_at = models.DateTimeField(null=True, blank=True)
    agent_joined_at = models.DateTimeField(null=True, blank=True)
    activated_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    close_reason = models.CharField(max_length=255, blank=True, default="")
    bytes_sent = models.BigIntegerField(default=0)
    bytes_received = models.BigIntegerField(default=0)
    reconnect_count = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Tunnel session")
        verbose_name_plural = _("Tunnel sessions")
        indexes = [
            models.Index(fields=["status", "created_at"]),
            models.Index(fields=["workstation", "status"]),
        ]

    def __str__(self) -> str:
        return f"TunnelSession {self.id} ({self.status})"


class TunnelEvent(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tunnel = models.ForeignKey(TunnelSession, on_delete=models.CASCADE, related_name="events")
    event_type = models.CharField(max_length=64, db_index=True)
    detail = models.TextField(blank=True, default="")
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("Tunnel event")
        verbose_name_plural = _("Tunnel events")
        ordering = ["-created_at"]


class TunnelMetric(models.Model):
    """Periodic / terminal metrics snapshot for a tunnel."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tunnel = models.ForeignKey(TunnelSession, on_delete=models.CASCADE, related_name="metrics")
    latency_ms = models.FloatField(null=True, blank=True)
    bytes_sent = models.BigIntegerField(default=0)
    bytes_received = models.BigIntegerField(default=0)
    packet_loss = models.FloatField(null=True, blank=True)
    bandwidth_kbps = models.FloatField(null=True, blank=True)
    heartbeat_rtt_ms = models.FloatField(null=True, blank=True)
    recorded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("Tunnel metric")
        verbose_name_plural = _("Tunnel metrics")
        ordering = ["-recorded_at"]
