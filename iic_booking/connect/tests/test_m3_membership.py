"""M3: community creation and membership sync (historical, future, staff, duplicates, groups, concurrency, API)."""

import threading
from datetime import timedelta
from io import StringIO

import pytest
from django.core.management import call_command
from django.db import connection
from django.utils import timezone

from iic_booking.connect import access
from iic_booking.connect.models import Community
from iic_booking.connect.models import CommunityKind
from iic_booking.connect.models import CommunityMembership
from iic_booking.connect.models import CommunityStatus
from iic_booking.connect.models import CommunitySyncRun
from iic_booking.connect.models import ConnectRole
from iic_booking.connect.models import ConnectRoleAssignment
from iic_booking.connect.models import DiscussionThread
from iic_booking.connect.models import MembershipAuditAction
from iic_booking.connect.models import MembershipAuditLog
from iic_booking.connect.models import MembershipStatus
from iic_booking.connect.services import membership as svc
from iic_booking.equipment.models import BookingStatus
from iic_booking.equipment.models import EquipmentManager
from iic_booking.equipment.models import EquipmentOperator
from iic_booking.equipment.models import EquipmentOperatorCoverage
from iic_booking.equipment.models import EquipmentTemporaryOIC
from iic_booking.users.models.user_type import UserType

from .conftest import API
from .conftest import client_for
from .conftest import general_community
from .conftest import make_booking
from .conftest import make_equipment
from .conftest import make_external
from .conftest import make_group
from .conftest import make_thread
from .conftest import make_user

pytestmark = pytest.mark.django_db


def community_of(group):
    return Community.objects.get(kind=CommunityKind.GROUP, equipment_group_ref_id=group.pk)


def membership(group, user):
    return CommunityMembership.objects.get(community=community_of(group), user=user)


def apply_sync(**kwargs):
    return svc.sync_all(dry_run=False, **kwargs)


# --- Communities from real groups --------------------------------------------------------------------------------


def test_every_group_gets_one_community_idempotently():
    groups = [make_group(f"Spectroscopy {i}") for i in range(3)]
    report = apply_sync()
    assert len(report.communities_created) >= 3
    for g in groups:
        c = community_of(g)
        assert c.name == g.name and c.status == CommunityStatus.ACTIVE and c.equipment_group_id == g.pk
    before = Community.objects.count()
    again = apply_sync()
    assert Community.objects.count() == before
    assert again.communities_created == [] and again.members_created == 0


def test_rename_keeps_slug_and_discussions():
    g = make_group("XRD Lab")
    apply_sync()
    c = community_of(g)
    slug = c.slug
    t = make_thread(c, make_user())
    g.name = "X-ray Diffraction Facility"
    g.save()
    report = apply_sync()
    c.refresh_from_db()
    assert c.name == "X-ray Diffraction Facility" and c.slug == slug
    assert report.communities_renamed and DiscussionThread.objects.filter(pk=t.pk).exists()


def test_removed_group_archives_community_and_keeps_discussions():
    g = make_group("Old Group")
    apply_sync()
    c = community_of(g)
    t = make_thread(c, make_user())
    gid = g.pk
    g.delete()
    report = apply_sync()
    c.refresh_from_db()
    assert c.status == CommunityStatus.ARCHIVED and c.equipment_group_id is None and c.equipment_group_ref_id == gid
    assert DiscussionThread.objects.filter(pk=t.pk).exists()
    assert any(row["group_id"] == gid for row in report.communities_archived)


def test_test_only_group_community_is_flagged():
    g = make_group()
    make_equipment(g, visible_to_test_accounts_only=True)
    apply_sync()
    assert community_of(g).is_test_only


# --- Historical and future bookers --------------------------------------------------------------------------------


def test_historical_bookers_join_on_first_sync(settings):
    settings.IITR_CONNECT_ENABLED = False  # bookings made while Connect was off
    g = make_group()
    eq = make_equipment(g)
    counted = {
        BookingStatus.COMPLETED: make_user(),
        BookingStatus.CANCELLED: make_user(),
        BookingStatus.WAITLISTED: make_user(),
        BookingStatus.REFUNDED: make_user(),
        BookingStatus.BOOKING_NOT_UTILIZED: make_user(),
    }
    for status_value, user in counted.items():
        make_booking(user, eq, status=status_value)
    unpaid = make_user()
    make_booking(unpaid, eq, status=BookingStatus.PENDING_PAYMENT)
    assert not CommunityMembership.objects.exists()
    apply_sync()
    for user in counted.values():
        m = membership(g, user)
        assert m.via_booking and m.status == MembershipStatus.ACTIVE and m.booking_count == 1
    assert not CommunityMembership.objects.filter(user=unpaid).exists()
    assert MembershipAuditLog.objects.filter(action=MembershipAuditAction.JOINED).count() >= len(counted)


def test_future_booking_adds_member_via_signal(connect_on, django_capture_on_commit_callbacks):
    g = make_group()
    eq = make_equipment(g)
    apply_sync()
    user = make_user()
    with django_capture_on_commit_callbacks(execute=True):
        make_booking(user, eq, status=BookingStatus.BOOKED)
    assert membership(g, user).via_booking


def test_pending_payment_booking_joins_when_paid(connect_on, django_capture_on_commit_callbacks):
    g = make_group()
    eq = make_equipment(g)
    user = make_user()
    with django_capture_on_commit_callbacks(execute=True):
        b = make_booking(user, eq, status=BookingStatus.PENDING_PAYMENT)
    assert not CommunityMembership.objects.filter(user=user).exists()
    with django_capture_on_commit_callbacks(execute=True):
        b.status = BookingStatus.BOOKED
        b.save()
    assert membership(g, user).via_booking


def test_signal_is_noop_when_flag_off(settings, django_capture_on_commit_callbacks):
    settings.IITR_CONNECT_ENABLED = False
    g = make_group()
    eq = make_equipment(g)
    with django_capture_on_commit_callbacks(execute=True):
        make_booking(make_user(), eq)
    assert not Community.objects.filter(kind=CommunityKind.GROUP).exists()


def test_mode_child_counts_for_parent_group():
    g = make_group()
    base = make_equipment(g)
    child = make_equipment(None, parent_equipment=base)
    user = make_user()
    make_booking(user, child)
    apply_sync()
    assert membership(g, user).via_booking


def test_external_booker_joins_group_but_not_general(connect_on):
    g = make_group()
    eq = make_equipment(g)
    ext = make_external()
    make_booking(ext, eq)
    apply_sync()
    c = community_of(g)
    access.invalidate(ext)
    assert access.accessible_community_ids(ext) == frozenset({c.pk})
    assert general_community().pk not in access.accessible_community_ids(ext)


def test_booking_source_is_sticky_when_equipment_moves_group():
    a, b = make_group(), make_group()
    eq = make_equipment(a)
    user = make_user()
    make_booking(user, eq)
    apply_sync()
    eq.equipment_group = b
    eq.save()
    apply_sync()
    assert membership(a, user).via_booking and membership(a, user).status == MembershipStatus.ACTIVE
    assert membership(b, user).via_booking


# --- Staff --------------------------------------------------------------------------------------------------------


def test_oic_operator_temp_oic_and_cover_are_members_and_moderators(connect_on):
    g = make_group()
    eq = make_equipment(g)
    oic = make_user(UserType.MANAGER)
    op = make_user(UserType.OPERATOR)
    temp = make_user(UserType.MANAGER)
    cover = make_user(UserType.OPERATOR)
    EquipmentManager.objects.create(equipment=eq, manager=oic)
    EquipmentOperator.objects.create(equipment=eq, operator=op)
    now = timezone.now()
    EquipmentTemporaryOIC.objects.create(
        equipment=eq, primary_oic=oic, temporary_oic=temp, start_at=now - timedelta(hours=1), resume_at=now + timedelta(days=2)
    )
    EquipmentOperatorCoverage.objects.create(
        equipment=eq, primary_operator=op, acting_operator=cover, mode="SECONDARY_OPERATOR",
        starts_at=now - timedelta(hours=1), ends_at=now + timedelta(days=1),
    )
    apply_sync()
    assert membership(g, oic).via_oic and membership(g, temp).via_oic
    assert membership(g, op).via_operator and membership(g, cover).via_operator
    c = community_of(g)
    for u, badge in ((oic, access.BADGE_OIC), (op, access.BADGE_OPERATOR)):
        access.invalidate(u)
        assert access.can_moderate(u, c.pk)
        assert access.role_badge(u, c.pk) == badge


def test_staff_who_leave_lapse_but_bookers_stay(connect_on):
    g = make_group()
    eq = make_equipment(g)
    op_only = make_user(UserType.OPERATOR)
    op_booker = make_user(UserType.OPERATOR)
    a1 = EquipmentOperator.objects.create(equipment=eq, operator=op_only, role=EquipmentOperator.Role.PRIMARY)
    a2 = EquipmentOperator.objects.create(equipment=eq, operator=op_booker, role=EquipmentOperator.Role.SECONDARY)
    make_booking(op_booker, eq)
    apply_sync()
    a1.delete()
    a2.delete()
    report = apply_sync()
    lapsed = membership(g, op_only)
    kept = membership(g, op_booker)
    assert lapsed.status == MembershipStatus.LAPSED and not lapsed.via_operator
    assert kept.status == MembershipStatus.ACTIVE and kept.via_booking and not kept.via_operator
    assert report.members_lapsed == 1
    assert MembershipAuditLog.objects.filter(user=op_only, action=MembershipAuditAction.LAPSED).exists()
    access.invalidate(op_only)
    assert not access.can_read_community(op_only, community_of(g).pk)


def test_expired_temporary_oic_lapses_on_nightly_sync(connect_on):
    g = make_group()
    eq = make_equipment(g)
    oic, temp = make_user(UserType.MANAGER), make_user(UserType.MANAGER)
    EquipmentManager.objects.create(equipment=eq, manager=oic)
    row = EquipmentTemporaryOIC.objects.create(
        equipment=eq, primary_oic=oic, temporary_oic=temp, resume_at=timezone.now() + timedelta(days=1)
    )
    apply_sync()
    assert membership(g, temp).status == MembershipStatus.ACTIVE
    EquipmentTemporaryOIC.objects.filter(pk=row.pk).update(resume_at=timezone.now() - timedelta(minutes=1))
    from iic_booking.connect.tasks import nightly_membership_sync

    nightly_membership_sync()
    assert membership(g, temp).status == MembershipStatus.LAPSED


def test_assignment_signal_updates_group(connect_on, django_capture_on_commit_callbacks):
    g = make_group()
    eq = make_equipment(g)
    apply_sync()
    op = make_user(UserType.OPERATOR)
    with django_capture_on_commit_callbacks(execute=True):
        EquipmentOperator.objects.create(equipment=eq, operator=op)
    assert membership(g, op).via_operator


def test_new_group_signal_creates_community(connect_on, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        g = make_group("Brand new group")
    assert community_of(g).name == "Brand new group"


# --- Manual states survive sync -----------------------------------------------------------------------------------


def test_suspension_and_revocation_survive_sync(connect_on):
    g = make_group()
    eq = make_equipment(g)
    s_user, r_user = make_user(), make_user()
    make_booking(s_user, eq)
    make_booking(r_user, eq)
    apply_sync()
    admin = make_user(UserType.ADMIN)
    c = community_of(g)
    svc.manual_action(c, s_user, "suspend", actor=admin, reason="Spam")
    svc.manual_action(c, r_user, "revoke", actor=admin, reason="Left the institute")
    make_booking(s_user, eq)
    apply_sync()
    assert membership(g, s_user).status == MembershipStatus.SUSPENDED
    assert membership(g, r_user).status == MembershipStatus.REVOKED
    svc.manual_action(c, r_user, "restore", actor=admin)
    assert membership(g, r_user).status == MembershipStatus.ACTIVE


def test_manual_action_never_creates_group_access_from_suspend():
    g = make_group()
    apply_sync()
    outsider = make_user()
    with pytest.raises(svc.MembershipActionError):
        svc.manual_action(community_of(g), outsider, "suspend", actor=make_user(UserType.ADMIN), reason="x")
    assert not CommunityMembership.objects.filter(user=outsider).exists()


def test_external_cannot_be_added_to_general():
    with pytest.raises(svc.MembershipActionError):
        svc.manual_action(general_community(), make_external(), "add", actor=make_user(UserType.ADMIN))


def test_expired_suspension_is_lifted():
    g = make_group()
    eq = make_equipment(g)
    user = make_user()
    make_booking(user, eq)
    apply_sync()
    svc.manual_action(community_of(g), user, "suspend", actor=make_user(UserType.ADMIN), reason="Cool off",
                      until=timezone.now() - timedelta(minutes=1))
    assert svc.lift_expired_suspensions() == 1
    assert membership(g, user).status == MembershipStatus.ACTIVE


# --- Dry run, duplicates, report, command, concurrency ---------------------------------------------------------------


def test_dry_run_writes_nothing_but_reports_everything():
    g = make_group()
    eq = make_equipment(g)
    for _ in range(3):
        make_booking(make_user(), eq)
    counts = (Community.objects.count(), CommunityMembership.objects.count(), MembershipAuditLog.objects.count(),
              CommunitySyncRun.objects.count())
    report = svc.sync_all(dry_run=True)
    assert report.dry_run and report.members_created == 3 and report.communities_created
    assert counts == (Community.objects.count(), CommunityMembership.objects.count(), MembershipAuditLog.objects.count(),
                      CommunitySyncRun.objects.count())


def test_duplicate_bookings_give_one_membership():
    g = make_group()
    eq1, eq2 = make_equipment(g), make_equipment(g)
    user = make_user()
    for eq in (eq1, eq2, eq1):
        make_booking(user, eq)
    apply_sync()
    apply_sync()
    rows = CommunityMembership.objects.filter(community=community_of(g), user=user)
    assert rows.count() == 1 and rows.get().booking_count == 3


def test_apply_records_sync_run_with_report():
    make_group()
    apply_sync(triggered_by=make_user(UserType.ADMIN))
    run = CommunitySyncRun.objects.latest("started_at")
    assert run.status == CommunitySyncRun.Status.DONE and not run.dry_run and "members_created" in run.report


def test_command_defaults_to_dry_run(tmp_path):
    g = make_group()
    make_booking(make_user(), make_equipment(g))
    out = StringIO()
    path = tmp_path / "report.json"
    call_command("connect_sync_communities", "--report", str(path), stdout=out)
    assert "DRY RUN" in out.getvalue() and path.exists()
    assert not Community.objects.filter(kind=CommunityKind.GROUP).exists()
    call_command("connect_sync_communities", "--apply", stdout=StringIO())
    assert community_of(g).memberships.count() == 1


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_concurrent_syncs_never_duplicate():

    g = make_group()
    eq = make_equipment(g)
    users = [make_user() for _ in range(5)]
    for u in users:
        make_booking(u, eq)
    errors = []

    def worker():
        try:
            svc.sync_all(dry_run=False)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert Community.objects.filter(kind=CommunityKind.GROUP, equipment_group_ref_id=g.pk).count() == 1
    assert CommunityMembership.objects.filter(community__equipment_group_ref_id=g.pk).count() == 5
    assert MembershipAuditLog.objects.filter(action=MembershipAuditAction.JOINED, community__equipment_group_ref_id=g.pk).count() == 5


# --- API ------------------------------------------------------------------------------------------------------------


def _setup_two_groups():
    a, b = make_group("Group A"), make_group("Group B")
    eqa, eqb = make_equipment(a), make_equipment(b)
    ua, ub = make_user(), make_user()
    make_booking(ua, eqa)
    make_booking(ub, eqb)
    apply_sync()
    return a, b, ua, ub


def test_api_lists_only_accessible_communities(connect_on):
    a, b, ua, _ub = _setup_two_groups()
    res = client_for(ua).get(f"{API}/communities/")
    assert res.status_code == 200
    names = {c["name"] for c in res.json()["results"]}
    assert "Group A" in names and "Group B" not in names and "General IITR Discussions" in names


def test_api_cross_group_detail_is_404(connect_on):
    a, b, ua, _ub = _setup_two_groups()
    slug_b = community_of(b).slug
    for path in ("", "members/", "audit/"):
        assert client_for(ua).get(f"{API}/communities/{slug_b}/{path}").status_code == 404
    assert client_for(ua).post(f"{API}/communities/{slug_b}/members/{ua.pk}/", {"action": "suspend"}).status_code == 404


def test_api_flag_off_404_and_unauthenticated_401(settings):
    settings.IITR_CONNECT_ENABLED = False
    user = make_user()
    assert client_for(user).get(f"{API}/communities/").status_code == 404
    settings.IITR_CONNECT_ENABLED = True
    assert client_for().get(f"{API}/communities/").status_code in (401, 403)


def test_api_external_cannot_open_general(connect_on):
    ext = make_external()
    assert client_for(ext).get(f"{API}/communities/general/").status_code == 404


def test_members_list_restricted_for_ordinary_members(connect_on):
    g = make_group()
    eq = make_equipment(g)
    member, other, op = make_user(), make_user(), make_user(UserType.OPERATOR)
    make_booking(member, eq)
    make_booking(other, eq)
    EquipmentOperator.objects.create(equipment=eq, operator=op)
    apply_sync()
    slug = community_of(g).slug
    rows = client_for(member).get(f"{API}/communities/{slug}/members/").json()["results"]
    assert {r["user_id"] for r in rows} == {op.pk}
    assert all("email" not in r and "status" not in r for r in rows)
    rows = client_for(op).get(f"{API}/communities/{slug}/members/").json()["results"]
    assert {member.pk, other.pk, op.pk} <= {r["user_id"] for r in rows}
    assert all("email" not in r for r in rows)


def test_moderator_can_suspend_member_but_not_revoke_or_touch_staff(connect_on):
    g = make_group()
    eq = make_equipment(g)
    member, op, oic = make_user(), make_user(UserType.OPERATOR), make_user(UserType.MANAGER)
    make_booking(member, eq)
    EquipmentOperator.objects.create(equipment=eq, operator=op)
    EquipmentManager.objects.create(equipment=eq, manager=oic)
    apply_sync()
    slug = community_of(g).slug
    url = f"{API}/communities/{slug}/members/"
    assert client_for(op).post(f"{url}{member.pk}/", {"action": "revoke", "reason": "x"}).status_code == 403
    assert client_for(op).post(f"{url}{oic.pk}/", {"action": "suspend", "reason": "x"}).status_code == 403
    res = client_for(op).post(f"{url}{member.pk}/", {"action": "suspend", "reason": "Off-topic posts"})
    assert res.status_code == 200 and res.json()["status"] == MembershipStatus.SUSPENDED
    assert client_for(member).post(f"{url}{op.pk}/", {"action": "suspend", "reason": "x"}).status_code == 404


def test_community_admin_can_revoke_and_appoint(connect_on):
    g = make_group()
    eq = make_equipment(g)
    member = make_user()
    make_booking(member, eq)
    apply_sync()
    cadmin = make_user()
    ConnectRoleAssignment.objects.create(user=cadmin, role=ConnectRole.COMMUNITY_ADMIN)
    url = f"{API}/communities/{community_of(g).slug}/members/{member.pk}/"
    assert client_for(cadmin).post(url, {"action": "appoint_moderator"}).json()["role"] == "moderator"
    assert client_for(cadmin).post(url, {"action": "revoke", "reason": "Left"}).json()["status"] == MembershipStatus.REVOKED
    audit = client_for(cadmin).get(f"{API}/communities/{community_of(g).slug}/audit/").json()["results"]
    assert {"revoked", "role_changed"} <= {row["action"] for row in audit}


def test_admin_sync_endpoint_portal_admin_only(connect_on):
    make_group()
    admin, student = make_user(UserType.ADMIN), make_user()
    assert client_for(student).post(f"{API}/admin/sync/", {}, format="json").status_code == 404
    dry = client_for(admin).post(f"{API}/admin/sync/", {}, format="json")
    assert dry.status_code == 200 and dry.json()["dry_run"] is True
    assert not Community.objects.filter(kind=CommunityKind.GROUP).exists()
    applied = client_for(admin).post(f"{API}/admin/sync/", {"apply": True}, format="json")
    assert applied.json()["dry_run"] is False and Community.objects.filter(kind=CommunityKind.GROUP).exists()
