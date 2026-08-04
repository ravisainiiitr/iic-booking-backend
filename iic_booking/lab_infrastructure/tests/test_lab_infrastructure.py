"""Integration-style tests for Laboratory Infrastructure Phase 2 APIs."""

from __future__ import annotations

import pytest
from django.urls import reverse
from rest_framework.test import APIClient

from iic_booking.lab_infrastructure.models import ConfigurationAck, LabAlert, LabAuditEvent, LabRepairAction
from iic_booking.lab_infrastructure.services.detectors import run_health_detectors
from iic_booking.lab_infrastructure.services.fleet import build_infrastructure_tree


@pytest.mark.django_db
def test_build_infrastructure_tree_smoke():
    tree = build_infrastructure_tree()
    assert "departments" in tree
    assert "counts" in tree
    assert "generated_at" in tree


@pytest.mark.django_db
def test_health_detectors_smoke():
    result = run_health_detectors()
    assert "opened_or_updated" in result


@pytest.mark.django_db
def test_lab_infrastructure_requires_auth():
    client = APIClient()
    url = "/api/v1/lab/infrastructure/"
    resp = client.get(url)
    assert resp.status_code in {401, 403}


@pytest.mark.django_db
def test_lab_alert_and_repair_models():
    alert = LabAlert.objects.create(
        code="test",
        title="Test alert",
        severity=LabAlert.Severity.WARNING,
        fingerprint="test-fp-1",
    )
    assert alert.status == LabAlert.Status.OPEN
    repair = LabRepairAction.objects.create(
        node_kind="dsa",
        node_id="dsa:00000000-0000-0000-0000-000000000001",
        action=LabRepairAction.Action.REFRESH_CONFIGURATION,
    )
    assert repair.status == LabRepairAction.Status.QUEUED
    LabAuditEvent.objects.create(event_type="test_event", message="ok")
    assert LabAuditEvent.objects.count() == 1


@pytest.mark.django_db
def test_configuration_ack_model():
    ack = ConfigurationAck.objects.create(
        configuration_version=3,
        status=ConfigurationAck.Status.APPLIED,
        equipment_pc_id="eq-1",
    )
    assert ack.configuration_version == 3


@pytest.mark.django_db
def test_run_health_detectors_task_importable():
    from iic_booking.lab_infrastructure.tasks import run_health_detectors_task

    assert run_health_detectors_task.name == "lab_infrastructure.run_health_detectors"


@pytest.mark.django_db
def test_node_detail_missing_returns_none():
    from iic_booking.lab_infrastructure.services.fleet import get_node_detail

    assert get_node_detail("dsa:00000000-0000-0000-0000-000000000099") is None
