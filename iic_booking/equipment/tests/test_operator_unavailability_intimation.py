"""Lab In-charge unavailability is an intimation: no OIC approval, shown as Submitted; team calendar is OIC/Admin only."""

from __future__ import annotations

from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from iic_booking.equipment.models import OperatorLeaveRequest
from iic_booking.users.models import UserType

User = get_user_model()

LEAVE_URL = "/api/operator/leave/requests/"
TEAM_CALENDAR_URL = "/api/team-calendar/department/"


class _SyncThread:
    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}

    def start(self):
        self._target(*self._args, **self._kwargs)


class OperatorUnavailabilityIntimationTests(TestCase):
    def setUp(self):
        self.operator = User.objects.create_user(
            email="lab.incharge@test.iitr.ac.in", password="pass12345", name="Lab", user_type=UserType.OPERATOR
        )
        self.api = APIClient()
        self.api.force_authenticate(self.operator)

    def _submit(self):
        return self.api.post(
            LEAVE_URL,
            {
                "start_date": "2026-10-05",
                "end_date": "2026-10-06",
                "start_session": "FN",
                "end_session": "AN",
                "reason": "Personal work",
            },
            format="json",
        )

    def test_submission_needs_no_oic_approval_and_intimates(self):
        with mock.patch("iic_booking.equipment.api_views.threading.Thread", _SyncThread), mock.patch(
            "iic_booking.equipment.api_views._send_leave_intimation_emails"
        ) as intimate:
            resp = self._submit()
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(resp.data["status_display"], "Submitted")
        req = OperatorLeaveRequest.objects.get(pk=resp.data["id"])
        self.assertEqual(req.status, OperatorLeaveRequest.Status.APPROVED)
        self.assertEqual(req.reviewed_by_id, self.operator.id)
        intimate.assert_called_once()

    def test_history_shows_submitted(self):
        with mock.patch("iic_booking.equipment.api_views.threading.Thread", _SyncThread), mock.patch(
            "iic_booking.equipment.api_views._send_leave_intimation_emails"
        ):
            self._submit()
        rows = self.api.get(LEAVE_URL, {"year": 2026}).data["leaves"]
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["self_intimated"])
        self.assertEqual(rows[0]["status_display"], "Submitted")

    def test_intimation_emails_do_not_raise_without_templates(self):
        with mock.patch("iic_booking.equipment.api_views.threading.Thread", _SyncThread):
            resp = self._submit()
        self.assertEqual(resp.status_code, 201, resp.data)

    def test_team_calendar_hidden_from_lab_incharge(self):
        resp = self.api.get(TEAM_CALENDAR_URL, {"month": "2026-10"})
        self.assertEqual(resp.status_code, 403)

    def test_team_calendar_open_to_oic(self):
        oic = User.objects.create_user(
            email="oic.cal@test.iitr.ac.in", password="pass12345", name="OIC", user_type=UserType.MANAGER
        )
        api = APIClient()
        api.force_authenticate(oic)
        resp = api.get(TEAM_CALENDAR_URL, {"month": "2026-10"})
        self.assertNotEqual(resp.status_code, 403)
