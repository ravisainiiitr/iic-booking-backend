"""UPS health monitoring: UPS units read by a Department Sync Agent over Modbus TCP, their latest reading and a
5-minute history. Alerts go to the existing ``sync.AlertEvent`` (category ``ups``)."""

from __future__ import annotations

from django.db import models


class UpsDevice(models.Model):
    """One UPS (network card) on a department LAN, polled by the department's sync agent."""

    name = models.CharField(max_length=120)
    sync_agent = models.ForeignKey("sync.DepartmentSyncAgent", on_delete=models.CASCADE, related_name="ups_devices")
    department = models.ForeignKey("users.Department", on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name="ups_devices")
    equipment = models.ManyToManyField("equipment.Equipment", blank=True, related_name="ups_devices",
                                       help_text="Instruments this UPS powers (shown with their alerts)")
    manufacturer = models.CharField(max_length=60, default="Vertiv")
    model = models.CharField(max_length=80, blank=True, help_text="e.g. Liebert GXT5-10KMVRT, ITA2 10kVA")
    host = models.CharField(max_length=255, help_text="IP address or host name of the UPS network card")
    port = models.PositiveIntegerField(default=502)
    unit_id = models.PositiveSmallIntegerField(default=1, help_text="Modbus unit / slave id")
    register_map = models.CharField(max_length=60, default="vertiv_unity_template",
                                    help_text="Key of a map in power.register_maps, or 'custom'")
    custom_map = models.JSONField(default=list, blank=True, help_text="Points when register_map is 'custom'")
    map_verified = models.BooleanField(default=False,
                                       help_text="Set once the register addresses were checked against this UPS")
    poll_seconds = models.PositiveIntegerField(default=30)
    low_battery_percent = models.PositiveSmallIntegerField(default=30)
    high_load_percent = models.PositiveSmallIntegerField(default=90)
    enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        constraints = [models.UniqueConstraint(fields=["host", "port", "unit_id"], name="uniq_ups_endpoint")]

    def __str__(self):
        return self.name


class UpsReading(models.Model):
    """A reading as sent by the agent. The latest per UPS is ``UpsDevice.latest``; older ones are kept every 5 minutes."""

    device = models.ForeignKey(UpsDevice, on_delete=models.CASCADE, related_name="readings")
    read_at = models.DateTimeField(db_index=True)
    reachable = models.BooleanField(default=True)
    input_voltage = models.FloatField(null=True, blank=True)
    output_voltage = models.FloatField(null=True, blank=True)
    output_frequency = models.FloatField(null=True, blank=True)
    load_percent = models.FloatField(null=True, blank=True)
    battery_percent = models.FloatField(null=True, blank=True)
    runtime_minutes = models.FloatField(null=True, blank=True)
    battery_voltage = models.FloatField(null=True, blank=True)
    temperature_c = models.FloatField(null=True, blank=True)
    on_battery = models.BooleanField(null=True, blank=True)
    on_bypass = models.BooleanField(null=True, blank=True)
    low_battery = models.BooleanField(null=True, blank=True)
    overload = models.BooleanField(null=True, blank=True)
    fault = models.BooleanField(null=True, blank=True)
    replace_battery = models.BooleanField(null=True, blank=True)
    error = models.CharField(max_length=200, blank=True)
    raw = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-read_at"]
        indexes = [models.Index(fields=["device", "-read_at"])]
