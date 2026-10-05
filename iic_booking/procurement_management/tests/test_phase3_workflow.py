from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from iic_booking.procurement_management import config_service
from iic_booking.procurement_management import constants as c
from iic_booking.procurement_management.models import ApprovalAction, ImmutableRecordError
from iic_booking.users.models.user_type import UserType

from .conftest import API, act, category, client_for, config_of, line, make_user, new_request, pdf_upload

pytestmark = pytest.mark.django_db
RS = c.RequestStatus


@pytest.fixture
def sent(monkeypatch):
    calls = []

    def fake(recipients, **kwargs):
        calls.append({"to": {u.pk for u in recipients}, **kwargs})

    monkeypatch.setattr("iic_booking.communication.in_app.notify_in_app", fake)
    return calls


def offline_post(user, r, expect=200, **fields):
    body = {
        "decision": "APPROVE",
        "approver_name": "Prof. R. Sharma",
        "approver_designation": "Head of Department",
        "approval_date": timezone.localdate().isoformat(),
        "reference": "CHEM/PUR/12",
        **fields,
    }
    if "file" not in fields:
        body["file"] = pdf_upload()
    elif fields["file"] is None:
        body.pop("file")
    res = client_for(user).post(f"{API}/requests/{r.pk}/offline-hod-decision/", body, format="multipart")
    assert res.status_code == expect, res.json()
    r.refresh_from_db()
    return res


class TestRouting:
    def test_operator_request_goes_oic_then_stores(self, world, sent):
        r = new_request(world.operator, equipment=world.equipment, submit=True)
        assert r.approval_route == ["OIC", "STORES"]
        assert r.status == RS.PENDING_OIC
        assert any(world.oic.pk in call["to"] for call in sent)
        act(world.oic, r, "approve", comments="ok")
        assert r.status == RS.PENDING_STORES
        act(world.stores, r, "approve")
        assert r.status == RS.APPROVED
        assert r.approved_amount == Decimal("1000.00")
        history = list(r.approval_actions.values_list("stage", "action", "to_status"))
        assert history == [
            ("REQUESTER", "SUBMIT", RS.PENDING_OIC),
            ("OIC", "APPROVE", RS.PENDING_STORES),
            ("STORES", "APPROVE", RS.APPROVED),
        ]
        assert r.approval_actions.get(stage="OIC").actor_role == c.ModuleRole.OIC

    def test_oic_raised_request_skips_oic_stage(self, world):
        r = new_request(world.oic, equipment=world.equipment, submit=True)
        assert r.raised_as_role == c.ModuleRole.OIC
        assert r.approval_route == ["STORES"]
        assert r.status == RS.PENDING_STORES

    def test_temporary_oic_can_approve(self, world, temp_oic):
        r = new_request(world.operator, equipment=world.equipment, submit=True)
        act(temp_oic, r, "approve")
        assert r.status == RS.PENDING_STORES

    def test_above_hod_threshold_needs_hod(self, world):
        r = new_request(world.operator, equipment=world.equipment, lines=[line("25000.01")], submit=True)
        assert r.approval_route == ["OIC", "STORES", "HOD"]
        at_threshold = new_request(world.operator, equipment=world.equipment, lines=[line("25000.00")], submit=True)
        assert at_threshold.approval_route == ["OIC", "STORES"]

    def test_hod_threshold_comes_from_config(self, world):
        config_service.update_config(world.admin, world.dept, {"hod_approval_threshold": "500.00"})
        r = new_request(world.operator, equipment=world.equipment, lines=[line("600.00")], submit=True)
        assert "HOD" in r.approval_route

    def test_major_asset_always_needs_hod(self, world):
        r = new_request(
            world.operator, equipment=world.equipment, rt="MAJOR_ASSET", cat=category(world.dept, "MAJOR_ASSET"),
            specification="Spec", lines=[line("100.00")], submit=True,
        )
        assert r.approval_route == ["OIC", "STORES", "HOD"]
        act(world.oic, r, "approve")
        act(world.stores, r, "approve")
        act(world.hod, r, "approve", amount="90.00")
        assert r.status == RS.APPROVED
        assert r.approved_amount == Decimal("90.00")

    def test_specification_required_for_assets(self, world):
        r = new_request(world.operator, equipment=world.equipment, rt="MINOR_ASSET", cat=category(world.dept, "MINOR_ASSET"))
        res = act(world.operator, r, "submit", expect=400)
        assert res.json()["code"] == "specification_required"

    def test_submit_needs_lines_and_justification(self, world):
        r = new_request(world.operator, equipment=world.equipment, lines=[])
        assert act(world.operator, r, "submit", expect=400).json()["code"] == "lines_required"
        r2 = new_request(world.operator, equipment=world.equipment, justification="")
        assert act(world.operator, r2, "submit", expect=400).json()["code"] == "required"


class TestSelfApprovalAndStageGuards:
    def test_requester_can_never_approve_own_request(self, world):
        stores2 = make_user(user_type=UserType.OPERATOR, name="Stores Two")
        config_service.assign_role(world.admin, world.dept, stores2, c.ModuleRole.OC_STORES)
        r = new_request(world.stores, rt="GENERAL_OFFICE", submit=True)
        assert r.status == RS.PENDING_STORES
        assert act(world.stores, r, "approve", expect=403).json()["code"] == "forbidden"
        assert "approve" not in client_for(world.stores).get(f"{API}/requests/{r.pk}/").json()["available_actions"]
        act(stores2, r, "approve")
        assert r.status == RS.APPROVED

    def test_submit_fails_when_only_approver_is_requester(self, world):
        r = new_request(world.stores, rt="GENERAL_OFFICE")
        res = act(world.stores, r, "submit", expect=400)
        assert res.json()["code"] == "no_approver" and res.json()["stage"] == "STORES"
        r.refresh_from_db()
        assert r.status == RS.DRAFT

    def test_wrong_stage_actors_refused(self, world):
        r = new_request(world.operator, equipment=world.equipment, submit=True)
        act(world.stores, r, "approve", expect=403)
        act(world.hod, r, "approve", expect=403)
        act(world.oic2, r, "approve", expect=404)
        act(world.other_oic, r, "approve", expect=404)
        act(world.operator2, r, "approve", expect=404)
        assert r.status == RS.PENDING_OIC

    def test_approved_amount_cannot_exceed_estimate(self, world):
        r = new_request(world.operator, equipment=world.equipment, submit=True)
        act(world.oic, r, "approve", expect=400, amount="1000.01")
        act(world.oic, r, "approve", amount="800.00")
        act(world.stores, r, "approve", expect=400, amount="900.00")


class TestRejectHoldResubmit:
    def test_reject_needs_reason_and_resubmit_restarts_route(self, world, sent):
        r = new_request(world.operator, equipment=world.equipment, submit=True)
        assert act(world.oic, r, "reject", expect=400).json()["code"] == "reason_required"
        act(world.oic, r, "approve")
        act(world.stores, r, "reject", reason="Use existing stock")
        assert r.status == RS.REJECTED and r.last_reason == "Use existing stock"
        assert any(world.operator.pk in call["to"] for call in sent)
        res = client_for(world.operator).patch(f"{API}/requests/{r.pk}/", {"lines": [line("400.00")]}, format="json")
        assert res.status_code == 200
        act(world.operator, r, "resubmit")
        assert r.status == RS.PENDING_OIC and r.route_index == 0 and r.resubmission_count == 1
        assert r.approval_actions.filter(action="RESUBMIT").count() == 1

    def test_resubmission_can_be_disabled(self, world):
        config_service.update_config(world.admin, world.dept, {"allow_resubmission": False})
        r = new_request(world.operator, equipment=world.equipment, submit=True)
        act(world.oic, r, "reject", reason="no")
        act(world.operator, r, "resubmit", expect=400)
        assert client_for(world.operator).patch(f"{API}/requests/{r.pk}/", {"title": "x"}, format="json").status_code == 400

    def test_hold_needs_reason_blocks_approval_and_resumes(self, world):
        r = new_request(world.operator, equipment=world.equipment, submit=True)
        act(world.oic, r, "hold", expect=400)
        act(world.oic, r, "hold", reason="Need quotation")
        assert r.status == RS.ON_HOLD and r.held_from_status == RS.PENDING_OIC
        act(world.oic, r, "approve", expect=400)
        act(world.stores, r, "resume", expect=403)
        act(world.oic, r, "resume")
        assert r.status == RS.PENDING_OIC
        act(world.oic, r, "approve")
        assert r.status == RS.PENDING_STORES

    def test_cancel_rules(self, world):
        r = new_request(world.operator, equipment=world.equipment, submit=True)
        act(world.oic, r, "cancel", expect=403, reason="x")
        act(world.operator, r, "cancel", expect=400)
        act(world.operator, r, "cancel", reason="Not needed")
        assert r.status == RS.CANCELLED
        act(world.operator, r, "cancel", expect=400, reason="again")


class TestOfflineHodApproval:
    def _at_hod(self, world):
        r = new_request(world.operator, equipment=world.equipment, lines=[line("30000.00")], submit=True)
        act(world.oic, r, "approve")
        act(world.stores, r, "approve")
        assert r.status == RS.PENDING_HOD
        return r

    def test_office_records_offline_approval_with_signed_document(self, world):
        r = self._at_hod(world)
        offline_post(world.office, r, amount="29000.00")
        assert r.status == RS.APPROVED and r.approved_amount == Decimal("29000.00")
        row = r.approval_actions.get(action="OFFLINE_APPROVE")
        assert row.is_offline and row.offline_approver_name == "Prof. R. Sharma"
        assert row.offline_document.doc_type == c.DocumentType.OFFLINE_APPROVAL
        assert row.offline_document.sha256
        assert row.actor == world.office and row.actor_role == c.ModuleRole.OFFICE

    def test_offline_validation(self, world):
        r = self._at_hod(world)
        offline_post(world.office, r, expect=400, file=None)
        tomorrow = (timezone.localdate() + timedelta(days=1)).isoformat()
        offline_post(world.office, r, expect=400, approval_date=tomorrow)
        from django.core.files.uploadedfile import SimpleUploadedFile

        fake_pdf = SimpleUploadedFile("x.pdf", b"MZ\x90\x00binary", content_type="application/pdf")
        assert offline_post(world.office, r, expect=400, file=fake_pdf).json()["code"] == "invalid_file_content"
        exe = SimpleUploadedFile("x.exe", b"MZ", content_type="application/octet-stream")
        assert offline_post(world.office, r, expect=400, file=exe).json()["code"] == "invalid_file_type"
        offline_post(world.office, r, expect=400, decision="REJECT")
        offline_post(world.office, r, decision="REJECT", comments="Budget exhausted")
        assert r.status == RS.REJECTED

    def test_offline_needs_permission(self, world):
        r = self._at_hod(world)
        offline_post(world.stores, r, expect=403)
        clerk = make_user(user_type=UserType.FINANCE)
        config_service.assign_role(world.admin, world.dept, clerk, c.ModuleRole.OFFICE, ["invoices"])
        offline_post(clerk, r, expect=403)

    def test_mode_in_app_disables_offline(self, world):
        config_service.update_config(world.admin, world.dept, {"hod_approval_mode": "IN_APP"})
        r = self._at_hod(world)
        assert offline_post(world.office, r, expect=400).json()["code"] == "offline_disabled"

    def test_mode_offline_disables_in_app_hod(self, world):
        config_service.update_config(world.admin, world.dept, {"hod_approval_mode": "OFFLINE"})
        r = self._at_hod(world)
        act(world.hod, r, "approve", expect=403)
        offline_post(world.office, r)
        assert r.status == RS.APPROVED


class TestStoresIssueFlow:
    def test_available_then_issue(self, world):
        r = new_request(world.office, rt="GENERAL_OFFICE", lines=[line("50.00", "4")], submit=True)
        act(world.stores, r, "stores-review", decision="AVAILABLE")
        assert r.status == RS.STORES_AVAILABLE
        act(world.stores, r, "issue")
        assert r.status == RS.ISSUED
        assert r.lines.get().issued_quantity == Decimal("4.000")

    def test_partial_issues_and_continues(self, world):
        r = new_request(world.office, rt="GENERAL_OFFICE", lines=[line("50.00", "4")], submit=True)
        ln = r.lines.get()
        act(world.stores, r, "stores-review", decision="PARTIAL", lines=[{"line_id": ln.pk, "quantity": "5"}], expect=400)
        act(world.stores, r, "stores-review", decision="PARTIAL", lines=[{"line_id": ln.pk, "quantity": "4"}], expect=400)
        act(world.stores, r, "stores-review", decision="PARTIAL", lines=[{"line_id": ln.pk, "quantity": "1"}])
        assert r.status == RS.APPROVED
        assert r.approved_amount == Decimal("150.00")
        assert r.lines.get().issued_quantity == Decimal("1.000")

    def test_not_available_proceeds(self, world):
        r = new_request(world.office, rt="GENERAL_OFFICE", submit=True)
        act(world.stores, r, "stores-review", decision="NOT_AVAILABLE")
        assert r.status == RS.APPROVED


class TestInboxAndHistory:
    def test_inbox(self, world):
        r = new_request(world.operator, equipment=world.equipment, submit=True)
        ids = lambda u: [row["id"] for row in client_for(u).get(f"{API}/approvals/").json()["results"]]  # noqa: E731
        assert r.pk in ids(world.oic)
        assert r.pk not in ids(world.stores)
        assert r.pk not in ids(world.operator)
        act(world.oic, r, "approve")
        assert r.pk not in ids(world.oic)
        assert r.pk in ids(world.stores)

    def test_history_is_immutable(self, world):
        r = new_request(world.operator, equipment=world.equipment, submit=True)
        row = ApprovalAction.objects.filter(purchase_request=r).first()
        row.comments = "tampered"
        with pytest.raises(ImmutableRecordError):
            row.save()
        with pytest.raises(ImmutableRecordError):
            ApprovalAction.objects.filter(pk=row.pk).delete()

    def test_disabled_department_blocks_actions(self, world):
        r = new_request(world.operator, equipment=world.equipment, submit=True)
        cfg = config_of(world.dept)
        cfg.module_enabled = False
        cfg.save()
        assert client_for(world.oic).post(f"{API}/requests/{r.pk}/approve/", {}, format="json").status_code in (403, 404)
